#!/bin/sh
set -eu

fail=0

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo 'error: public audit must run inside a Git worktree' >&2
  exit 2
fi

if matches=$(git grep -En '(xox[baprs]-[A-Za-z0-9-]{8,}|xapp-[A-Za-z0-9-]{8,}|sk-[A-Za-z0-9_-]{16,})' -- ':!scripts/audit-public.sh'); then
  printf '%s\n' "$matches"
  echo 'error: possible credential in tracked content' >&2
  fail=1
else
  status=$?
  [ "$status" -eq 1 ] || exit 2
fi

if matches=$(git grep -En '(/home/|reading-pack-[A-Za-z0-9_-]*-eval)' -- ':!scripts/audit-public.sh'); then
  printf '%s\n' "$matches"
  echo 'error: local path or evaluation-repository reference in tracked content' >&2
  fail=1
else
  status=$?
  [ "$status" -eq 1 ] || exit 2
fi

internal_identifier_pattern='(^|[^[:alnum:]_.-])[[:alnum:]_-]+\.(local|internal)([^[:alnum:]_.-]|$)'\
'|https://[[:alnum:]-]+\.slack\.com/archives/[A-Z0-9]{8,}'\
'|(^|[[:space:]`])#[[:alnum:]_][[:alnum:]_-]{2,}([[:space:]`.,;:]|$)'
if matches=$(git grep -En "$internal_identifier_pattern" -- ':!scripts/audit-public.sh'); then
  printf '%s\n' "$matches"
  echo 'error: internal deployment identifier in tracked content' >&2
  fail=1
else
  status=$?
  [ "$status" -eq 1 ] || exit 2
fi

if matches=$(git ls-files | grep -E '(^|/)(\.?env($|\.)|.*\.(sqlite3?|db|log)(-(wal|shm|journal))?$|config(\.[^.]+)?\.toml$)' | grep -vE '(^|/)\.?env\.example$|(^|/)config\.example\.toml$|\.example\.toml$'); then
  printf '%s\n' "$matches"
  echo 'error: runtime secret, database, or log is tracked' >&2
  fail=1
fi

fixture='tests/fixtures/clockwork-garden-reading-pack.en.md'
expected='d16280ea15f1e516be157b31547bf21d8991444e78a1e94cd12b83f14ac75c4d'
actual=$(sha256sum "$fixture" | awk '{print $1}')
if [ "$actual" != "$expected" ]; then
  echo 'error: synthetic fixture hash changed' >&2
  fail=1
fi

python3 scripts/check-doc-links.py || fail=1

exit "$fail"
