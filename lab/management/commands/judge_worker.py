import fcntl
import logging
import signal
import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from judge.docker_runner import DockerJudge, InfrastructureError
from lab.models import Submission, ReferenceCheck
from lab.services import claim_submission, problem_for
from lab.reference_checks import claim_reference_check
from lab.worker_monitor import WorkerMonitor

logger = logging.getLogger(__name__)


class WorkerStopped(BaseException):
    pass


class Command(BaseCommand):
    help = "Single Docker judge worker. Web never imports or calls the Docker runner."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one queued submission")

    def handle(self, *args, **options):
        try:
            with open(settings.WORKER_LOCK, "a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise CommandError("Another worker holds the lock; use one worker per installation.")
                self.run(options["once"])
        except OSError as exc:
            raise CommandError(f"Cannot open worker lock: {exc}") from exc

    def run(self, once):
        judge = DockerJudge(settings.JUDGE_IMAGE, settings.JUDGE_PREFIX)
        old_handlers = {}
        def stop(signum, frame):
            raise WorkerStopped()
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.signal(sig, stop)
        current = None
        monitor = WorkerMonitor()
        failed = ""
        try:
            monitor.start()
            Submission.objects.filter(status="running").update(status="infra_error", finished_at=timezone.now(), result={"message": "Предыдущая проверка прервана. Требуется повторная проверка преподавателем."})
            ReferenceCheck.objects.filter(status="running").update(status="infra_error", finished_at=timezone.now(), result={"message": "Проверка прервана. Запустите эталон снова."})
            judge.cleanup_stale()
            monitor.update("idle")
            while True:
                current = claim_submission() or claim_reference_check()
                if current is None:
                    if once:
                        return
                    time.sleep(1)
                    continue
                try:
                    reference = isinstance(current, ReferenceCheck)
                    monitor.update("running", None if reference else current.pk,
                                   message=f"Проверка эталона задачи #{current.problem_id}" if reference else "")
                    # No transaction is open during Docker execution.
                    if reference:
                        if current.payload.get("image") != settings.JUDGE_IMAGE:
                            outcome = {"status": "infra_error", "result": {"message": "Образ проверки изменился. Запустите эталон заново."}}
                        else:
                            outcome = judge.judge(current.payload["source"], current.payload, "judge")
                    else:
                        outcome = judge.judge(current.source, problem_for(current.attempt, current.problem_id), current.kind,
                                              custom_input=current.custom_input)
                    type(current).objects.filter(pk=current.pk, status="running").update(**outcome, finished_at=timezone.now())
                    logger.info("Judge completed type=%s id=%s status=%s", type(current).__name__, current.pk, outcome["status"])
                    monitor.update("idle")
                    current = None
                except InfrastructureError:
                    raise
                except Exception:
                    failed = "Ошибка проверяющего процесса. Требуется проверка журнала worker."
                    logger.exception("Unexpected judge failure on submission %s", current.pk)
                    type(current).objects.filter(pk=current.pk, status="running").update(status="infra_error", result={"message": "Ошибка проверяющего процесса. Требуется повторная проверка."}, finished_at=timezone.now())
                    current = None
                    raise CommandError("Unexpected judge failure; worker stopped safely.")
                if once:
                    return
        except WorkerStopped:
            self.stdout.write("Worker stopped.")
        except InfrastructureError as exc:
            failed = "Ошибка инфраструктуры или очистки Docker. Проверьте журнал worker."
            raise CommandError(str(exc)) from exc
        except Exception:
            failed = failed or "Worker завершился с ошибкой. Проверьте журнал."
            raise
        finally:
            # Ignore further stop signals while deleting the current containers/volume.
            for sig in old_handlers:
                signal.signal(sig, signal.SIG_IGN)
            cleanup_failed = False
            try:
                try:
                    if current:
                        type(current).objects.filter(pk=current.pk, status="running").update(status="infra_error", result={"message": "Проверка прервана. Требуется повторная проверка преподавателем."}, finished_at=timezone.now())
                finally:
                    judge.cleanup_stale()
            except InfrastructureError:
                cleanup_failed = True
                failed = "Очистка Docker не подтверждена. Следующий запуск должен очистить оставшиеся ресурсы."
                logger.error("Docker cleanup unavailable; next worker startup must clean stale resources.")
            except Exception:
                failed = failed or "Ошибка при завершении worker. Проверьте журнал."
                raise
            finally:
                monitor.stop()
                try:
                    monitor.update("error" if failed else "stopped", message=failed)
                finally:
                    for sig, handler in old_handlers.items():
                        signal.signal(sig, handler)
            if cleanup_failed:
                raise CommandError("Docker cleanup failed; resources must be cleaned on next startup.")
