import math
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from .validators import validate_utf8_size


class Problem(models.Model):
    class Format(models.TextChoices):
        PLAIN = "plain", "Обычный текст"
        MARKDOWN = "markdown", "Markdown"

    title = models.CharField("Название", max_length=200)
    statement = models.TextField("Условие")
    statement_format = models.CharField("Формат условия", max_length=8, choices=Format, default=Format.PLAIN)
    input_description = models.TextField("Входные данные", blank=True)
    output_description = models.TextField("Выходные данные", blank=True)
    constraints = models.TextField("Ограничения", blank=True)
    starter_code = models.TextField("Стартовый код C++17", blank=True, max_length=65536)
    reference_source = models.TextField("Эталонное решение C++17", blank=True, validators=[validate_utf8_size],
        help_text="После сохранения запустите проверку эталона. При заполненном эталоне публикация требует успешной проверки текущих тестов и лимитов.")
    time_limit = models.FloatField("Лимит секунд", default=2, validators=[MinValueValidator(.1), MaxValueValidator(10)])
    memory_mb = models.PositiveIntegerField("Память МБ", default=128, validators=[MinValueValidator(32), MaxValueValidator(512)])

    def clean(self):
        if self.time_limit is not None and not math.isfinite(self.time_limit):
            raise ValidationError({"time_limit": "Лимит должен быть конечным числом."})

    def __str__(self):
        return self.title


class TestCase(models.Model):
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name="tests")
    label = models.CharField("Название", max_length=100)
    input = models.TextField("Ввод", blank=True, max_length=65536, validators=[validate_utf8_size])
    expected = models.TextField("Ожидаемый вывод", blank=True, max_length=65536, validators=[validate_utf8_size])
    is_sample = models.BooleanField("Открытый пример", default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return self.label


class Assessment(models.Model):
    class Kind(models.TextChoices):
        LAB = "lab", "Лабораторная"
        EXAM = "exam", "Зачёт"

    title = models.CharField("Название", max_length=200)
    kind = models.CharField("Тип", choices=Kind, default=Kind.LAB, max_length=8)
    instructions = models.TextField("Инструкция", blank=True)
    groups = models.ManyToManyField(Group, verbose_name="Учебные группы")
    problems = models.ManyToManyField(Problem, through="AssessmentProblem", verbose_name="Задачи")
    opens_at = models.DateTimeField("Открытие")
    closes_at = models.DateTimeField("Закрытие")
    duration_minutes = models.PositiveIntegerField("Продолжительность, минут", null=True, blank=True, validators=[MinValueValidator(1)])
    pass_score = models.PositiveIntegerField("Проходной балл", default=1)
    submission_limit = models.PositiveIntegerField("Лимит оцениваемых отправок на задачу", null=True, blank=True, validators=[MinValueValidator(1)])
    reveal_after_close = models.BooleanField("Раскрывать оценки после закрытия", default=False)
    current_version = models.ForeignKey("AssessmentVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+", editable=False)

    def clean(self):
        if self.opens_at and self.closes_at and self.closes_at <= self.opens_at:
            raise ValidationError("Закрытие должно быть позже открытия.")
        if self.kind == self.Kind.EXAM and not self.duration_minutes:
            raise ValidationError("Укажите продолжительность зачёта.")

    def __str__(self):
        return self.title


class AssessmentProblem(models.Model):
    assessment = models.ForeignKey(Assessment, on_delete=models.CASCADE, related_name="problem_rules")
    problem = models.ForeignKey(Problem, on_delete=models.PROTECT, verbose_name="Задача")
    max_score = models.PositiveIntegerField("Максимум баллов", default=10, validators=[MinValueValidator(1)])
    required = models.BooleanField("Обязательная", default=True)
    order = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        ordering = ["order", "pk"]
        constraints = [models.UniqueConstraint(fields=["assessment", "problem"], name="unique_assessment_problem")]


class TheoryQuestion(models.Model):
    assessment = models.ForeignKey(Assessment, on_delete=models.CASCADE, related_name="questions")
    problem = models.ForeignKey(Problem, null=True, blank=True, on_delete=models.PROTECT, verbose_name="Связанная задача")
    text = models.TextField("Вопрос")
    max_score = models.PositiveIntegerField("Максимум баллов", default=5, validators=[MinValueValidator(1)])
    source = models.CharField("Источник", max_length=100, default="teacher")

    def clean(self):
        if self.problem_id and self.assessment_id and not self.assessment.problems.filter(pk=self.problem_id).exists():
            raise ValidationError("Связанная задача должна входить в работу. Сначала сохраните состав задач.")

    def __str__(self):
        return self.text[:80]


class ImmutableVersionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Опубликованная версия неизменяема.")

    def delete(self):
        raise ValidationError("Опубликованную версию нельзя удалить.")

    def bulk_create(self, objs, *args, **kwargs):
        if kwargs.get("update_conflicts"):
            raise ValidationError("Опубликованная версия неизменяема.")
        return super().bulk_create(objs, *args, **kwargs)


class AssessmentVersion(models.Model):
    objects = ImmutableVersionQuerySet.as_manager()
    assessment = models.ForeignKey(Assessment, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    # Private immutable document. Never serialize it into student responses.
    snapshot = models.JSONField()
    published_at = models.DateTimeField(auto_now_add=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["assessment", "number"], name="unique_assessment_version")]

    def save(self, *args, **kwargs):
        if not self._state.adding or (self.pk is not None and type(self).objects.filter(pk=self.pk).exists()):
            raise ValidationError("Опубликованная версия неизменяема. Опубликуйте новую версию работы.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Опубликованную версию нельзя удалить.")

    def __str__(self):
        return f"{self.snapshot['title']} · версия {self.number}"


class Attempt(models.Model):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    assessment = models.ForeignKey(Assessment, on_delete=models.PROTECT, related_name="attempts")
    version = models.ForeignKey(AssessmentVersion, on_delete=models.PROTECT)
    started_at = models.DateTimeField()
    deadline = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "assessment"], name="one_attempt_per_student")]

    @property
    def snapshot(self):
        return self.version.snapshot

    def __str__(self):
        return f"{self.student} — {self.version}"


