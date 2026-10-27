from contextlib import closing
import os
from pathlib import Path
import sqlite3
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Consistent online SQLite backup, including writes in the WAL."

    def add_arguments(self, parser):
        parser.add_argument("destination")

    def handle(self, *args, **options):
        destination = Path(options["destination"]).resolve()
        source = Path(settings.DATABASES["default"]["NAME"]).resolve()
        if destination == source or destination.exists():
            raise CommandError("Destination must be a new file distinct from the live database.")
        if not source.is_file():
            raise CommandError("Source database does not exist.")
        with os.fdopen(os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"):
            pass
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(destination)) as dest:
            src.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CommandError("Backup integrity check failed.")
        destination.chmod(0o600)
        self.stdout.write(str(destination))
