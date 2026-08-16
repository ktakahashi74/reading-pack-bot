#!/bin/sh
set -eu

if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo 'usage: scripts/verify-wheel.sh <wheel>' >&2
  exit 2
fi

temporary=$(mktemp -d "${TMPDIR:-/tmp}/reading-pack-bot-wheel.XXXXXX")
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

python3 -m zipfile -e "$1" "$temporary"
diff -ru --exclude=__pycache__ src/reading_pack_bot "$temporary/reading_pack_bot"