class DeadlineExtension(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.PROTECT, related_name="extensions")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    old_deadline = models.DateTimeField()
    new_deadline = models.DateTimeField()
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class Draft(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE)
    # IDs refer to the frozen document, not to editable source tasks/questions.
    problem_id = models.PositiveBigIntegerField()
    source = models.TextField(max_length=65536, blank=True)
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["attempt", "problem_id"], name="one_draft_per_problem")]


class Submission(models.Model):
    class Kind(models.TextChoices):
        SAMPLE = "sample", "Примеры"
        JUDGE = "judge", "На проверку"
        CUSTOM = "custom", "Свой ввод"

    class Status(models.TextChoices):
        QUEUED = "queued", "В очереди"
        RUNNING = "running", "Выполняется"
        ACCEPTED = "accepted", "Принято"
        WRONG = "wrong_answer", "Неверный ответ"
        COMPILE = "compile_error", "Ошибка компиляции"
        RUNTIME = "runtime_error", "Ошибка выполнения"
        LIMIT = "limit_exceeded", "Превышение лимита"
        INFRA = "infra_error", "Ошибка инфраструктуры"

    attempt = models.ForeignKey(Attempt, on_delete=models.PROTECT, related_name="submissions")
    request_id = models.UUIDField(null=True, blank=True, editable=False)
    problem_id = models.PositiveBigIntegerField()
    source = models.TextField(max_length=65536)
    custom_input = models.TextField(blank=True, max_length=65536)
    kind = models.CharField(choices=Kind, max_length=8)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(choices=Status, max_length=20, default=Status.QUEUED, db_index=True)
    result = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["attempt", "request_id"], name="unique_submission_request")]

    def __str__(self):
        return f"#{self.pk} {self.attempt.student} ({self.get_status_display()})"


class WorkerState(models.Model):
    # A single row, updated only by the process holding WORKER_LOCK.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    run_id = models.CharField(max_length=32)
    status = models.CharField(max_length=16, default="starting")
    heartbeat_at = models.DateTimeField()
    current_submission = models.ForeignKey(Submission, null=True, blank=True, on_delete=models.SET_NULL)
    message = models.CharField(max_length=300, blank=True)


class TheoryAnswer(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.PROTECT, related_name="answers")
    question_id = models.PositiveBigIntegerField()
    text = models.TextField("Ответ", blank=True, max_length=20000)
    revision = models.PositiveIntegerField(default=0)
    submission = models.ForeignKey(Submission, null=True, blank=True, on_delete=models.PROTECT, verbose_name="Версия кода")
    updated_at = models.DateTimeField(auto_now=True)
    score = models.PositiveIntegerField("Утверждённые баллы", null=True, blank=True)
    feedback = models.TextField("Комментарий преподавателя", blank=True)
    proposed_score = models.PositiveIntegerField("Предложенные баллы", null=True, blank=True, editable=False)
    proposed_feedback = models.TextField(blank=True, editable=False)
    graded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    graded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["attempt", "question_id"], name="one_answer_per_question")]

    def clean(self):
        if not self.attempt_id:
            return
        question = next((q for q in self.attempt.snapshot["questions"] if q["id"] == self.question_id), None)
        if not question:
            raise ValidationError("Вопрос не принадлежит этой работе.")
        if self.score is not None and self.score > question["max_score"]:
            raise ValidationError({"score": "Балл выше максимума."})
        if self.submission_id and (self.submission.attempt_id != self.attempt_id or self.submission.kind != "judge" or self.submission.problem_id != question["problem_id"]):
            raise ValidationError("Выберите оцениваемую отправку по связанной задаче из этой попытки.")

    def __str__(self):
        return f"{self.attempt} · вопрос {self.question_id}"


class GradeChange(models.Model):
    answer = models.ForeignKey(TheoryAnswer, on_delete=models.PROTECT, related_name="grade_changes")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=20, choices=[("graded", "Оценка преподавателем"), ("answer_changed", "Изменение ответа"), ("baseline", "Оценка до введения журнала")])
    old_score = models.PositiveIntegerField(null=True)
    new_score = models.PositiveIntegerField(null=True)
    old_feedback = models.TextField(blank=True)
    new_feedback = models.TextField(blank=True)
    answer_revision = models.PositiveIntegerField()
    answer_text = models.TextField(blank=True)
    submission = models.ForeignKey(Submission, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ["-created_at", "-pk"]


class LoginRateBucket(models.Model):
    # HMAC keys avoid storing usernames/IP addresses in this operational table.
    key = models.CharField(max_length=64, primary_key=True)
    attempts = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)


class ReferenceCheck(models.Model):
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name="reference_checks")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64)
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=Submission.Status, default=Submission.Status.QUEUED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    result = models.JSONField(default=dict)
