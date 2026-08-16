#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

bytecode_dir=$(mktemp -d)
trap 'rm -rf "$bytecode_dir"' EXIT HUP INT TERM

PYTHONPYCACHEPREFIX="$bytecode_dir" PYTHONPATH=src python3 -m compileall -q src tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
sh scripts/audit-public.sh
git diff --check
