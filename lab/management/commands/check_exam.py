"""Machine-readable operational check; suitable for an external scheduler."""
import json
from django.core.management.base import BaseCommand, CommandError
from lab.teacher_views import judge_data


class Command(BaseCommand):
    help = "Check worker heartbeat, queue delay and optionally confirmed offsite backup. Exits nonzero on a problem."

    def add_arguments(self, parser):
        parser.add_argument("--backup-directory", help="Directory containing .last-success.json and the local confirmed backup")

    def handle(self, *args, **options):
        state = judge_data()
        warnings = list(state["warnings"])
        if options["backup_directory"]:
            from scripts.backup_offsite import check
            from contextlib import redirect_stdout
            from io import StringIO
            try:
                with redirect_stdout(StringIO()):
                    check(options["backup_directory"])
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                warnings.append(f"Резервная копия не подтверждена: {exc}")
        self.stdout.write(json.dumps({"healthy": not warnings, "queued": state["queued"],
            "running": state["running"], "oldest_seconds": state["oldest_seconds"],
            "heartbeat_at": state["heartbeat_at"], "warnings": warnings}, ensure_ascii=False))
        if warnings:
            raise CommandError("Exam service requires attention.")
