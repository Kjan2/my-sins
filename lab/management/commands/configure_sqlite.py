from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Enable persistent WAL mode on the local SQLite database after migrate."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL")
            mode=cursor.fetchone()[0]
        self.stdout.write(f"SQLite journal mode: {mode}")
