from functools import wraps
import csv
import io
from django.conf import settings
from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from . import services
from .models import Assessment, Attempt, Submission, TheoryAnswer, WorkerState


def teacher_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        services.teacher(request.user)
        return view(request, *args, **kwargs)
    return wrapped


class GradeForm(forms.Form):
    score = forms.IntegerField(label="Баллы", min_value=0, required=False)
    feedback = forms.CharField(label="Комментарий", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    expected_updated_at = forms.DateTimeField(widget=forms.HiddenInput)

    def __init__(self, *args, maximum, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["score"].max_value = maximum
        self.fields["score"].widget.attrs["max"] = maximum
        self.fields["score"].label = f"Баллы (0–{maximum}; пусто — снять оценку)"

    def clean_score(self):
        value = self.cleaned_data["score"]
        if value is not None and value > self.fields["score"].max_value:
            raise ValidationError("Балл выше максимума.")
        return value


class ExtensionForm(forms.Form):
    deadline = forms.DateTimeField(label="Новый срок (МСК)", widget=forms.DateTimeInput(
        format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}))
    reason = forms.CharField(label="Причина продления", widget=forms.Textarea(attrs={"rows": 2}), max_length=2000)


class StudentImportForm(forms.Form):
    file = forms.FileField(label="CSV UTF-8", help_text="До 200 новых студентов, 1 МиБ; разделитель — запятая.")


@teacher_required
def import_students(request):
    from .student_import import import_students as save_students
    form = StudentImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            count = save_students(request.user, form.cleaned_data["file"].read(1048577))
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f"Создано студентов: {count}. Группы назначены.")
            return redirect("teacher_dashboard")
    return render(request, "teacher/import_students.html", {"form": form},
                  status=400 if request.method == "POST" else 200)


def csv_cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value


