#!/usr/bin/env python3
"""Read-only macOS disk cleanup scanner.

The scanner walks user-relevant filesystem roots, classifies conservative
cleanup candidates, and writes a Markdown or JSON report. It never deletes,
moves, archives, uploads, or modifies scanned paths.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

MB = 1024 * 1024
LOW_RISK_AGE_DAYS = 14
DOWNLOAD_AGE_DAYS = 30
PROJECT_REVIEW_AGE_DAYS = 120
DEFAULT_MIN_SIZE_MB = 100

LOW_RISK_BUCKET = "extremely low risk"
REVIEW_BUCKET = "review"

INSTALLER_ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".dmg",
    ".pkg",
    ".mpkg",
    ".iso",
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tbz",
    ".tbz2",
    ".txz",
    ".rar",
    ".7z",
    ".xz",
    ".gz",
)

PROJECT_MARKERS: set[str] = {
    ".git",
    ".hg",
    ".svn",
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "pyproject.toml",
    "requirements.txt",
    "Pipfile",
    "poetry.lock",
    "Cargo.toml",
    "go.mod",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "pom.xml",
    "Gemfile",
    "composer.json",
    "pubspec.yaml",
    "Package.swift",
    "Makefile",
}

PROJECT_SUFFIXES: tuple[str, ...] = (
    ".xcodeproj",
    ".xcworkspace",
    ".sln",
    ".csproj",
    ".fsproj",
    ".vcxproj",
)

DEPENDENCY_ARTIFACT_NAMES: set[str] = {
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    ".nox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".parcel-cache",
    ".turbo",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".dart_tool",
    ".build",
    "DerivedData",
    "ModuleCache.noindex",
}

BUILD_OUTPUT_NAMES: set[str] = {
    "build",
    "target",
    "cmake-build-debug",
    "cmake-build-release",
    "cmake-build-relwithdebinfo",
    "cmake-build-minsizerel",
}

LOW_RISK_PATH_SUFFIXES: tuple[tuple[str, ...], ...] = (
    (".gradle", "caches"),
    (".gradle", "wrapper", "dists"),
    (".npm", "_cacache"),
    (".npm", "_npx"),
    (".cargo", "registry", "cache"),
    (".cargo", "git", "checkouts"),
    (".rustup", "downloads"),
    (".rustup", "tmp"),
    (".pub-cache", "hosted"),
    ("Library", "Developer", "Xcode", "DerivedData"),
    ("Library", "Developer", "Xcode", "iOS Device Logs"),
    ("Library", "Caches", "Homebrew"),
    ("Library", "Caches", "pip"),
)

REVIEW_PATH_SUFFIXES: tuple[tuple[str, ...], ...] = (
    ("Library", "Developer", "Xcode", "Archives"),
    ("Library", "Developer", "Xcode", "iOS DeviceSupport"),
    ("Library", "Developer", "CoreSimulator", "Devices"),
    (".docker",),
    ("Library", "Containers", "com.docker.docker"),
    ("Library", "Group Containers", "group.com.docker"),
)

SKIP_NAMES: set[str] = {
    ".DocumentRevisions-V100",
    ".fseventsd",
    ".Spotlight-V100",
    ".TemporaryItems",
    ".vol",
    "Network",
    "dev",
    "net",
    "home",
}

SKIP_PREFIXES: tuple[str, ...] = (
    "/System/Volumes/Preboot",
    "/System/Volumes/Update",
    "/System/Volumes/VM",
    "/System/Volumes/xarts",
    "/System/Volumes/iSCPreboot",
    "/System/Volumes/Hardware",
    "/Library/Developer/CoreSimulator/Volumes",
    "/Library/Developer/CoreSimulator/Cryptex/Images",
)

WHOLE_VOLUME_ROOTS: tuple[str, ...] = (
    "/Users",
    "/Applications",
    "/Library",
    "/opt",
    "/usr/local",
    "/private/tmp",
    "/var/tmp",
    "/private/var/folders",
)


@dataclass(frozen=True)
class Candidate:
    bucket: str
    category: str
    confidence: str
    path: str
    kind: str
    size_bytes: int
    size: str
    newest_modified: str
    age_days: int | None
    reason: str
    evidence: list[str]


@dataclass
class DirSummary:
    size_bytes: int = 0
    newest_mtime: float | None = None
    file_count: int = 0
    dir_count: int = 0


@dataclass
class ScanContext:
    now: float
    min_size_bytes: int
    candidates: list[Candidate]
    errors: list[str]
    skipped: list[str]
    roots: list[str]
    one_file_system: bool = True


def human_size(size_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def path_key(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def parts(path: Path) -> tuple[str, ...]:
    return tuple(path.parts)


def has_suffix(path: Path, suffix: Iterable[str]) -> bool:
    path_parts = parts(path)
    suffix_parts = tuple(suffix)
    return len(path_parts) >= len(suffix_parts) and path_parts[-len(suffix_parts) :] == suffix_parts


def contains_sequence(path: Path, sequence: Iterable[str]) -> int | None:
    path_parts = parts(path)
    seq = tuple(sequence)
    if not seq:
        return None
    for index in range(0, len(path_parts) - len(seq) + 1):
        if path_parts[index : index + len(seq)] == seq:
            return index
    return None


def age_days(now: float, mtime: float | None) -> int | None:
    if mtime is None:
        return None
    return max(0, int((now - mtime) // 86400))


def iso_date(mtime: float | None) -> str:
    if mtime is None:
        return "unknown"
    return dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def old_enough(ctx: ScanContext, mtime: float | None, days: int) -> bool:
    age = age_days(ctx.now, mtime)
    return age is not None and age >= days


def large_enough(ctx: ScanContext, size_bytes: int) -> bool:
    return size_bytes >= ctx.min_size_bytes


def make_candidate(
    *,
    ctx: ScanContext,
    bucket: str,
    category: str,
    confidence: str,
    path: Path,
    kind: str,
    size_bytes: int,
    mtime: float | None,
    reason: str,
    evidence: list[str],
) -> Candidate:
    return Candidate(
        bucket=bucket,
        category=category,
        confidence=confidence,
        path=path_key(path),
        kind=kind,
        size_bytes=size_bytes,
        size=human_size(size_bytes),
        newest_modified=iso_date(mtime),
        age_days=age_days(ctx.now, mtime),
        reason=reason,
        evidence=evidence,
    )


def record_error(ctx: ScanContext, path: Path, exc: OSError) -> None:
    error_number = exc.errno
    label = "ERROR" if error_number is None else errno.errorcode.get(error_number, "ERROR")
    ctx.errors.append(f"{path_key(path)}: {label}: {exc}")


def should_skip_dir(
    path: Path, root_dev: int, stat_result: os.stat_result, ctx: ScanContext
) -> bool:
    path_string = path_key(path)
    if any(
        path_string == prefix or path_string.startswith(prefix + "/") for prefix in SKIP_PREFIXES
    ):
        ctx.skipped.append(f"{path_string}: skipped system/runtime mount")
        return True
    if path.name in SKIP_NAMES:
        ctx.skipped.append(f"{path_string}: skipped system metadata")
        return True
    if path.name == ".Trashes":
        return False
    if ctx.one_file_system and stat_result.st_dev != root_dev:
        ctx.skipped.append(f"{path_string}: skipped different filesystem")
        return True
    return False


def is_download_file(path: Path) -> bool:
    return any(part == "Downloads" for part in parts(path))


def is_installer_or_archive(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(INSTALLER_ARCHIVE_SUFFIXES)


def is_direct_child_of_sequence(path: Path, sequence: tuple[str, ...]) -> bool:
    index = contains_sequence(path, sequence)
    return index is not None and len(parts(path)) == index + len(sequence) + 1


def is_cache_child(path: Path) -> bool:
    return (
        is_direct_child_of_sequence(path, ("Library", "Caches"))
        or is_direct_child_of_sequence(path, (".cache",))
        or is_direct_child_of_sequence(path, ("Library", "Logs"))
    )


def is_var_folder_cache_child(path: Path) -> bool:
    path_parts = parts(path)
    if len(path_parts) < 6:
        return False
    if not path_key(path).startswith("/private/var/folders/"):
        return False
    for index, part in enumerate(path_parts):
        if part in {"C", "T"} and len(path_parts) == index + 2:
            return True
    return False


def is_trash_path(path: Path) -> bool:
    return ".Trash" in parts(path) or ".Trashes" in parts(path)


def has_project_marker(entry_names: set[str]) -> bool:
    if PROJECT_MARKERS.intersection(entry_names):
        return True
    return any(name.endswith(PROJECT_SUFFIXES) for name in entry_names)


def has_nearby_project_marker(path: Path, max_levels: int = 3) -> bool:
    current = path
    for _ in range(max_levels + 1):
        try:
            names = {entry.name for entry in os.scandir(current)}
        except OSError:
            names = set()
        if has_project_marker(names):
            return True
        if current.parent == current:
            return False
        current = current.parent
    return False


def git_remote_evidence(path: Path) -> list[str]:
    git_dir = path / ".git"
    config_path = git_dir / "config"
    if not config_path.exists() and git_dir.is_file():
        try:
            text = git_dir.read_text(errors="ignore")
        except OSError:
            text = ""
        if text.startswith("gitdir:"):
            maybe_git_dir = (path / text.split(":", 1)[1].strip()).resolve()
            config_path = maybe_git_dir / "config"
    try:
        text = config_path.read_text(errors="ignore")
    except OSError:
        return ["git repository detected"]
    if "[remote " in text and "url =" in text:
        return ["git repository detected", "git remote configured"]
    return ["git repository detected"]


def classify_file(path: Path, stat_result: os.stat_result, ctx: ScanContext) -> None:
    size_bytes = stat_result.st_size
    mtime = stat_result.st_mtime
    if not large_enough(ctx, size_bytes):
        return
    if (
        is_download_file(path)
        and is_installer_or_archive(path)
        and old_enough(ctx, mtime, DOWNLOAD_AGE_DAYS)
    ):
        archive_suffix = path.suffix or "compound suffix"
        ctx.candidates.append(
            make_candidate(
                ctx=ctx,
                bucket=LOW_RISK_BUCKET,
                category="old download installer/archive",
                confidence="high",
                path=path,
                kind="file",
                size_bytes=size_bytes,
                mtime=mtime,
                reason="Old installer or archive file in Downloads.",
                evidence=[
                    f"Downloads file with installer/archive suffix: {archive_suffix}",
                    f"modified at least {DOWNLOAD_AGE_DAYS} days ago",
                ],
            )
        )


def classify_directory(
    path: Path, summary: DirSummary, entry_names: set[str], ctx: ScanContext
) -> None:
    if not large_enough(ctx, summary.size_bytes):
        return

    mtime = summary.newest_mtime
    name = path.name

    low_risk_reason: tuple[str, str, list[str]] | None = None
    if is_trash_path(path):
        low_risk_reason = (
            "Trash contents",
            "Path is already in Trash.",
            ["trash path", "manual emptying still required"],
        )
    elif is_cache_child(path) or is_var_folder_cache_child(path):
        low_risk_reason = (
            "cache/log/temp data",
            "Direct child of a known macOS cache, log, or temp container.",
            ["cache/log/temp container", f"modified at least {LOW_RISK_AGE_DAYS} days ago"],
        )
    elif name in DEPENDENCY_ARTIFACT_NAMES:
        low_risk_reason = (
            "dependency/build artifact",
            "Directory name matches a common dependency or generated artifact.",
            [f"artifact directory name: {name}", f"modified at least {LOW_RISK_AGE_DAYS} days ago"],
        )
    elif name in BUILD_OUTPUT_NAMES and has_nearby_project_marker(path.parent):
        low_risk_reason = (
            "derived build output",
            "Build output directory inside or near a detected project.",
            [f"build directory name: {name}", "near project marker"],
        )
    else:
        for low_risk_suffix in LOW_RISK_PATH_SUFFIXES:
            if has_suffix(path, low_risk_suffix):
                low_risk_reason = (
                    "cache/dependency artifact",
                    "Path matches a known cache or generated dependency location.",
                    ["/".join(low_risk_suffix), f"modified at least {LOW_RISK_AGE_DAYS} days ago"],
                )
                break

    if low_risk_reason and old_enough(ctx, mtime, LOW_RISK_AGE_DAYS):
        category, reason, evidence = low_risk_reason
        ctx.candidates.append(
            make_candidate(
                ctx=ctx,
                bucket=LOW_RISK_BUCKET,
                category=category,
                confidence="high",
                path=path,
                kind="directory",
                size_bytes=summary.size_bytes,
                mtime=mtime,
                reason=reason,
                evidence=evidence
                + [f"{summary.file_count} files", f"{summary.dir_count} subdirectories"],
            )
        )
        return

    review_reason: tuple[str, str, list[str]] | None = None
    if has_project_marker(entry_names) and old_enough(ctx, mtime, PROJECT_REVIEW_AGE_DAYS):
        review_reason = (
            "old project",
            "Project-like directory is old enough to review for deletion or online archival.",
            git_remote_evidence(path) + [f"modified at least {PROJECT_REVIEW_AGE_DAYS} days ago"],
        )
    elif (
        is_direct_child_of_sequence(path, ("Library", "Application Support"))
        or is_direct_child_of_sequence(path, ("Library", "Containers"))
        or is_direct_child_of_sequence(path, ("Library", "Group Containers"))
    ) and old_enough(ctx, mtime, PROJECT_REVIEW_AGE_DAYS):
        review_reason = (
            "application support data",
            "Large old app-owned data can contain user data and needs review.",
            ["app support/container path", f"modified at least {PROJECT_REVIEW_AGE_DAYS} days ago"],
        )
    elif (
        is_direct_child_of_sequence(path, ("Dropbox",))
        or is_direct_child_of_sequence(path, ("OneDrive",))
        or is_direct_child_of_sequence(path, ("Google Drive",))
        or is_direct_child_of_sequence(path, ("Library", "Mobile Documents"))
    ) and old_enough(ctx, mtime, PROJECT_REVIEW_AGE_DAYS):
        review_reason = (
            "cloud-synced data",
            "Cloud-synced folder may be removable locally or archivable but can contain user data.",
            ["cloud-synced path", f"modified at least {PROJECT_REVIEW_AGE_DAYS} days ago"],
        )
    else:
        for review_suffix in REVIEW_PATH_SUFFIXES:
            if has_suffix(path, review_suffix) and old_enough(ctx, mtime, PROJECT_REVIEW_AGE_DAYS):
                review_reason = (
                    "developer/app state",
                    "Large old developer or app state may be cleanable but can contain "
                    "useful data.",
                    [
                        "/".join(review_suffix),
                        f"modified at least {PROJECT_REVIEW_AGE_DAYS} days ago",
                    ],
                )
                break

    if review_reason:
        category, reason, evidence = review_reason
        ctx.candidates.append(
            make_candidate(
                ctx=ctx,
                bucket=REVIEW_BUCKET,
                category=category,
                confidence="medium",
                path=path,
                kind="directory",
                size_bytes=summary.size_bytes,
                mtime=mtime,
                reason=reason,
                evidence=evidence
                + [f"{summary.file_count} files", f"{summary.dir_count} subdirectories"],
            )
        )


def scan_dir(path: Path, root_dev: int, ctx: ScanContext) -> DirSummary:
    summary = DirSummary()
    try:
        stat_result = path.stat(follow_symlinks=False)
    except OSError as exc:
        record_error(ctx, path, exc)
        return summary

    if should_skip_dir(path, root_dev, stat_result, ctx):
        return summary

    entry_names: set[str] = set()
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                entry_path = Path(entry.path)
                entry_names.add(entry.name)
                try:
                    if entry.is_symlink():
                        continue
                    entry_stat = entry.stat(follow_symlinks=False)
                    if entry.is_dir(follow_symlinks=False):
                        child = scan_dir(entry_path, root_dev, ctx)
                        summary.size_bytes += child.size_bytes
                        summary.file_count += child.file_count
                        summary.dir_count += child.dir_count + 1
                        if child.newest_mtime is not None:
                            summary.newest_mtime = max(
                                summary.newest_mtime or 0, child.newest_mtime
                            )
                    elif entry.is_file(follow_symlinks=False):
                        summary.size_bytes += entry_stat.st_size
                        summary.file_count += 1
                        summary.newest_mtime = max(summary.newest_mtime or 0, entry_stat.st_mtime)
                        classify_file(entry_path, entry_stat, ctx)
                except OSError as exc:
                    record_error(ctx, entry_path, exc)
    except OSError as exc:
        record_error(ctx, path, exc)
        return summary

    summary.newest_mtime = max(summary.newest_mtime or 0, stat_result.st_mtime)
    classify_directory(path, summary, entry_names, ctx)
    return summary


def default_roots(scope: str) -> list[Path]:
    if scope == "home":
        return [Path.home()]
    roots: list[Path] = []
    seen: set[tuple[int, int]] = set()
    for raw_root in WHOLE_VOLUME_ROOTS:
        root = Path(raw_root).expanduser()
        try:
            stat_result = root.stat()
        except OSError:
            continue
        key = (stat_result.st_dev, stat_result.st_ino)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


def scan_roots(roots: list[Path], ctx: ScanContext) -> None:
    for root in roots:
        try:
            stat_result = root.stat(follow_symlinks=False)
        except OSError as exc:
            record_error(ctx, root, exc)
            continue
        if not stat_result:
            continue
        ctx.roots.append(path_key(root))
        if root.is_dir():
            scan_dir(root, stat_result.st_dev, ctx)
        elif root.is_file():
            classify_file(root, stat_result, ctx)


def is_nested_under(path: str, ancestor: str) -> bool:
    return path != ancestor and path.startswith(ancestor.rstrip("/") + "/")


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    selected: list[Candidate] = []
    for candidate in sorted(
        candidates, key=lambda item: (item.bucket, item.category, item.path.count("/"))
    ):
        if any(
            candidate.bucket == existing.bucket
            and candidate.category == existing.category
            and is_nested_under(candidate.path, existing.path)
            for existing in selected
        ):
            continue
        selected.append(candidate)
    return sorted(
        selected, key=lambda item: (item.bucket != LOW_RISK_BUCKET, -item.size_bytes, item.path)
    )


def markdown_table(candidates: list[Candidate]) -> str:
    lines = [
        "| Size | Age | Category | Confidence | Path | Reason | Evidence |",
        "| ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for item in candidates:
        age = "unknown" if item.age_days is None else f"{item.age_days}d"
        evidence = "; ".join(item.evidence)
        path = item.path.replace("|", "\\|")
        reason = item.reason.replace("|", "\\|")
        evidence = evidence.replace("|", "\\|")
        row = (
            f"| {item.size} | {age} | {item.category} | {item.confidence} | "
            f"`{path}` | {reason} | {evidence} |"
        )
        lines.append(row)
    return "\n".join(lines)


def render_markdown(ctx: ScanContext, started_at: dt.datetime, finished_at: dt.datetime) -> str:
    candidates = dedupe_candidates(ctx.candidates)
    low_risk = [item for item in candidates if item.bucket == LOW_RISK_BUCKET]
    review = [item for item in candidates if item.bucket == REVIEW_BUCKET]
    total_low_risk = sum(item.size_bytes for item in low_risk)
    total_review = sum(item.size_bytes for item in review)

    lines = [
        "# Disk Cleanup Report",
        "",
        f"- Started: {started_at.isoformat(timespec='seconds')}",
        f"- Finished: {finished_at.isoformat(timespec='seconds')}",
        f"- Roots scanned: {', '.join(f'`{root}`' for root in ctx.roots) or 'none'}",
        f"- Minimum item size: {human_size(ctx.min_size_bytes)}",
        f"- Low-risk total shown: {human_size(total_low_risk)} across {len(low_risk)} items",
        f"- Review total shown: {human_size(total_review)} across {len(review)} items",
        "",
        "This report is read-only. It does not delete, move, archive, upload, or empty anything.",
        "",
        "## Extremely Low Risk To Delete",
        "",
    ]
    if low_risk:
        lines.append(markdown_table(low_risk))
    else:
        lines.append("No low-risk candidates met the filters.")

    lines.extend(["", "## Worth Reviewing", ""])
    if review:
        lines.append(markdown_table(review))
    else:
        lines.append("No review candidates met the filters.")

    lines.extend(["", "## Scan Notes", ""])
    if ctx.skipped:
        lines.append("Skipped paths:")
        for item in ctx.skipped[:100]:
            lines.append(f"- `{item}`")
        if len(ctx.skipped) > 100:
            lines.append(f"- ... {len(ctx.skipped) - 100} more skipped paths")
    else:
        lines.append("No skipped paths recorded.")

    lines.append("")
    if ctx.errors:
        lines.append("Permission or read errors:")
        for item in ctx.errors[:100]:
            lines.append(f"- `{item}`")
        if len(ctx.errors) > 100:
            lines.append(f"- ... {len(ctx.errors) - 100} more errors")
    else:
        lines.append("No permission or read errors recorded.")

    lines.append("")
    return "\n".join(lines)


def json_payload(
    ctx: ScanContext, started_at: dt.datetime, finished_at: dt.datetime
) -> dict[str, object]:
    candidates = dedupe_candidates(ctx.candidates)
    return {
        "started": started_at.isoformat(timespec="seconds"),
        "finished": finished_at.isoformat(timespec="seconds"),
        "roots": ctx.roots,
        "min_size_bytes": ctx.min_size_bytes,
        "candidates": [asdict(item) for item in candidates],
        "skipped": ctx.skipped,
        "errors": ctx.errors,
    }


def output_path(raw_output: str | None, report_format: str, started_at: dt.datetime) -> Path:
    suffix = ".json" if report_format == "json" else ".md"
    filename = f"disk-cleanup-{started_at.strftime('%Y%m%d-%H%M')}{suffix}"
    if raw_output is None:
        return Path.cwd() / "reports" / filename
    path = Path(raw_output).expanduser()
    if path.suffix:
        return path
    return path / filename


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only macOS disk cleanup scanner that writes reviewable reports.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--scope",
        choices=("whole-volume", "home"),
        default="whole-volume",
        help="Default scan scope when --root is not provided.",
    )
    parser.add_argument(
        "--root",
        action="append",
        help="Scan this root instead of the default scope. May be repeated.",
    )
    parser.add_argument(
        "--output",
        help="Output file or directory. Defaults to reports/disk-cleanup-YYYYMMDD-HHMM.md.",
    )
    parser.add_argument(
        "--format", choices=("markdown", "json"), default="markdown", help="Primary report format."
    )
    parser.add_argument(
        "--json", action="store_true", help="Also write a JSON sidecar when using Markdown output."
    )
    parser.add_argument(
        "--min-size-mb",
        type=float,
        default=DEFAULT_MIN_SIZE_MB,
        help="Minimum candidate size in MB.",
    )
    parser.add_argument(
        "--include-small",
        action="store_true",
        help="Include candidates below the default size threshold.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SystemExit("Refusing to run as root. Run as the local user without sudo.")

    min_size_mb = 0 if args.include_small else max(0, args.min_size_mb)
    roots = (
        [Path(root).expanduser() for root in args.root] if args.root else default_roots(args.scope)
    )
    started_at = dt.datetime.now()
    ctx = ScanContext(
        now=started_at.timestamp(),
        min_size_bytes=int(min_size_mb * MB),
        candidates=[],
        errors=[],
        skipped=[],
        roots=[],
    )

    scan_roots(roots, ctx)
    finished_at = dt.datetime.now()

    primary_path = output_path(args.output, args.format, started_at)
    if args.format == "json":
        write_text(
            primary_path, json.dumps(json_payload(ctx, started_at, finished_at), indent=2) + "\n"
        )
    else:
        write_text(primary_path, render_markdown(ctx, started_at, finished_at))
        if args.json:
            write_text(
                primary_path.with_suffix(".json"),
                json.dumps(json_payload(ctx, started_at, finished_at), indent=2) + "\n",
            )

    print(primary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
