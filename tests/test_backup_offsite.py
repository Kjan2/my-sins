import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts.backup_offsite import backup, check


class OffsiteBackupTests(unittest.TestCase):
    def run_backup(self, directory, fail_upload=False):
        def command(args, **kwargs):
            if "cp" in args:
                Path(args[-1]).write_bytes(b"fixture backup")
            if args[0] == "scp" and fail_upload:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        with patch("scripts.backup_offsite.subprocess.run", side_effect=command) as calls:
            result = backup("/project", directory, "backup@storage:/private/contest", keep=2)
            self.assertTrue(any(call.args[0][0] == "scp" and call.kwargs["check"] for call in calls.call_args_list))
            self.assertIn("rm", calls.call_args_list[-1].args[0])
        return result

    def test_confirmed_upload_records_success_and_retains_latest_two(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "contest-20000101T000000Z-old.sqlite3"
            old.write_bytes(b"old")
            (root / "contest-20000102T000000Z-old.sqlite3").write_bytes(b"old2")
            unrelated = root / "other.sqlite3"
            unrelated.write_bytes(b"keep")
            result = self.run_backup(root)
            self.assertFalse(old.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(result.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((root / ".last-success.json").read_text())["file"], result.name)
            check(root)

    def test_failed_upload_does_not_record_success_or_delete_old_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "contest-20000101T000000Z-old.sqlite3"
            old.write_bytes(b"old")
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_backup(root, fail_upload=True)
            self.assertTrue(old.exists())
            self.assertFalse((root / ".last-success.json").exists())
            self.assertEqual(len(list(root.glob("contest-*.sqlite3"))), 2)

    def test_stale_or_missing_local_copy_fails_monitoring(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / ".last-success.json"
            marker.write_text(json.dumps({"file": "missing.sqlite3", "completed_at": time.time()}))
            with self.assertRaises(RuntimeError): check(root)
            (root / "missing.sqlite3").touch()
            marker.write_text(json.dumps({"file": "missing.sqlite3", "completed_at": time.time()-27*3600}))
            with self.assertRaises(RuntimeError): check(root)

    def test_invalid_remote_is_rejected_before_running_commands(self):
        with patch("scripts.backup_offsite.subprocess.run") as command:
            for remote in ("", "relative/path", "host:/unsafe path", "host:/path;command"):
                with self.assertRaises(ValueError): backup("/project", "/unused", remote)
            command.assert_not_called()
