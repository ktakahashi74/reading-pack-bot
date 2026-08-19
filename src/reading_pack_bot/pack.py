"""Read-only validation of generated Reading Pack Markdown artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from types import MappingProxyType

from .errors import PackValidationError
from .models import PackSnapshot

_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECTION_RE = re.compile(r"^##\s+(SYS|BIB|MAP|META)(?:\s+\|.*)?$")
_REQUIRED_COUNT_KEYS = {"chapters", "props", "mis", "names", "gloss", "ref"}
_OPTIONAL_COUNT_KEYS = {"policy"}


def _pairs(line: str, marker: str) -> dict[str, str]:
    if not line.startswith(marker + " | "):
        raise PackValidationError(f"first or final line must begin {marker} |")
    result: dict[str, str] = {}
    for part in line.split(" | ")[1:]:
        if "=" not in part:
            raise PackValidationError(f"{marker} field lacks =")
        key, value = part.split("=", 1)
        if not _KEY_RE.fullmatch(key) or not value:
            raise PackValidationError(f"{marker} contains an invalid field")
        if key in result:
            raise PackValidationError(f"{marker} contains duplicate field {key}")
        if any(ord(character) < 32 for character in value):
            raise PackValidationError(f"{marker} contains control characters")
        result[key] = value
    return result


def _read_bounded(path: Path, max_bytes: int) -> bytes:
    descriptor = -1
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise PackValidationError("pack path must be a regular file")
            if before.st_size <= 0:
                raise PackValidationError("pack is empty")
            if before.st_size > max_bytes:
                raise PackValidationError("pack exceeds configured byte limit")
            raw = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
    except PackValidationError:
        raise
    except OSError as exc:
        if path.is_symlink():
            raise PackValidationError("pack path must not be a symbolic link") from None
        raise PackValidationError(f"cannot read pack: {type(exc).__name__}") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > max_bytes:
        raise PackValidationError("pack exceeds configured byte limit")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise PackValidationError("pack changed while it was being read")
    return raw


def load_pack(
    path: str | Path,
    *,
    max_bytes: int,
    expected_sha256: str | None = None,
) -> PackSnapshot:
    candidate = Path(path)
    if expected_sha256 is not None and not _SHA256_RE.fullmatch(expected_sha256):
        raise PackValidationError("expected SHA-256 is malformed")
    raw = _read_bounded(candidate, max_bytes)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise PackValidationError("pack SHA-256 does not match the operator pin")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise PackValidationError("pack must not contain a UTF-8 BOM")
    if b"\x00" in raw:
        raise PackValidationError("pack contains NUL bytes")
    if b"\r" in raw:
        raise PackValidationError("pack must use LF line endings")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise PackValidationError("pack is not strict UTF-8") from None
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise PackValidationError("pack must end with exactly one LF")
    lines = text[:-1].split("\n")
    if not lines or not lines[0].startswith("PACK | "):
        raise PackValidationError("PACK must be the first physical line")
    if sum(line.startswith("PACK | ") for line in lines) != 1:
        raise PackValidationError("pack must contain exactly one PACK line")
    if not lines[-1].startswith("ENDPACK | "):
        raise PackValidationError("ENDPACK must be the final logical line")
    if sum(line.startswith("ENDPACK | ") for line in lines) != 1:
        raise PackValidationError("pack must contain exactly one ENDPACK line")
    header = _pairs(lines[0], "PACK")
    for required in ("v", "date", "status", "lang", "primary", "profile", "basis"):
        if required not in header:
            raise PackValidationError(f"PACK is missing required field {required}")
    if not _DATE_RE.fullmatch(header["date"]):
        raise PackValidationError("PACK date must use YYYY-MM-DD")
    if len(header["v"]) > 80 or len(header["status"]) > 80:
        raise PackValidationError("PACK version or status is too long")
    if header["status"] not in {"draft", "beta", "canonical", "retired"}:
        raise PackValidationError("PACK status is unsupported")
    if header["status"] == "retired":
        raise PackValidationError("retired packs cannot be served")
    sections: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = _SECTION_RE.fullmatch(line)
        if match:
            name = match.group(1)
            if name in sections:
                raise PackValidationError(f"required section {name} is duplicated")
            sections[name] = index
    if set(sections) != {"SYS", "BIB", "MAP", "META"}:
        raise PackValidationError("pack must contain exactly one SYS, BIB, MAP, and META section")
    if not (sections["SYS"] < sections["BIB"] < sections["MAP"] < sections["META"] < len(lines) - 1):
        raise PackValidationError("required pack sections are out of order")
    names = [
        line[2:].strip()
        for line in lines[1 : sections["SYS"]]
        if line.startswith("# ")
    ]
    if len(names) != 1 or not names[0]:
        raise PackValidationError("pack must contain exactly one non-empty H1 name before SYS")
    h1 = names[0]
    if any(ord(character) < 32 for character in h1):
        raise PackValidationError("pack H1 name contains control characters")
    name, separator, description = h1.rpartition(" — ")
    if not separator:
        name = h1
        description = None
    if "{{" in text or "}}" in text:
        raise PackValidationError("pack contains unresolved template markers")
    raw_counts = _pairs(lines[-1], "ENDPACK")
    count_keys = set(raw_counts)
    if (
        not _REQUIRED_COUNT_KEYS.issubset(count_keys)
        or not count_keys.issubset(_REQUIRED_COUNT_KEYS | _OPTIONAL_COUNT_KEYS)
    ):
        raise PackValidationError(
            "ENDPACK must contain exactly chapters, props, mis, names, gloss, "
            "and ref, with optional policy"
        )
    counts: dict[str, int] = {}
    for key, value in raw_counts.items():
        if not value.isdecimal():
            raise PackValidationError(f"ENDPACK count {key} must be a non-negative integer")
        number = int(value)
        if number > 1_000_000:
            raise PackValidationError(f"ENDPACK count {key} is unreasonably large")
        counts[key] = number
    return PackSnapshot(
        path=candidate.resolve(strict=False),
        raw_markdown=text,
        name=name,
        description=description,
        sha256=digest,
        header=MappingProxyType(header),
        end_counts=MappingProxyType(counts),
        size_bytes=len(raw),
    )
