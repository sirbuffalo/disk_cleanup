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

From the skill folder:

```bash
python3 scripts/scan_disk_cleanup.py
```

Defaults:

- Scope: whole-volume macOS user-relevant paths
- Format: Markdown
- Output: `reports/disk-cleanup-YYYYMMDD-HHMM.md`
- Minimum candidate size: 100 MB

Useful examples:

```bash
python3 scripts/scan_disk_cleanup.py --root ~/Downloads --min-size-mb 25
python3 scripts/scan_disk_cleanup.py --json
python3 scripts/scan_disk_cleanup.py --root ~/PycharmProjects --output reports/projects.md
```

## Development

Run the tests:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests/test_scan_disk_cleanup.py
```

Validate the Codex skill metadata with the skill-creator validator if available:

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py disk-cleanup-agent
```

The test suite uses temporary fixture trees and does not scan or modify your real disk.
