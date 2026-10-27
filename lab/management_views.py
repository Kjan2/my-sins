"""Course-scoped authoring pages in the teacher cabinet."""
from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services
from .models import Assessment, Course, Problem, TheoryQuestion
from .permissions import courses_for, scoped
from .problem_content import problem_sections
from .teacher_forms import AssessmentForm, CourseForm, ProblemForm, QuestionFormSet, RuleFormSet, TestFormSet
from .teacher_views import teacher_required


def selected_course(request):
    courses = courses_for(request.user).order_by("title", "pk")
    selected = request.GET.get("course") or request.POST.get("course")
    if selected:
        if not selected.isdecimal() or len(selected) > 19:
            raise Http404
        return get_object_or_404(courses, pk=selected)
    choices = list(courses[:2])
    return choices[0] if len(choices) == 1 else None


@teacher_required
def courses(request):
    items = courses_for(request.user).annotate(group_count=Count("groups", distinct=True),
        work_count=Count("assessments", distinct=True), problem_count=Count("problems", distinct=True)).order_by("title", "pk")
    return render(request, "teacher/courses.html", {"courses": items})


@teacher_required
def course_edit(request, pk=None):
    course = get_object_or_404(courses_for(request.user), pk=pk) if pk else Course()
    form = CourseForm(request.POST if request.method == "POST" else None, instance=course, user=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            if not pk:
                course.teachers.add(request.user)
        messages.success(request, "Курс сохранён.")
        return redirect("teacher_courses")
    return render(request, "teacher/course_form.html", {"form": form, "course": course},
                  status=400 if form.is_bound else 200)


@teacher_required
def assessment_edit(request, pk=None):
    work = get_object_or_404(scoped(request.user, Assessment.objects.select_related("course", "current_version")), pk=pk) if pk else None
    course = work.course if work else selected_course(request)
    if not course:
        return render(request, "teacher/select_course.html", {"courses": courses_for(request.user), "creating": "работы"})
    work = work or Assessment(course=course, opens_at=timezone.now().replace(second=0, microsecond=0),
                              closes_at=timezone.now().replace(second=0, microsecond=0) + timedelta(days=7))
    data = request.POST if request.method == "POST" else None
    form = AssessmentForm(data, instance=work, course=course)
    rules = RuleFormSet(data, instance=work, prefix="rules", form_kwargs={"course": course})
    existing = {q.pk: q for q in work.questions.all()} if work.pk else {}
    form_valid = form.is_valid() if data is not None else False
    rules_valid = rules.is_valid() if data is not None else False
    if rules_valid:
        problem_ids = {row.cleaned_data["problem"].pk for row in rules.forms
                       if row.cleaned_data.get("problem") and not row.cleaned_data.get("DELETE")}
    else:
        problem_ids = set(work.problems.values_list("pk", flat=True)) if work.pk else set()
    questions = QuestionFormSet(data, prefix="questions", form_kwargs={"course": course},
        initial=[{"question_id": q.pk, "text": q.text, "max_score": q.max_score, "problem": q.problem_id} for q in existing.values()],
        existing_ids=existing, problem_ids=problem_ids)
    questions_valid = questions.is_valid() if data is not None else False
    if form_valid and rules_valid and questions_valid:
        with transaction.atomic():
            form.save()
            rules.save()
            kept = []
            for row in questions.forms:
                values = row.cleaned_data
                if not values or values.get("DELETE"):
                    continue
                question = existing.get(values.get("question_id")) or TheoryQuestion(assessment=work)
                question.text, question.max_score, question.problem = values["text"], values["max_score"], values["problem"]
                question.full_clean()
                question.save()
                kept.append(question.pk)
            work.questions.exclude(pk__in=kept).delete()
            if request.POST.get("action") == "publish":
                try:
                    version = services.publish_assessment(request.user, work.pk)
                except ValidationError as exc:
                    messages.error(request, "Изменения сохранены. Публикация не выполнена: " + " ".join(exc.messages))
                else:
                    messages.success(request, f"Опубликована версия {version.number}. Работа доступна назначенным группам в указанные сроки.")
            else:
                messages.success(request, "Изменения сохранены. Для доступа студентов опубликуйте работу.")
        return redirect("teacher_assessment_edit", pk=work.pk)
    readiness = []
    if work.pk and not form.is_bound:
        try:
            services.make_snapshot(work)
        except ValidationError as exc:
            readiness = exc.messages
    return render(request, "teacher/assessment_form.html", {"work": work, "course": course,
        "form": form, "rules": rules, "questions": questions, "readiness": readiness,
        "has_errors": data is not None, "attempt_count": work.attempts.count() if work.pk else 0},
        status=400 if data is not None else 200)


@teacher_required
def problems(request):
    items = scoped(request.user, Problem.objects.all()).select_related("course").annotate(
        sample_count=Count("tests", filter=Q(tests__is_sample=True)), hidden_count=Count("tests", filter=Q(tests__is_sample=False)))
    course = selected_course(request) if request.GET.get("course") else None
    if course:
        items = items.filter(course=course)
    query = request.GET.get("q", "").strip()[:200]
    if query:
        items = items.filter(title__icontains=query)
    return render(request, "teacher/problems.html", {"problems": items.order_by("-pk"),
        "courses": courses_for(request.user), "course_id": str(course.pk) if course else "", "query": query})


@teacher_required
def problem_edit(request, pk=None):
    problem = get_object_or_404(scoped(request.user, Problem.objects.select_related("course")), pk=pk) if pk else None
    course = problem.course if problem else selected_course(request)
    if not course:
        return render(request, "teacher/select_course.html", {"courses": courses_for(request.user), "creating": "задачи"})
    problem = problem or Problem(course=course)
    data = request.POST if request.method == "POST" else None
    form = ProblemForm(data, instance=problem)
    tests = TestFormSet(data, instance=problem, prefix="tests")
    form_valid = form.is_valid() if data is not None else False
    tests_valid = tests.is_valid() if data is not None else False
    if form_valid and tests_valid:
        with transaction.atomic():
            form.save()
            tests.save()
            if request.POST.get("action") == "check":
                from .reference_checks import queue_reference_check
                try:
                    queue_reference_check(request.user, problem.pk)
                except ValidationError as exc:
                    messages.error(request, "Задача сохранена. " + " ".join(exc.messages))
                else:
                    messages.success(request, "Задача сохранена. Эталон отправлен на проверку; обновите страницу для результата.")
            else:
                messages.success(request, "Задача и тесты сохранены.")
        return redirect("teacher_problem_edit", pk=problem.pk)
    reference = None
    if problem.pk and problem.reference_source:
        from .reference_checks import reference_fingerprint, reference_payload
        reference = problem.reference_checks.filter(fingerprint=reference_fingerprint(reference_payload(problem))).order_by("-pk").first()
    return render(request, "teacher/problem_form.html", {"problem": problem, "course": course,
        "form": form, "tests": tests, "reference": reference, "has_errors": data is not None},
        status=400 if data is not None else 200)


@teacher_required
@require_POST
def problem_preview(request):
    problem = {key: request.POST.get(key, "") for key in ("statement", "statement_format", "input_description", "output_description", "constraints")}
    return render(request, "lab/problem_preview.html", {"title": request.POST.get("title", "Предпросмотр"), "sections": problem_sections(problem)})
