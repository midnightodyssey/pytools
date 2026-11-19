#!/usr/bin/env bash
# Local mirror of the release CI gates: ruff, mypy, pytest, plus a UTF-8 check.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif [ -x .venv/Scripts/python ]; then
    PY=.venv/Scripts/python
elif [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
else
    PY=python
fi
echo "check.sh: using interpreter $PY"
$PY -m ruff check src tests scripts
$PY -m ruff format --check src tests scripts
$PY -m mypy
$PY scripts/check_utf8.py
$PY -m pytest -q
