"""Restore a SQLite backup from stdin while web and worker are stopped."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import sys


def restore(database, stream):
    database = Path(database).resolve()
    temporary = database.with_name("restored.sqlite3")
    rescue = database.with_name("before-restore.sqlite3")
    if temporary.exists() or rescue.exists():
        raise RuntimeError("Move existing restored.sqlite3/before-restore.sqlite3 copies elsewhere first")
    # Exclusive creation and private permissions before writing any contents.
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as dest:
        shutil.copyfileobj(stream, dest)
    with closing(sqlite3.connect(temporary.as_uri()+"?mode=ro", uri=True)) as check:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed; live database was not changed")
        if not check.execute("SELECT name FROM sqlite_master WHERE name='django_migrations'").fetchone():
            raise RuntimeError("Input is not a migrated Django database; live database was not changed")
    if database.exists():
        with os.fdopen(os.open(rescue, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"):
            pass
        with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True)) as src, closing(sqlite3.connect(rescue)) as dest:
            src.backup(dest)
    # All connections must be closed. Production services were explicitly stopped.
    for suffix in ("-wal", "-shm"):
        Path(str(database)+suffix).unlink(missing_ok=True)
    temporary.replace(database)
    database.chmod(0o600)
    return rescue if rescue.exists() else None


if __name__ == "__main__":
    try:
        result = restore(os.environ.get("DATABASE_PATH", "/data/db.sqlite3"), sys.stdin.buffer)
        print(f"Restored. Previous database: {result or 'no previous database'}")
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        raise SystemExit(f"Restore failed: {exc}") from exc
