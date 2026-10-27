"""Back up a Compose installation, upload via SSH, and record confirmed success."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4


def backup(project, directory, remote, keep=14):
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+:/[A-Za-z0-9_./-]+", remote):
        raise ValueError("Remote must be user@host:/absolute/path without spaces")
    if keep < 1:
        raise ValueError("Keep at least one local backup")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    name = time.strftime("contest-%Y%m%dT%H%M%SZ-", time.gmtime()) + uuid4().hex[:8] + ".sqlite3"
    local = directory / name
    partial = local.with_suffix(".partial")
    container_path = "/data/" + name
    compose = ["docker", "compose", "--project-directory", str(Path(project).resolve())]
    try:
        subprocess.run([*compose, "exec", "-T", "web", "python", "manage.py", "backup_database", container_path], check=True)
        subprocess.run([*compose, "cp", "web:" + container_path, str(partial)], check=True)
        partial.chmod(0o600)
        partial.replace(local)
        subprocess.run(["scp", "-B", "-oBatchMode=yes", "-oConnectTimeout=15", str(local), remote.rstrip("/") + "/"], check=True, timeout=300)
        success = directory / ".last-success.json"
        temporary = directory / ".last-success.tmp"
        temporary.write_text(json.dumps({"completed_at": time.time(), "file": name}) + "\n")
        temporary.replace(success)
        older = sorted((path for path in directory.glob("contest-*.sqlite3") if path != local),
                       key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for old in older[keep-1:]:
            old.unlink()
        print(f"Backup and remote upload confirmed: {name}")
        return local
    finally:
        subprocess.run([*compose, "exec", "-T", "web", "rm", "-f", container_path], check=True, timeout=60)
        partial.unlink(missing_ok=True)


def check(directory, max_age_hours=26):
    directory = Path(directory)
    success = json.loads((directory / ".last-success.json").read_text())
    age = time.time() - success["completed_at"]
    if age < 0 or age > max_age_hours * 3600 or not (directory / success["file"]).is_file():
        raise RuntimeError("No recent confirmed offsite backup")
    print(f"Last confirmed offsite backup: {age / 3600:.1f} hours ago")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--directory", type=Path, default=os.environ.get("CONTEST_BACKUP_DIR"))
    parser.add_argument("--remote", default=os.environ.get("CONTEST_BACKUP_REMOTE", ""))
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.directory is None:
        parser.error("Set --directory or CONTEST_BACKUP_DIR")
    os.umask(0o077)
    if args.check:
        check(args.directory)
    else:
        backup(args.project, args.directory, args.remote, args.keep)
