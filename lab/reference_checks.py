"""Private reference checks run by the judge worker, never by a web process."""
import hashlib
import json
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .models import Problem, ReferenceCheck
from .services import teacher


def reference_payload(problem, tests=None):
    return {"source": problem.reference_source, "image": settings.JUDGE_IMAGE,
            "time_limit": problem.time_limit, "memory_mb": problem.memory_mb,
            "tests": tests if tests is not None else list(problem.tests.values("label", "input", "expected", "is_sample"))}


def reference_fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


@transaction.atomic
def queue_reference_check(actor, problem_id):
    teacher(actor)
    problem = Problem.objects.get(pk=problem_id)
    problem.full_clean()
    if not problem.reference_source.strip():
        raise ValidationError("Заполните эталонное решение.")
    tests = list(problem.tests.all())
    if not tests or len(tests) > 100:
        raise ValidationError("Для проверки нужно от 1 до 100 тестов.")
    for test in tests:
        test.full_clean()
    payload = reference_payload(problem)
    fingerprint = reference_fingerprint(payload)
    active = problem.reference_checks.filter(fingerprint=fingerprint, status__in=["queued", "running"]).first()
    if active:
        return active
    if ReferenceCheck.objects.filter(status__in=["queued", "running"]).count() >= 3:
        raise ValidationError("Уже проверяются три эталона. Дождитесь результата.")
    return ReferenceCheck.objects.create(problem=problem, actor=actor, payload=payload, fingerprint=fingerprint)


@transaction.atomic
def claim_reference_check():
    item = ReferenceCheck.objects.filter(status="queued").order_by("pk").first()
    if item:
        item.status, item.started_at = "running", timezone.now()
        item.save(update_fields=["status", "started_at"])
    return item
