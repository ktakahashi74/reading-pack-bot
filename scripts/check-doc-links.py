#!/usr/bin/env python3
"""Fail when a local Markdown link points outside the repository or is missing."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def markdown_files() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "--", "*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(
        ROOT / name
        for name in result.stdout.splitlines()
        if (ROOT / name).is_file()
    )


def main() -> int:
    failures: list[str] = []
    for document in markdown_files():
        lines = document.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, 1):
            for match in LINK.finditer(line):
                destination = match.group(1).strip().strip("<>")
                parsed = urlsplit(destination)
                if parsed.scheme or destination.startswith("#"):
                    continue
                target = (document.parent / unquote(parsed.path)).resolve()
                try:
                    target.relative_to(ROOT)
                except ValueError:
                    failures.append(
                        f"{document.relative_to(ROOT)}:{line_number}: "
                        "link leaves repository"
                    )
                    continue
                if not target.exists():
                    failures.append(
                        f"{document.relative_to(ROOT)}:{line_number}: missing {destination}"
                    )
    for failure in failures:
        print(failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
