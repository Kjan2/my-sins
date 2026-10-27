from datetime import timedelta
from uuid import UUID
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import Assessment, AssessmentVersion, Attempt, DeadlineExtension, Draft, Submission, TheoryAnswer, GradeChange
from .validators import validate_utf8_size


class Closed(ValidationError):
    pass


class Conflict(ValidationError):
    def __init__(self, field):
        self.field = field
        super().__init__("Текст изменён в другой вкладке. Сравните версии перед сохранением.")


def check_revision(expected, current, field, unchanged):
    # Internal teacher/management calls may omit the token; HTTP callers must supply it.
    if expected is None:
        return
    if type(expected) is not int or expected < 0:
        raise ValidationError("Обновите страницу: отсутствует корректная версия сохраняемого текста.")
    if expected != current and not unchanged:
        raise Conflict(field)


def draft_state(draft, problem_id):
    return {"type": "code", "problem": problem_id, "source": draft.source if draft else "",
            "revision": draft.revision if draft else 0}


def answer_state(answer, question_id):
    return {"type": "theory", "question": question_id, "text": answer.text if answer else "",
            "submission": answer.submission_id if answer else None, "revision": answer.revision if answer else 0}


def teacher(user):
    if not user.is_active or not user.is_staff:
        raise PermissionDenied


def enrolled(user, snapshot):
    return user.is_authenticated and user.is_active and (user.is_staff or user.groups.filter(pk__in=snapshot["group_ids"]).exists())


def accessible_attempt(user, pk):
    attempt = Attempt.objects.select_related("version", "assessment", "student").get(pk=pk)
    if attempt.student_id != user.pk and not user.is_staff:
        raise PermissionDenied
    return attempt


def mutable_attempt(user, pk):
    attempt = accessible_attempt(user, pk)
    if attempt.student_id != user.pk:
        raise PermissionDenied
    if attempt.finished_at or timezone.now() >= attempt.deadline:
        raise Closed("Время работы истекло или работа уже завершена.")
    return attempt


def make_snapshot(assessment):
    assessment.full_clean()
    problems = []
    for rule in assessment.problem_rules.select_related("problem").prefetch_related("problem__tests"):
        rule.full_clean()
        p = rule.problem
        p.full_clean()
        tests = []
        for t in p.tests.all():
            t.full_clean()
            tests.append({"label": t.label, "input": t.input, "expected": t.expected, "is_sample": t.is_sample})
        if len(tests) > 100:
            raise ValidationError(f"Задача «{p.title}»: допускается не больше 100 тестов.")
        if not any(not t["is_sample"] for t in tests):
            raise ValidationError(f"Задача «{p.title}»: добавьте хотя бы один скрытый тест с вводом и ожидаемым ответом в карточке задачи.")
        if p.reference_source:
            from .reference_checks import reference_fingerprint, reference_payload
            fingerprint = reference_fingerprint(reference_payload(p, tests))
            check = p.reference_checks.filter(fingerprint=fingerprint).order_by("-pk").first()
            if not check or check.status != "accepted":
                raise ValidationError(f"Задача «{p.title}»: сначала успешно проверьте эталон на текущих тестах и лимитах.")
        validate_source(p.starter_code)
        problems.append({"id": p.pk, "title": p.title, "statement": p.statement,
                         "statement_format": p.statement_format, "input_description": p.input_description,
                         "output_description": p.output_description, "constraints": p.constraints,
                         "starter_code": p.starter_code, "time_limit": p.time_limit,
                         "memory_mb": p.memory_mb, "max_score": rule.max_score, "required": rule.required, "tests": tests})
    if not problems or not assessment.groups.exists():
        raise ValidationError("Добавьте задачи и учебные группы.")
    questions = []
    for q in assessment.questions.order_by("pk"):
        q.full_clean()
        questions.append({"id": q.pk, "text": q.text, "problem_id": q.problem_id, "max_score": q.max_score, "source": q.source})
    total = sum(p["max_score"] for p in problems) + sum(q["max_score"] for q in questions)
    if assessment.pass_score > total:
        raise ValidationError("Проходной балл больше суммы максимальных баллов.")
    return {"problems": problems, "questions": questions, "title": assessment.title, "kind": assessment.kind,
            "instructions": assessment.instructions, "group_ids": list(assessment.groups.values_list("pk", flat=True)),
            "opens_at": assessment.opens_at.isoformat(), "closes_at": assessment.closes_at.isoformat(),
            "duration_minutes": assessment.duration_minutes, "pass_score": assessment.pass_score,
            "submission_limit": assessment.submission_limit, "reveal_after_close": assessment.reveal_after_close}


