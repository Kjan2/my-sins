import json
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST
from . import services
from .models import Assessment, Attempt, Draft, Submission
from .problem_content import problem_sections

DEFAULT_SOURCE = '#include <iostream>\nusing namespace std;\n\nint main() {\n    return 0;\n}\n'
SUBMISSION_META = ("id", "problem_id", "kind", "created_at", "status")


def task_progress(user, item):
    drafted = set(Draft.objects.filter(attempt=item).values_list("problem_id", flat=True))
    judged = list(item.submissions.filter(kind="judge").values_list("problem_id", "status"))
    visible = services.results_visible(user, item)
    return {str(p["id"]): "Принято" if visible and (p["id"], "accepted") in judged else
            "Отправлено" if any(pk == p["id"] for pk, _ in judged) else
            "Есть черновик" if p["id"] in drafted else "Нет отправок" for p in item.snapshot["problems"]}


def owned(user, pk):
    try:
        return services.accessible_attempt(user, pk)
    except Attempt.DoesNotExist:
        raise Http404


def public_submission(user, item, attempt, *, include_result=True):
    visible = item.kind in ("sample", "custom") or services.results_visible(user, attempt)
    data = {"id": item.pk, "problem_id": item.problem_id, "kind": item.get_kind_display(), "kind_code": item.kind,
            "problem_title": services.problem_for(attempt, item.problem_id)["title"],
            "created_at": item.created_at.isoformat(), "status": item.status if visible else "received",
            "status_label": item.get_status_display() if visible else "Отправка принята"}
    if include_result:
        data["result"] = item.result if visible else {}
        data["source"] = item.source
        if item.kind == "custom":
            data["custom_input"] = item.custom_input
    if item.kind == "custom" and item.status == "accepted":
        data["status_label"] = "Выполнено"
    return data


def clock_data(item):
    return {"server_now": timezone.now().isoformat(), "deadline": item.deadline.isoformat(),
            "closed": bool(item.finished_at or timezone.now() >= item.deadline)}


def json_body(request):
    data = json.loads(request.body)
    if not isinstance(data, dict):
        raise ValueError
    return data


def api_error(exc):
    if isinstance(exc, services.Conflict):
        return JsonResponse({"error": " ".join(exc.messages), "conflict": exc.field}, status=409)
    if isinstance(exc, ValidationError):
        return JsonResponse({"error": " ".join(exc.messages)}, status=409 if isinstance(exc, services.Closed) else 400)
    return JsonResponse({"error": "Некорректный запрос."}, status=400)


def expected_revision(data):
    value = data.get("expected_revision")
    return value if type(value) is int else "missing"


@login_required
def dashboard(request):
    attempts = {a.assessment_id: a for a in Attempt.objects.filter(student=request.user).select_related("version")}
    rows = []
    for work in Assessment.objects.select_related("current_version", "course").order_by("-pk"):
        old = attempts.get(work.pk)
        version = old.version if old else work.current_version
        if not version or (not old and not services.enrolled(request.user, version.snapshot, work.course_id)):
            continue
        s = version.snapshot
        now = timezone.now()
        available = parse_datetime(s["opens_at"]) <= now < parse_datetime(s["closes_at"])
        status = ("Завершена" if old.finished_at else "Срок истёк" if now >= old.deadline else "В работе") if old else ("Можно начать" if available else "Ещё не открыта" if now < parse_datetime(s["opens_at"]) else "Закрыта")
        rows.append({"id": work.pk, "title": s["title"], "course": work.course.title, "kind": "Зачёт" if s["kind"] == "exam" else "Лабораторная", "opens_at": parse_datetime(s["opens_at"]), "closes_at": parse_datetime(s["closes_at"]), "attempt": old, "available": available, "status": status})
    return render(request, "lab/dashboard.html", {"rows": rows})


@login_required
def rules(request, pk):
    work = get_object_or_404(Assessment.objects.select_related("current_version"), pk=pk, current_version__isnull=False)
    old = Attempt.objects.filter(student=request.user, assessment=work).first()
    if old:
        return redirect("attempt", pk=old.pk)
    s = work.current_version.snapshot
    if not services.enrolled(request.user, s, work.course_id):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    return render(request, "lab/rules.html", {"work_id": work.pk, "title": s["title"], "instructions": s["instructions"], "duration": s["duration_minutes"], "opens_at": parse_datetime(s["opens_at"]), "closes_at": parse_datetime(s["closes_at"]), "pass_score": s["pass_score"], "limit": s["submission_limit"], "deferred": s["reveal_after_close"], "version": work.current_version.number})


@login_required
@require_POST
def start(request, pk):
    try:
        item = services.start_attempt(request.user, pk)
    except Assessment.DoesNotExist:
        raise Http404
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("dashboard")
    return redirect("attempt", pk=item.pk)


