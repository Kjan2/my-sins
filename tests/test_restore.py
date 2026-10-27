import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from scripts.restore_database import restore


class RestoreTests(unittest.TestCase):
    def create_db(self,path,value):
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE django_migrations (value TEXT)")
            db.execute("INSERT INTO django_migrations VALUES (?)",(value,))

    def test_restore_keeps_consistent_rescue_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            live,backup=root/"db.sqlite3",root/"backup.sqlite3"
            self.create_db(live,"old")
            self.create_db(backup,"new")
            with backup.open("rb") as stream:
                rescue=restore(live,stream)
            with sqlite3.connect(live) as db:
                self.assertEqual(db.execute("SELECT value FROM django_migrations").fetchone()[0],"new")
            with sqlite3.connect(rescue) as db:
                self.assertEqual(db.execute("SELECT value FROM django_migrations").fetchone()[0],"old")
            self.assertEqual(live.stat().st_mode & 0o777,0o600)
            self.assertEqual(rescue.stat().st_mode & 0o777,0o600)
            with self.assertRaises(RuntimeError): restore(live,io.BytesIO(b""))

    def test_invalid_backup_leaves_live_database_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            live=Path(directory)/"db.sqlite3"
            self.create_db(live,"old")
            with self.assertRaises(sqlite3.Error): restore(live,io.BytesIO(b"invalid"))
            with sqlite3.connect(live) as db:
                self.assertEqual(db.execute("SELECT value FROM django_migrations").fetchone()[0],"old")