@transaction.atomic
def publish_assessment(actor, assessment_id):
    teacher(actor)
    assessment = Assessment.objects.get(pk=assessment_id)
    snapshot = make_snapshot(assessment)
    last = assessment.versions.order_by("-number").first()
    version = AssessmentVersion.objects.create(assessment=assessment, number=last.number + 1 if last else 1, snapshot=snapshot, published_by=actor)
    assessment.current_version = version
    assessment.save(update_fields=["current_version"])
    return version


@transaction.atomic
def start_attempt(user, assessment_id):
    # An existing attempt stays accessible even when a new version has new groups or dates.
    old = Attempt.objects.select_related("version").filter(student=user, assessment_id=assessment_id).first()
    if old:
        return old
    assessment = Assessment.objects.select_related("current_version").get(pk=assessment_id, current_version__isnull=False)
    snapshot = assessment.current_version.snapshot
    if not enrolled(user, snapshot):
        raise PermissionDenied
    now = timezone.now()
    closes = parse_datetime(snapshot["closes_at"])
    if not parse_datetime(snapshot["opens_at"]) <= now < closes:
        raise Closed("Работа сейчас недоступна.")
    deadline = closes
    if snapshot["duration_minutes"]:
        deadline = min(deadline, now + timedelta(minutes=snapshot["duration_minutes"]))
    attempt = Attempt.objects.create(student=user, assessment=assessment, version=assessment.current_version, started_at=now, deadline=deadline)
    # Empty answers also need an explicit manual grade when the attempt closes.
    TheoryAnswer.objects.bulk_create([TheoryAnswer(attempt=attempt, question_id=q["id"]) for q in snapshot["questions"]])
    return attempt


def frozen_item(attempt, collection, item_id):
    try:
        if isinstance(item_id, bool):
            raise ValueError
        return next(p for p in attempt.snapshot[collection] if p["id"] == int(item_id))
    except (StopIteration, ValueError, TypeError, OverflowError):
        raise ValidationError("Задача или вопрос не входит в работу.")


def problem_for(attempt, problem_id):
    return frozen_item(attempt, "problems", problem_id)


def validate_source(source, label="Код"):
    try:
        validate_utf8_size(source)
    except ValidationError:
        raise ValidationError(f"{label} должен быть текстом UTF-8 размером не больше 64 КиБ.")


@transaction.atomic
def save_draft(user, attempt_id, problem_id, source, expected_revision=None):
    attempt = mutable_attempt(user, attempt_id)
    problem = problem_for(attempt, problem_id)
    validate_source(source)
    draft = Draft.objects.filter(attempt=attempt, problem_id=problem["id"]).first()
    unchanged = draft is not None and draft.source == source
    check_revision(expected_revision, draft.revision if draft else 0, draft_state(draft, problem["id"]), unchanged)
    mutable_attempt(user, attempt_id)
    if draft is None:
        return Draft.objects.create(attempt=attempt, problem_id=problem["id"], source=source)
    if not unchanged:
        draft.source = source
        draft.revision += 1
        draft.save(update_fields=["source", "revision", "updated_at"])
    return draft


