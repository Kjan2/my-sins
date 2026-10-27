"""Heartbeat uses its own short-lived Django connection, including during Docker calls."""
import logging
import threading
import uuid
from django.db import connections, OperationalError
from django.utils import timezone
from .models import WorkerState

logger = logging.getLogger(__name__)


class WorkerMonitor:
    interval = 5

    def __init__(self):
        self.run_id = uuid.uuid4().hex
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        WorkerState.objects.update_or_create(pk=1, defaults={
            "run_id": self.run_id, "status": "starting", "heartbeat_at": timezone.now(),
            "current_submission": None, "message": ""})
        self.thread = threading.Thread(target=self._beat, name="judge-heartbeat", daemon=True)
        self.thread.start()

    def _beat(self):
        try:
            while not self.stop_event.wait(self.interval):
                try:
                    WorkerState.objects.filter(pk=1, run_id=self.run_id).update(heartbeat_at=timezone.now())
                except OperationalError:
                    logger.warning("Worker heartbeat could not be saved", exc_info=True)
                finally:
                    connections.close_all()
        finally:
            connections.close_all()

    def update(self, status, current=None, message=""):
        WorkerState.objects.filter(pk=1, run_id=self.run_id).update(
            status=status, current_submission_id=current, message=message[:300], heartbeat_at=timezone.now())

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
