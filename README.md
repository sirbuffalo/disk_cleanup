# Disk Cleanup Agent

A reusable Codex skill for conservative macOS disk cleanup exploration. It scans local disk usage, writes a human-reviewable report, and separates paths into:

- **Extremely low risk to delete**: old caches, logs, temp files, build outputs, dependency artifacts, Trash contents, and old installer/archive files.
- **Worth reviewing**: old projects, cloud-synced folders, app support data, Docker/Xcode/simulator state, and other large paths that may contain user data.

The scanner is read-only. It does not delete, move, archive, upload, empty Trash, use `sudo`, or emit destructive commands.

## Install

Clone this repo, then link or copy the skill folder into Codex's skill directory:

```bash
ln -s "$PWD/disk-cleanup-agent" "$HOME/.codex/skills/disk-cleanup-agent"
```

If you prefer not to use a symlink:

```bash
cp -R disk-cleanup-agent "$HOME/.codex/skills/disk-cleanup-agent"
```

After that, ask Codex to use `$disk-cleanup-agent` for disk cleanup reports.

## Manual Usage

For cleanup reports, the scanner is intentionally runnable with plain Python. It
uses only the Python standard library, so an installed or copied Codex skill does
not need this repo's UV environment.

From the skill folder:

```bash
python3 scripts/scan_disk_cleanup.py
```

Defaults:

- Scope: whole-volume macOS user-relevant paths
- Format: Markdown
- Output: `reports/disk-cleanup-YYYYMMDD-HHMM.md`
- Minimum candidate size: 100 MB
- Markdown scan notes: grouped summaries by default; use `--verbose-notes`
  for full skipped/error lists

Useful examples:

```bash
python3 scripts/scan_disk_cleanup.py --root ~/Downloads --min-size-mb 25
python3 scripts/scan_disk_cleanup.py --json
python3 scripts/scan_disk_cleanup.py --verbose-notes
python3 scripts/scan_disk_cleanup.py --root ~/PycharmProjects --output reports/projects.md
```

The default whole-volume scan prioritizes user-relevant locations instead of
exhaustive filesystem coverage. It scans the current user's home folder,
common shared application/developer locations, `/tmp`-style roots, and the
current user's Darwin temp/cache folders derived from `TMPDIR`.

Granting Full Disk Access to Terminal can reduce macOS privacy prompts and
TCC-related denials for user data, but it does not make the scanner root and
does not bypass POSIX permissions, SIP, Data Vault protections, or root-owned
service directories. Some `EACCES` or `EPERM` results are normal on modern
macOS and are summarized in Markdown while remaining fully available in JSON.

If you are working from this repository checkout and prefer the project-managed
interpreter, use UV explicitly:

```bash
uv run python disk-cleanup-agent/scripts/scan_disk_cleanup.py
```

Use `uv run` for development commands and checks. Use `python3` for portable
manual scanner runs, especially from a copied or symlinked skill folder outside
the repository.

## Development

Set up the project-local environment:

```bash
uv sync --dev
```

Run the tests:

```bash
uv run python -m unittest tests/test_scan_disk_cleanup.py
```

Run formatting and linting:

```bash
uv run ruff format .
uv run ruff check .
```

Run type checks:

```bash
uv run mypy
uv run pyright
```

Run the full local gate:

```bash
scripts/check.sh
```

Install the tracked commit hook in this checkout:

```bash
git config core.hooksPath .githooks
```

The hook runs `scripts/check.sh` before each commit, which verifies Ruff formatting,
Ruff linting, unit tests, mypy, and pyright.

Validate the Codex skill metadata with the skill-creator validator if available:

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py disk-cleanup-agent
```

The test suite uses temporary fixture trees and does not scan or modify your real disk.