@transaction.atomic
def submit_code(user, attempt_id, problem_id, source, kind, custom_input=None, *, request_id=None, expected_revision=None):
    attempt = accessible_attempt(user, attempt_id)
    if attempt.student_id != user.pk:
        raise PermissionDenied
    problem = problem_for(attempt, problem_id)
    if request_id is not None:
        try:
            request_id = UUID(str(request_id))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError("Некорректный идентификатор отправки. Обновите страницу.")
        previous = attempt.submissions.filter(request_id=request_id).first()
        if previous:
            if (previous.problem_id != problem["id"] or previous.source != source or previous.kind != kind or
                    previous.custom_input != (custom_input or "")):
                raise ValidationError("Идентификатор уже использован для другой отправки.")
            return previous
    attempt = mutable_attempt(user, attempt_id)
    validate_source(source)
    if kind not in Submission.Kind.values:
        raise ValidationError("Неизвестный тип запуска.")
    if kind == "custom":
        custom_input = "" if custom_input is None else custom_input
        validate_source(custom_input, label="Ввод")
    elif custom_input is not None:
        raise ValidationError("Свой ввод разрешён только для пользовательского запуска.")
    if not source.strip():
        raise ValidationError("Введите код.")
    if kind == "sample" and not any(t["is_sample"] for t in problem["tests"]):
        raise ValidationError("Открытых примеров нет.")
    if Submission.objects.filter(attempt__student=user, status__in=["queued", "running"]).count() >= 3:
        raise ValidationError("У вас уже три задания в очереди. Дождитесь результата.")
    limit = attempt.snapshot["submission_limit"]
    if kind == "judge" and limit and attempt.submissions.filter(problem_id=problem["id"], kind="judge").count() >= limit:
        raise ValidationError("Достигнут лимит оцениваемых отправок по задаче.")
    save_draft(user, attempt_id, problem["id"], source, expected_revision)
    accepted_at = timezone.now()
    if accepted_at >= attempt.deadline:
        raise Closed("Время работы истекло.")
    return Submission.objects.create(attempt=attempt, problem_id=problem["id"], source=source, kind=kind,
                                     custom_input=custom_input or "", created_at=accepted_at, request_id=request_id)


@transaction.atomic
def save_answer(user, attempt_id, question_id, text, submission_id=None, expected_revision=None):
    attempt = mutable_attempt(user, attempt_id)
    question = frozen_item(attempt, "questions", question_id)
    if not isinstance(text, str) or len(text) > 20000:
        raise ValidationError("Ответ не должен превышать 20 000 символов.")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError("Ответ должен быть текстом UTF-8.")
    submission = None
    if submission_id:
        try:
            submission = Submission.objects.filter(pk=int(submission_id), attempt=attempt, kind="judge", problem_id=question["problem_id"]).first()
        except (ValueError, TypeError, OverflowError):
            pass
        if not submission:
            raise ValidationError("Выберите свою оцениваемую отправку по связанной задаче.")
    mutable_attempt(user, attempt_id)
    answer, _ = TheoryAnswer.objects.get_or_create(attempt=attempt, question_id=question["id"])
    unchanged = answer.text == text and answer.submission_id == (submission.pk if submission else None)
    check_revision(expected_revision, answer.revision, answer_state(answer, question["id"]), unchanged)
    if not unchanged:
        if answer.score is not None or answer.feedback:
            record_grade_change(answer, user, None, "", "answer_changed")
        answer.text, answer.submission = text, submission
        answer.revision += 1
        answer.score, answer.feedback, answer.graded_by, answer.graded_at = None, "", None, None
        answer.proposed_score, answer.proposed_feedback = None, ""
        answer.save()
    return answer


@transaction.atomic
def finish_attempt(user, attempt_id, drafts, answers, *, check_revisions=False):
    previous = accessible_attempt(user, attempt_id)
    if previous.student_id != user.pk:
        raise PermissionDenied
    if previous.finished_at:
        return previous
    attempt = mutable_attempt(user, attempt_id)
    # The finish request carries all fields. An invalid field rolls back the whole request.
    if not isinstance(drafts, list) or not isinstance(answers, list) or not all(isinstance(d, dict) for d in drafts + answers):
        raise ValidationError("Перед завершением сохраните код и ответы.")
    if len(drafts) != len(attempt.snapshot["problems"]) or {str(d.get("problem")) for d in drafts} != {str(p["id"]) for p in attempt.snapshot["problems"]}:
        raise ValidationError("Переданы не все черновики.")
    if len(answers) != len(attempt.snapshot["questions"]) or {str(a.get("question")) for a in answers} != {str(q["id"]) for q in attempt.snapshot["questions"]}:
        raise ValidationError("Переданы не все ответы.")
    for d in drafts:
        save_draft(user, attempt_id, d.get("problem"), d.get("source", ""), d.get("expected_revision", "missing") if check_revisions else None)
    for a in answers:
        save_answer(user, attempt_id, a.get("question"), a.get("text", ""), a.get("submission"), a.get("expected_revision", "missing") if check_revisions else None)
    # Re-check after saving; crossing the deadline rolls back these writes too.
    attempt = mutable_attempt(user, attempt_id)
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["finished_at"])
    return attempt


