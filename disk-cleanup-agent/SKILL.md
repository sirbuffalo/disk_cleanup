---
name: disk-cleanup-agent
description: Conservative macOS disk cleanup exploration for finding large, old, low-risk delete candidates and separate review-only candidates. Use when Codex is asked to inspect disk usage, caches, Downloads installers, generated build artifacts, old abandoned projects, Docker/Xcode/simulator data, or to prepare a safe cleanup report without deleting anything.
---

# Disk Cleanup Agent

## Operating Rules

Use this skill to explore disk usage on macOS and prepare a reviewable cleanup report.

Never delete, move, archive, upload, empty Trash, run cleanup tools, or issue destructive commands. Do not use `sudo`. Treat the scanner and manual inspection commands as read-only; report paths and reasons so the user can decide what to remove.

## Quick Start

Run the bundled scanner from this skill directory or by absolute path:

```bash
python3 scripts/scan_disk_cleanup.py --scope whole-volume --format markdown
```

Useful options:

```bash
python3 scripts/scan_disk_cleanup.py --root ~/Downloads --min-size-mb 25
python3 scripts/scan_disk_cleanup.py --scope whole-volume --json
python3 scripts/scan_disk_cleanup.py --root ~/PycharmProjects --output reports/projects.md
```

The default output is `reports/disk-cleanup-YYYYMMDD-HHMM.md` in the current working directory.

## Workflow

1. Run `scripts/scan_disk_cleanup.py` with the narrowest root that satisfies the user. Use `--scope whole-volume` when the user asks for a broad Mac scan.
2. Open the generated Markdown report and summarize the largest low-risk items first, then review-only items.
3. If the scanner reports permission errors, mention them briefly. Do not retry with `sudo`.
4. If a path looks important despite a low-risk classification, move it to review in your answer.

## Classification Policy

Low-risk means conservative disposable data only: old caches, logs, temp files, derived build outputs, dependency artifacts, Trash contents, and old installer/archive files in Downloads. The default age thresholds are 14 days for generated data, 30 days for installers/download archives, and 120 days for old projects.

Review-only means large or old data that might be worth deleting or archiving but can contain user data: projects, cloud-synced folders, Application Support data, Docker state, Xcode archives, simulator/device support data, and app containers.

Whole-volume scans focus on writable or user-relevant locations such as `/Users`, `/Applications`, `/Library`, `/opt`, `/usr/local`, `/private/tmp`, and `/private/var/folders`. They skip sealed system roots, read-only mounts, virtual mounts, backup metadata, and OS-critical paths.

## Report Expectations

Each report entry should include path, size, age, category, confidence, reason, and evidence. Present the report as recommendations for manual review only; do not provide `rm` commands or automated deletion scripts.