def gradebook_csv(work, version, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([csv_cell(value) for value in ["Логин", "Фамилия", "Имя", "Состояние", "Версия"] +
                    ([p["title"] for p in version.snapshot["problems"]] if version else []) +
                    ["Теория к проверке", "Баллы", "Максимум", "Итог"]])
    for row in rows:
        student, summary = row["student"], row.get("summary")
        values = [student.username, student.last_name, student.first_name, row["status"], version.number]
        values += ([p["score"] for p in summary["problems"]] + [row["ungraded"], summary["score"], summary["maximum"],
                   "Предварительный" if summary["provisional"] else "Зачтено" if summary["passed"] else "Не зачтено"]) if summary else [""] * (len(version.snapshot["problems"]) + 4)
        writer.writerow([csv_cell(value) for value in values])
    response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="gradebook-{work.pk}.csv"'
    return response


@teacher_required
def dashboard(request):
    works = Assessment.objects.select_related("current_version").annotate(attempt_count=Count("attempts")).order_by("-pk")
    return render(request, "teacher/dashboard.html", {"works": works})


@teacher_required
def gradebook(request, pk):
    work = get_object_or_404(Assessment.objects.select_related("current_version"), pk=pk)
    versions = list(work.versions.order_by("-number"))
    selected = request.GET.get("version")
    version = next((v for v in versions if str(v.pk) == selected), None) if selected else work.current_version
    if selected and version is None:
        raise Http404
    group_id = request.GET.get("group", "")
    if group_id and (not group_id.isdecimal() or len(group_id) > 19 or
                     not 0 < int(group_id) <= 9223372036854775807 or
                     not Group.objects.filter(pk=group_id).exists()):
        raise Http404
    rows = []
    if version:
        attempts = Attempt.objects.filter(assessment=work, version=version).select_related("student").prefetch_related(
            Prefetch("submissions", queryset=Submission.objects.only("attempt_id", "problem_id", "kind", "status")),
            Prefetch("answers", queryset=TheoryAnswer.objects.only("attempt_id", "question_id", "score", "feedback")))
        if group_id:
            attempts = attempts.filter(student__groups__pk=group_id)
        for attempt in attempts:
            attempt.version = version
            summary = services.result_summary(attempt, submissions=attempt.submissions.all(), answers=attempt.answers.all())
            status = "Завершена" if attempt.finished_at else "Срок истёк" if timezone.now() >= attempt.deadline else "В работе"
            rows.append({"student": attempt.student, "attempt": attempt, "status": status, "summary": summary,
                         "ungraded": sum(q["score"] is None for q in summary["theory"])})
        if version.pk == work.current_version_id:
            students = get_user_model().objects.filter(is_active=True, is_staff=False, groups__pk__in=version.snapshot["group_ids"])
            # One attempt per work: a student taking an old version cannot start the new one.
            students = students.exclude(pk__in=Attempt.objects.filter(assessment=work).values("student_id"))
            if group_id:
                students = students.filter(groups__pk=group_id)
            rows.extend({"student": student, "status": "Не начал"} for student in students.distinct())
        rows.sort(key=lambda row: (row["student"].last_name.casefold(), row["student"].first_name.casefold(), row["student"].username.casefold()))
    if request.GET.get("format") == "csv":
        return gradebook_csv(work, version, rows)
    return render(request, "teacher/gradebook.html", {"work": work, "versions": versions, "version": version,
                  "groups": Group.objects.order_by("name"), "group_id": group_id, "rows": rows})


@teacher_required
def attempt(request, pk):
    item = get_object_or_404(Attempt.objects.select_related("student", "version", "assessment"), pk=pk)
    answers = {a.question_id: a for a in item.answers.select_related("submission", "graded_by").prefetch_related("grade_changes__actor")}
    extension_form = ExtensionForm(request.POST if request.method == "POST" and request.POST.get("action") == "extend" else None)
    error_status = 200
    if request.method == "POST" and request.POST.get("action") == "extend":
        if extension_form.is_valid():
            try:
                services.extend_deadline(request.user, item.pk, extension_form.cleaned_data["deadline"], extension_form.cleaned_data["reason"])
            except ValidationError as exc:
                extension_form.add_error(None, exc)
            else:
                messages.success(request, "Срок продлён; изменение записано в журнал.")
                return redirect("teacher_attempt", pk=pk)
        error_status = 400
    question_rows = []
    for q in item.snapshot["questions"]:
        answer = answers.get(q["id"])
        bound = request.method == "POST" and request.POST.get("action") == "grade" and str(q["id"]) == request.POST.get("question")
        form = GradeForm(request.POST if bound else None, prefix=f"q{q['id']}", maximum=q["max_score"], initial={
            "score": answer.score if answer else None, "feedback": answer.feedback if answer else "",
            "expected_updated_at": answer.updated_at.isoformat() if answer else ""})
        if bound:
            if answer and form.is_valid():
                try:
                    services.grade_answer(request.user, answer.pk, form.cleaned_data["score"], form.cleaned_data["feedback"], form.cleaned_data["expected_updated_at"])
                except ValidationError as exc:
                    form.add_error(None, exc)
                else:
                    messages.success(request, "Оценка сохранена.")
                    return redirect("teacher_attempt", pk=pk)
            error_status = 400
        question_rows.append({"question": q, "answer": answer, "form": form,
                              "grade_changes": answer.grade_changes.all() if answer else []})
    if request.method == "POST" and (request.POST.get("action") not in ("extend", "grade") or
            (request.POST.get("action") == "grade" and request.POST.get("question") not in {str(q["id"]) for q in item.snapshot["questions"]})):
        raise Http404
    return render(request, "teacher/attempt.html", {"item": item, "questions": question_rows,
                  "summary": services.result_summary(item), "extension_form": extension_form,
                  "extensions": item.extensions.select_related("actor").order_by("-created_at"),
                  "submissions": item.submissions.defer("source", "result", "custom_input").order_by("-pk")[:50]}, status=error_status)


def judge_data():
    now = timezone.now()
    worker = WorkerState.objects.first()
    fresh = bool(worker and (now - worker.heartbeat_at).total_seconds() <= 30)
    labels = {"starting": "Запускается", "idle": "Ожидает задания", "running": "Проверяет", "stopped": "Остановлен", "error": "Остановлен с ошибкой"}
    oldest = Submission.objects.filter(status="queued").order_by("created_at").values_list("created_at", flat=True).first()
    errors = Submission.objects.filter(status="infra_error").select_related("attempt__student").defer("source", "result", "custom_input").order_by("-finished_at", "-pk")[:20]
    oldest_seconds = max(0, int((now - oldest).total_seconds())) if oldest else 0
    warnings = []
    if not fresh or worker.status not in ("idle", "running", "starting"):
        warnings.append("Проверяющий процесс недоступен. Проверьте worker.")
    if oldest_seconds >= settings.QUEUE_WARNING_SECONDS:
        warnings.append(f"Очередь задерживается: старейшая отправка ожидает {oldest_seconds} с.")
    return {
        "server_now": now.isoformat(),
        "worker_label": labels.get(worker.status, "Неизвестно") if worker and (fresh or worker.status in ("error", "stopped")) else "Нет свежего сигнала",
        "heartbeat_at": worker.heartbeat_at.isoformat() if worker else None,
        "fresh": fresh, "current_submission": worker.current_submission_id if worker else None,
        "message": worker.message if worker else "Worker ещё не запускался.",
        "queued": Submission.objects.filter(status="queued").count(),
        "running": Submission.objects.filter(status="running").count(),
        "oldest_seconds": oldest_seconds, "warnings": warnings,
        "errors": [{"id": s.pk, "student": s.attempt.student.get_full_name() or s.attempt.student.username,
                    "kind": s.get_kind_display(), "attempt_id": s.attempt_id} for s in errors],
    }


@teacher_required
def judge(request):
    return render(request, "teacher/judge.html", {"state": judge_data()})


@teacher_required
def judge_state(request):
    return JsonResponse(judge_data())


@teacher_required
@require_POST
def requeue(request, pk):
    get_object_or_404(Submission, pk=pk)
    try:
        services.requeue_submission(request.user, pk)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"Отправка #{pk} возвращена в очередь.")
    return redirect("teacher_judge")
