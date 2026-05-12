import datetime as dt
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER_PATH = REPO_ROOT / "disk-cleanup-agent" / "scripts" / "scan_disk_cleanup.py"


def load_scanner():
    spec = importlib.util.spec_from_file_location("scan_disk_cleanup", SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
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
    def test_cli_defaults_to_whole_volume_markdown(self):
        with mock.patch.object(sys, "argv", ["scan_disk_cleanup.py"]):
            args = scanner.parse_args()
        self.assertEqual(args.scope, "whole-volume")
        self.assertEqual(args.format, "markdown")

    def test_fixture_classification_and_markdown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Users" / "davis"
            write_blob(root / "Library" / "Caches" / "BigCache" / "blob.bin", 2 * scanner.MB, 20)
            write_blob(root / "Downloads" / "OldInstaller.dmg", 3 * scanner.MB, 45)
            write_blob(root / "PycharmProjects" / "old-project" / "data.bin", 2 * scanner.MB, 150)
            git_config = root / "PycharmProjects" / "old-project" / ".git" / "config"
            git_config.parent.mkdir(parents=True, exist_ok=True)
            git_config.write_text('[remote "origin"]\n\turl = git@example.com:repo/project.git\n')
            write_blob(root / "Library" / "Application Support" / "BigApp" / "state.db", 2 * scanner.MB, 150)
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

    def test_cli_writes_markdown_report_and_json_sidecar(self):
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

    def test_permission_errors_are_recorded(self):
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

    def test_source_has_no_destructive_filesystem_calls(self):
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
