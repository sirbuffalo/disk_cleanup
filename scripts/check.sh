#!/usr/bin/env bash
set -euo pipefail

uv run ruff format --check .
uv run ruff check .
uv run python -m unittest tests/test_scan_disk_cleanup.py
uv run mypy
uv run pyright
