# Agent Instructions

When changing this repository, keep the Python checks green before handing work back or committing.

Run the full gate:

```bash
scripts/check.sh
```

For formatter-driven edits, run Ruff first:

```bash
uv run ruff format .
uv run ruff check . --fix
scripts/check.sh
```

The gate covers Ruff formatting, Ruff linting, unit tests, mypy, and pyright. Pyright is the same type checker used by Pylance, so treat pyright failures as Pylance-facing issues too.