@transaction.atomic
def extend_deadline(actor, attempt_id, new_deadline, reason):
    teacher(actor)
    attempt = Attempt.objects.get(pk=attempt_id)
    if attempt.finished_at:
        raise ValidationError("Нельзя продлить досрочно завершенную работу.")
    if new_deadline <= attempt.deadline or new_deadline <= timezone.now() or not reason.strip():
        raise ValidationError("Укажите будущий срок позже текущего и причину.")
    DeadlineExtension.objects.create(attempt=attempt, actor=actor, old_deadline=attempt.deadline, new_deadline=new_deadline, reason=reason.strip())
    attempt.deadline = new_deadline
    attempt.save(update_fields=["deadline"])
    return attempt


@transaction.atomic
def grade_answer(actor, answer_id, score, feedback, expected_updated_at=None):
    teacher(actor)
    answer = TheoryAnswer.objects.select_related("attempt__version").get(pk=answer_id)
    if expected_updated_at and answer.updated_at != expected_updated_at:
        raise ValidationError("Студент изменил ответ. Обновите страницу перед оценкой.")
    previous_score, previous_feedback = answer.score, answer.feedback
    answer.score, answer.feedback = score, feedback
    answer.graded_by = actor if score is not None else None
    answer.graded_at = timezone.now() if score is not None else None
    answer.full_clean()
    if (previous_score, previous_feedback) != (score, feedback):
        answer.score, answer.feedback = previous_score, previous_feedback
        record_grade_change(answer, actor, score, feedback, "graded")
        answer.score, answer.feedback = score, feedback
    answer.save()
    return answer


def record_grade_change(answer, actor, new_score, new_feedback, reason):
    return GradeChange.objects.create(answer=answer, actor=actor, reason=reason,
        old_score=answer.score, new_score=new_score, old_feedback=answer.feedback, new_feedback=new_feedback,
        answer_revision=answer.revision, answer_text=answer.text, submission_id=answer.submission_id)


def results_visible(user, attempt, now=None):
    return user.is_staff or not attempt.snapshot["reveal_after_close"] or (now or timezone.now()) >= parse_datetime(attempt.snapshot["closes_at"])


def result_summary(attempt, *, submissions=None, answers=None):
    submissions = list(attempt.submissions.filter(kind="judge").only("problem_id", "status")) if submissions is None else [s for s in submissions if s.kind == "judge"]
    answers = {a.question_id: a for a in (attempt.answers.all() if answers is None else answers)}
    problems = []
    required_ok = True
    for p in attempt.snapshot["problems"]:
        accepted = any(s.problem_id == p["id"] and s.status == "accepted" for s in submissions)
        problems.append({"id": p["id"], "title": p["title"], "score": p["max_score"] if accepted else 0, "max_score": p["max_score"], "required": p["required"], "accepted": accepted})
        if p["required"] and not accepted:
            required_ok = False
    theory = [{"text": q["text"], "score": answers[q["id"]].score if q["id"] in answers else None, "feedback": answers[q["id"]].feedback if q["id"] in answers else "", "max_score": q["max_score"]} for q in attempt.snapshot["questions"]]
    score = sum(p["score"] for p in problems) + sum(q["score"] or 0 for q in theory)
    pending = any(s.status in ("queued", "running", "infra_error") for s in submissions) or any(q["score"] is None for q in theory)
    active = not attempt.finished_at and timezone.now() < attempt.deadline
    return {"problems": problems, "theory": theory, "score": score, "maximum": sum(p["max_score"] for p in problems) + sum(q["max_score"] for q in theory), "provisional": pending or active, "passed": score >= attempt.snapshot["pass_score"] and required_ok, "pass_score": attempt.snapshot["pass_score"], "required_ok": required_ok}


@transaction.atomic
def claim_submission():
    item = Submission.objects.select_related("attempt__version").filter(status="queued").order_by("created_at", "pk").first()
    if item:
        item.status, item.started_at = "running", timezone.now()
        item.save(update_fields=["status", "started_at"])
    return item


@transaction.atomic
def requeue_submission(actor, submission_id):
    teacher(actor)
    item = Submission.objects.get(pk=submission_id)
    if item.status != "infra_error":
        raise ValidationError("Повторная проверка доступна только после ошибки инфраструктуры.")
    if Submission.objects.filter(attempt__student_id=item.attempt.student_id, status__in=["queued", "running"]).count() >= 3:
        raise ValidationError("Очередь студента заполнена.")
    item.status, item.started_at, item.finished_at, item.result = "queued", None, None, {}
    item.save(update_fields=["status", "started_at", "finished_at", "result"])
    return item
