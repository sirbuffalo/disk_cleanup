import datetime as dt
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER_PATH = REPO_ROOT / "disk-cleanup-agent" / "scripts" / "scan_disk_cleanup.py"


def load_scanner() -> Any:
    spec = importlib.util.spec_from_file_location("scan_disk_cleanup", SCANNER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scanner = load_scanner()


def write_blob(path: Path, size_bytes: int, days_old: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size_bytes)
    old_timestamp = (dt.datetime.now() - dt.timedelta(days=days_old)).timestamp()
    os.utime(path, (old_timestamp, old_timestamp))


def make_old_tree(path: Path, days_old: int) -> None:
    old_timestamp = (dt.datetime.now() - dt.timedelta(days=days_old)).timestamp()
    for current, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.utime(Path(current) / name, (old_timestamp, old_timestamp))
        for name in dirs:
            os.utime(Path(current) / name, (old_timestamp, old_timestamp))
        os.utime(current, (old_timestamp, old_timestamp))


class DiskCleanupScannerTests(unittest.TestCase):
    def test_cli_defaults_to_whole_volume_markdown(self) -> None:
        with mock.patch.object(sys, "argv", ["scan_disk_cleanup.py"]):
            args = scanner.parse_args()
        self.assertEqual(args.scope, "whole-volume")
        self.assertEqual(args.format, "markdown")
        self.assertFalse(args.verbose_notes)

    def test_whole_volume_defaults_to_user_relevant_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "Users" / "davis"
            darwin_tmp = Path(temp_dir) / "var" / "folders" / "rt" / "session" / "T"
            darwin_cache = darwin_tmp.parent / "C"
            home.mkdir(parents=True)
            darwin_tmp.mkdir(parents=True)
            darwin_cache.mkdir(parents=True)

            with mock.patch.dict(os.environ, {"HOME": str(home), "TMPDIR": str(darwin_tmp)}):
                roots = {str(path) for path in scanner.default_roots("whole-volume")}

            self.assertIn(str(home), roots)
            self.assertIn(str(darwin_tmp), roots)
            self.assertIn(str(darwin_cache), roots)
            self.assertNotIn(str(home.parent), roots)
            self.assertNotIn("/private/var/folders", roots)

    def test_fixture_classification_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Users" / "davis"
            write_blob(root / "Library" / "Caches" / "BigCache" / "blob.bin", 2 * scanner.MB, 20)
            write_blob(root / "Downloads" / "OldInstaller.dmg", 3 * scanner.MB, 45)
            write_blob(root / "PycharmProjects" / "old-project" / "data.bin", 2 * scanner.MB, 150)
            git_config = root / "PycharmProjects" / "old-project" / ".git" / "config"
            git_config.parent.mkdir(parents=True, exist_ok=True)
            git_config.write_text('[remote "origin"]\n\turl = git@example.com:repo/project.git\n')
            write_blob(
                root / "Library" / "Application Support" / "BigApp" / "state.db",
                2 * scanner.MB,
                150,
            )
            make_old_tree(root, 150)
            write_blob(root / "Downloads" / "RecentInstaller.pkg", 3 * scanner.MB, 2)

            ctx = scanner.ScanContext(
                now=dt.datetime.now().timestamp(),
                min_size_bytes=scanner.MB,
                candidates=[],
                errors=[],
                skipped=[],
                roots=[],
            )
            scanner.scan_roots([root], ctx)
            candidates = scanner.dedupe_candidates(ctx.candidates)
            paths = {Path(item.path).name: item for item in candidates}

            self.assertEqual(paths["BigCache"].bucket, scanner.LOW_RISK_BUCKET)
            self.assertEqual(paths["OldInstaller.dmg"].bucket, scanner.LOW_RISK_BUCKET)
            self.assertNotIn("RecentInstaller.pkg", paths)
            self.assertEqual(paths["old-project"].bucket, scanner.REVIEW_BUCKET)
            self.assertEqual(paths["BigApp"].bucket, scanner.REVIEW_BUCKET)

            report = scanner.render_markdown(ctx, dt.datetime.now(), dt.datetime.now())
            self.assertIn("Extremely Low Risk To Delete", report)
            self.assertIn("Worth Reviewing", report)
            self.assertIn("OldInstaller.dmg", report)
            self.assertNotIn("rm -", report)
            self.assertNotIn("sudo ", report)

    def test_common_project_directory_names_are_not_globally_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Users" / "davis"
            for name in ("dev", "net", "home", "Network"):
                write_blob(root / name / "node_modules" / "blob.bin", scanner.MB, 30)
            make_old_tree(root, 30)

            ctx = scanner.ScanContext(
                now=dt.datetime.now().timestamp(),
                min_size_bytes=scanner.MB,
                candidates=[],
                errors=[],
                skipped=[],
                roots=[],
            )
            scanner.scan_roots([root], ctx)
            paths = {item.path for item in scanner.dedupe_candidates(ctx.candidates)}

            for name in ("dev", "net", "home", "Network"):
                self.assertTrue(
                    any(f"/{name}/node_modules" in path for path in paths),
                    f"expected candidate under {name}",
                )
            self.assertFalse(any("skipped system metadata" in item for item in ctx.skipped))

    def test_exact_system_roots_are_still_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            normal_dev = root / "dev"
            normal_dev.mkdir()
            stat_result = root.stat()
            ctx = scanner.ScanContext(
                now=dt.datetime.now().timestamp(),
                min_size_bytes=0,
                candidates=[],
                errors=[],
                skipped=[],
                roots=[],
            )

            self.assertTrue(
                scanner.should_skip_dir(Path("/dev"), stat_result.st_dev, stat_result, ctx)
            )
            self.assertFalse(
                scanner.should_skip_dir(normal_dev, stat_result.st_dev, normal_dev.stat(), ctx)
            )
            self.assertIn("/dev: skipped system root", ctx.skipped)

    def test_cli_writes_markdown_report_and_json_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Users" / "davis"
            write_blob(root / "Library" / "Caches" / "App" / "cache.bin", scanner.MB, 30)
            make_old_tree(root, 30)
            output = Path(temp_dir) / "reports" / "scan.md"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCANNER_PATH),
                    "--root",
                    str(root),
                    "--output",
                    str(output),
                    "--min-size-mb",
                    "1",
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.stdout.strip(), str(output))
            self.assertTrue(output.exists())
            self.assertTrue(output.with_suffix(".json").exists())
            self.assertIn("App", output.read_text())

    def test_markdown_scan_notes_are_grouped_unless_verbose(self) -> None:
        ctx = scanner.ScanContext(
            now=dt.datetime.now().timestamp(),
            min_size_bytes=0,
            candidates=[],
            errors=[
                "/private/secret: EACCES: [Errno 13] Permission denied: '/private/secret'",
                "/private/vault: EPERM: [Errno 1] Operation not permitted: '/private/vault'",
            ],
            skipped=[
                "/dev: skipped system root",
                "/net: skipped system root",
                "/mnt: skipped different filesystem",
            ],
            roots=[],
        )

        default_report = scanner.render_markdown(ctx, dt.datetime.now(), dt.datetime.now())
        verbose_report = scanner.render_markdown(
            ctx, dt.datetime.now(), dt.datetime.now(), verbose_notes=True
        )

        self.assertIn("Skipped paths: 3 total", default_report)
        self.assertIn("skipped system root: 2 paths", default_report)
        self.assertIn("Permission or read errors: 2 total", default_report)
        self.assertIn("Full details are available in JSON output", default_report)
        self.assertNotIn("`/dev: skipped system root`", default_report)
        self.assertIn("`/dev: skipped system root`", verbose_report)
        self.assertIn("`/private/secret: EACCES:", verbose_report)

    def test_app_bundle_internals_are_not_low_risk_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Applications"
            app = root / "Cursor.app"
            write_blob(
                app / "Contents" / "Resources" / "app" / "node_modules" / "blob.bin",
                2 * scanner.MB,
                180,
            )
            make_old_tree(root, 180)

            ctx = scanner.ScanContext(
                now=dt.datetime.now().timestamp(),
                min_size_bytes=scanner.MB,
                candidates=[],
                errors=[],
                skipped=[],
                roots=[],
            )
            scanner.scan_roots([root], ctx)
            candidates = scanner.dedupe_candidates(ctx.candidates)

            self.assertFalse(
                any(
                    item.bucket == scanner.LOW_RISK_BUCKET and "Cursor.app/Contents" in item.path
                    for item in candidates
                )
            )
            self.assertTrue(
                any(
                    item.bucket == scanner.REVIEW_BUCKET and item.path.endswith("Cursor.app")
                    for item in candidates
                )
            )

    def test_permission_errors_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Users" / "davis"
            blocked = root / "Library" / "Caches" / "Blocked"
            blocked.mkdir(parents=True)
            os.chmod(blocked, 0)
            try:
                ctx = scanner.ScanContext(
                    now=dt.datetime.now().timestamp(),
                    min_size_bytes=0,
                    candidates=[],
                    errors=[],
                    skipped=[],
                    roots=[],
                )
                scanner.scan_roots([root], ctx)
                self.assertTrue(ctx.errors)
            finally:
                os.chmod(blocked, 0o700)

    def test_source_has_no_destructive_filesystem_calls(self) -> None:
        source = SCANNER_PATH.read_text()
        forbidden = (
            "os.remove(",
            "os.rmdir(",
            "shutil.rmtree(",
            ".unlink(",
            ".rename(",
        )
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
