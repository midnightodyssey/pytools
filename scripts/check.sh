#!/usr/bin/env bash
# Local mirror of the release CI gates: ruff, mypy, pytest.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
$PY -m ruff check src tests
$PY -m ruff format --check src tests
$PY -m mypy
$PY -m pytest -q