@login_required
def attempt(request, pk):
    item = owned(request.user, pk)
    draft_objects = {d.problem_id: d for d in Draft.objects.filter(attempt=item)}
    drafts = {pk: d.source for pk, d in draft_objects.items()}
    answers = {a.question_id: a for a in item.answers.all()}
    submissions = list(item.submissions.only(*SUBMISSION_META).order_by("-pk")[:50])
    linked_submissions = list(item.submissions.filter(kind="judge").values("id", "problem_id"))
    progress = task_progress(request.user, item)
    problems = [{"id": p["id"], "title": p["title"], "statement": p["statement"], "sections": problem_sections(p),
                 "time_limit": p["time_limit"], "memory_mb": p["memory_mb"], "max_score": p["max_score"],
                 "required": p["required"], "source": drafts.get(p["id"], p.get("starter_code") or DEFAULT_SOURCE),
                 "revision": draft_objects[p["id"]].revision if p["id"] in draft_objects else 0,
                 "samples": [t for t in p["tests"] if t["is_sample"]]} for p in item.snapshot["problems"]]
    for p in problems:
        p["progress"] = progress[str(p["id"])]
    questions = [{"id": q["id"], "text": q["text"], "max_score": q["max_score"], "answer": answers.get(q["id"]), "submissions": [{"id": s["id"]} for s in linked_submissions if s["problem_id"] == q["problem_id"]], "problem_id": q["problem_id"], "linked": bool(q["problem_id"])} for q in item.snapshot["questions"]]
    active = not item.finished_at and timezone.now() < item.deadline and item.student_id == request.user.pk
    return render(request, "lab/attempt.html", {"attempt_id": pk, "title": item.snapshot["title"], "instructions": item.snapshot["instructions"], "version": item.version.number, "problems": problems, "questions": questions, "submissions": [public_submission(request.user, s, item, include_result=False) for s in submissions[:50]], "active": active, "can_edit": not item.finished_at and item.student_id == request.user.pk, "clock": clock_data(item)})


@login_required
@require_POST
def save(request, pk):
    try:
        data = json_body(request)
        if data.get("type") == "theory":
            saved = services.save_answer(request.user, pk, data.get("question"), data.get("text", ""), data.get("submission") or None, expected_revision(data))
        elif data.get("type") == "code":
            saved = services.save_draft(request.user, pk, data.get("problem"), data.get("source", ""), expected_revision(data))
        else:
            raise ValueError
    except Attempt.DoesNotExist:
        raise Http404
    except (ValueError, TypeError, RecursionError, UnicodeDecodeError, ValidationError) as exc:
        return api_error(exc)
    return JsonResponse({"saved": True, "revision": saved.revision, **clock_data(owned(request.user, pk))})


@login_required
@require_POST
def submit(request, pk):
    try:
        data = json_body(request)
        if "custom_input" in data and data.get("kind") != "custom":
            raise ValidationError("Свой ввод разрешён только для пользовательского запуска.")
        item = services.submit_code(request.user, pk, data.get("problem"), data.get("source", ""), data.get("kind"), data.get("custom_input"),
                                    request_id=data.get("request_id") or "missing", expected_revision=expected_revision(data))
    except Attempt.DoesNotExist:
        raise Http404
    except (ValueError, TypeError, RecursionError, UnicodeDecodeError, ValidationError) as exc:
        return api_error(exc)
    return JsonResponse({"submission": public_submission(request.user, item, item.attempt), **clock_data(item.attempt)}, status=201)


@login_required
@require_POST
def finish(request, pk):
    try:
        data = json_body(request)
        if data.get("confirm") is not True:
            raise ValidationError("Подтвердите завершение работы.")
        # Normalize absent/null tokens so an old browser cannot bypass conflict checks.
        drafts, answers = data.get("drafts"), data.get("answers")
        if isinstance(drafts, list) and isinstance(answers, list):
            for field in drafts + answers:
                if isinstance(field, dict):
                    field["expected_revision"] = expected_revision(field)
        item = services.finish_attempt(request.user, pk, drafts, answers, check_revisions=True)
    except Attempt.DoesNotExist:
        raise Http404
    except (ValueError, TypeError, AttributeError, RecursionError, UnicodeDecodeError, ValidationError) as exc:
        return api_error(exc)
    return JsonResponse({"finished": True, "url": f"/attempts/{pk}/results/", **clock_data(item)})


@login_required
def state(request, pk):
    item = owned(request.user, pk)
    return JsonResponse({**clock_data(item), "task_progress": task_progress(request.user, item), "submissions": [public_submission(request.user, s, item, include_result=False) for s in item.submissions.only(*SUBMISSION_META).order_by("-pk")[:50]]})


@login_required
def history(request, pk):
    item = owned(request.user, pk)
    problem = request.GET.get("problem", "")
    items = item.submissions.only(*SUBMISSION_META).order_by("-pk")
    if problem:
        try:
            services.problem_for(item, problem)
        except ValidationError:
            raise Http404
        items = items.filter(problem_id=problem)
    page = Paginator(items, 50).get_page(request.GET.get("page"))
    return render(request, "lab/history.html", {"attempt_id": pk, "page": page, "problem": problem,
        "items": [public_submission(request.user, s, item, include_result=False) for s in page],
        "problems": [{"id": p["id"], "title": p["title"]} for p in item.snapshot["problems"]]})


@login_required
def submission_state(request, pk):
    item = get_object_or_404(Submission, pk=pk)
    attempt = owned(request.user, item.attempt_id)
    return JsonResponse({"submission": public_submission(request.user, item, attempt)})


@login_required
def submission(request, pk):
    item = get_object_or_404(Submission.objects.select_related("attempt__version"), pk=pk)
    attempt = owned(request.user, item.attempt_id)
    return render(request, "lab/submission.html", {"item": public_submission(request.user, item, attempt), "source": item.source, "attempt_id": attempt.pk, "pending": item.status in ("queued", "running") and (item.kind in ("sample", "custom") or services.results_visible(request.user, attempt))})


@login_required
def results(request, pk):
    item = owned(request.user, pk)
    visible = services.results_visible(request.user, item)
    return render(request, "lab/results.html", {"attempt_id": pk, "title": item.snapshot["title"], "version": item.version.number, "visible": visible, "reveal_at": parse_datetime(item.snapshot["closes_at"]), "summary": services.result_summary(item) if visible else None})
