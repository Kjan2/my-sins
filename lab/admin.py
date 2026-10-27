from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin, GroupAdmin
from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from . import models, services
from .problem_content import problem_sections

admin.site.site_header = "Кафедра · управление работами"
admin.site.site_title = "Кафедра"


class TeachingAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_module_permission(request)


class ReadOnlyAdmin(TeachingAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class TestInline(admin.TabularInline):
    model = models.TestCase
    extra = 1

    def has_view_permission(self, request, obj=None):
        return request.user.is_staff

    has_change_permission = has_view_permission
    has_delete_permission = has_view_permission
    has_add_permission = has_view_permission


@admin.register(models.Problem)
class ProblemAdmin(TeachingAdmin):
    change_form_template = "admin/lab/problem/change_form.html"
    list_display = ["title", "time_limit", "memory_mb"]
    inlines = [TestInline]
    readonly_fields = ["reference_status"]

    @admin.display(description="Проверка эталона")
    def reference_status(self, obj):
        if not obj or not obj.pk:
            return "Сохраните задачу, тесты и эталонное решение."
        from .reference_checks import reference_fingerprint, reference_payload
        item = obj.reference_checks.filter(fingerprint=reference_fingerprint(reference_payload(obj))).order_by("-pk").first()
        if not item:
            return "Текущие тесты, лимиты и эталон ещё не проверены."
        return format_html('{} · {}<pre>{}</pre>', item.get_status_display(), item.created_at,
                           item.result.get("compiler") or item.result.get("message", ""))

    def response_change(self, request, obj):
        if "_check_reference" in request.POST:
            return self.queue_reference(request, obj)
        return super().response_change(request, obj)

    def response_add(self, request, obj, post_url_continue=None):
        if "_check_reference" in request.POST:
            return self.queue_reference(request, obj)
        return super().response_add(request, obj, post_url_continue)

    def queue_reference(self, request, obj):
        from .reference_checks import queue_reference_check
        try:
            item = queue_reference_check(request.user, obj.pk)
            self.message_user(request, f"Проверка эталона #{item.pk} поставлена в очередь. Обновите страницу после проверки.")
        except ValidationError as exc:
            self.message_user(request, " ".join(exc.messages), messages.ERROR)
        return redirect("admin:lab_problem_change", obj.pk)

    def get_urls(self):
        return [path("preview/", self.admin_site.admin_view(self.preview), name="lab_problem_preview")] + super().get_urls()

    def preview(self, request):
        services.teacher(request.user)
        if request.method != "POST":
            from django.http import HttpResponseNotAllowed
            return HttpResponseNotAllowed(["POST"])
        problem = {key: request.POST.get(key, "") for key in
                   ("statement", "statement_format", "input_description", "output_description", "constraints")}
        return TemplateResponse(request, "lab/problem_preview.html", {
            "title": request.POST.get("title", "Предпросмотр"), "sections": problem_sections(problem)})


class RuleInline(TestInline):
    model = models.AssessmentProblem
    extra = 1


class QuestionInline(TestInline):
    model = models.TheoryQuestion
    extra = 0


@admin.register(models.Assessment)
class AssessmentAdmin(TeachingAdmin):
    change_form_template = "admin/lab/assessment/change_form.html"
    list_display = ["title", "kind", "opens_at", "closes_at", "publication_status"]
    inlines = [RuleInline, QuestionInline]
    filter_horizontal = ["groups"]
    readonly_fields = ["current_version"]
    actions = ["publish"]

    @admin.display(description="Публикация")
    def publication_status(self, obj):
        return f"Опубликована · версия {obj.current_version.number}" if obj.current_version_id else "Черновик · студентам не видна"

    def publish_saved(self, request, obj):
        # response_add/change runs after the task rules and questions are saved.
        try:
            version = services.publish_assessment(request.user, obj.pk)
            self.message_user(request, f"Опубликована версия {version.number}. Работа видна студентам назначенных групп.", messages.SUCCESS)
        except ValidationError as exc:
            self.message_user(request, f"Изменения сохранены, но не опубликованы: {' '.join(exc.messages)}", messages.ERROR)
        return redirect("admin:lab_assessment_change", obj.pk)

    def response_add(self, request, obj, post_url_continue=None):
        if "_publish" in request.POST:
            return self.publish_saved(request, obj)
        return super().response_add(request, obj, post_url_continue)

    def response_change(self, request, obj):
        if "_publish" in request.POST:
            return self.publish_saved(request, obj)
        return super().response_change(request, obj)

    @admin.action(description="Опубликовать новую неизменяемую версию")
    def publish(self, request, queryset):
        for work in queryset:
            try:
                version = services.publish_assessment(request.user, work.pk)
                self.message_user(request, f"Опубликовано: {version}")
            except ValidationError as exc:
                self.message_user(request, f"{work}: {' '.join(exc.messages)}", messages.ERROR)


@admin.register(models.AssessmentVersion)
class VersionAdmin(ReadOnlyAdmin):
    list_display = ["__str__", "published_at", "published_by"]
    readonly_fields = ["assessment", "number", "snapshot", "published_at", "published_by"]


class ExtensionForm(forms.Form):
    new_deadline = forms.DateTimeField(label="Новый срок", help_text="Часовой пояс: Europe/Moscow. Формат: ГГГГ-ММ-ДД ЧЧ:ММ:СС")
    reason = forms.CharField(label="Причина", widget=forms.Textarea)


@admin.register(models.Attempt)
class AttemptAdmin(ReadOnlyAdmin):
    list_display = ["student", "assessment", "version", "started_at", "deadline", "finished_at", "summary", "extend_link"]
    list_filter = ["assessment"]
    readonly_fields = ["student", "assessment", "version", "started_at", "deadline", "finished_at"]

    @admin.display(description="Результат")
    def summary(self, obj):
        summary = services.result_summary(obj)
        label = f"{summary['score']}/{summary['maximum']}" + (" · предварительно" if summary["provisional"] else (" · зачтено" if summary["passed"] else " · не зачтено"))
        return format_html('<a href="{}">{}</a>', reverse("results", args=[obj.pk]), label)

    @admin.display(description="Продление")
    def extend_link(self, obj):
        return format_html('<a href="{}">Продлить время</a>', reverse("admin:lab_attempt_extend", args=[obj.pk]))

    def get_urls(self):
        return [path("<int:pk>/extend/", self.admin_site.admin_view(self.extend_view), name="lab_attempt_extend")] + super().get_urls()

    def extend_view(self, request, pk):
        item = get_object_or_404(models.Attempt, pk=pk)
        form = ExtensionForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                services.extend_deadline(request.user, pk, **form.cleaned_data)
                self.message_user(request, "Срок продлён; причина и автор записаны в журнал.")
                return redirect("admin:lab_attempt_changelist")
            except ValidationError as exc:
                form.add_error(None, exc)
        return TemplateResponse(request, "admin/lab/extend.html", {**self.admin_site.each_context(request), "title": f"Продлить: {item}", "form": form, "item": item})


@admin.register(models.DeadlineExtension)
class ExtensionAdmin(ReadOnlyAdmin):
    list_display = ["attempt", "actor", "old_deadline", "new_deadline", "created_at"]
    readonly_fields = ["attempt", "actor", "old_deadline", "new_deadline", "reason", "created_at"]


@admin.register(models.GradeChange)
class GradeChangeAdmin(ReadOnlyAdmin):
    list_display = ["answer", "actor", "reason", "old_score", "new_score", "created_at"]
    list_filter = ["reason", "created_at"]


@admin.register(models.Submission)
class SubmissionAdmin(ReadOnlyAdmin):
    list_display = ["id", "attempt", "problem_id", "kind", "status", "created_at"]
    list_filter = ["status", "kind", "attempt__assessment"]
    readonly_fields = ["attempt", "problem_id", "source", "kind", "status", "result", "created_at", "started_at", "finished_at"]
    actions = ["requeue"]

    @admin.action(description="Повторить проверку после ошибки инфраструктуры")
    def requeue(self, request, queryset):
        for item in queryset:
            try:
                services.requeue_submission(request.user, item.pk)
            except ValidationError as exc:
                self.message_user(request, f"#{item.pk}: {' '.join(exc.messages)}", messages.ERROR)

    # Read-only model still needs permission for this explicit action.
    requeue.allowed_permissions = ["view"]


class AnswerForm(forms.ModelForm):
    expected_updated_at = forms.DateTimeField(widget=forms.HiddenInput)

    class Meta:
        model = models.TheoryAnswer
        fields = ["score", "feedback"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["expected_updated_at"] = self.instance.updated_at.isoformat()

    def clean(self):
        data = super().clean()
        if self.instance.pk:
            # Admin's POST change form is already in an IMMEDIATE transaction.
            current = models.TheoryAnswer.objects.get(pk=self.instance.pk)
            if data.get("expected_updated_at") != current.updated_at:
                raise ValidationError("Ответ изменён после открытия формы. Обновите страницу.")
        return data


@admin.register(models.TheoryAnswer)
class AnswerAdmin(TeachingAdmin):
    form = AnswerForm
    list_display = ["attempt", "question_id", "score", "graded_by", "graded_at"]
    list_filter = ["attempt__assessment", "graded_at"]
    readonly_fields = ["attempt", "question_text", "question_id", "maximum", "text", "submission", "graded_by", "graded_at", "updated_at"]
    fields = ["attempt", "question_id", "question_text", "maximum", "text", "submission", "score", "feedback", "expected_updated_at", "graded_by", "graded_at", "updated_at"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Вопрос опубликованной версии")
    def question_text(self, obj):
        return services.frozen_item(obj.attempt, "questions", obj.question_id)["text"]

    @admin.display(description="Максимум баллов")
    def maximum(self, obj):
        return services.frozen_item(obj.attempt, "questions", obj.question_id)["max_score"]

    def save_model(self, request, obj, form, change):
        saved = services.grade_answer(request.user, obj.pk, obj.score, obj.feedback, form.cleaned_data["expected_updated_at"])
        obj.graded_by, obj.graded_at, obj.updated_at = saved.graded_by, saved.graded_at, saved.updated_at


admin.site.unregister(User)
admin.site.unregister(Group)


@admin.register(User)
class DepartmentUserAdmin(UserAdmin, TeachingAdmin):
    def has_module_permission(self, request):
        return request.user.is_staff

    def has_view_permission(self, request, obj=None):
        return request.user.is_staff

    def has_add_permission(self, request):
        return request.user.is_staff

    def has_change_permission(self, request, obj=None):
        return request.user.is_staff and (request.user.is_superuser or obj is None or not obj.is_staff or obj.pk == request.user.pk)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_staff and (request.user.is_superuser or obj is None or not obj.is_staff)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(is_staff=False, is_superuser=False)

    def get_fieldsets(self, request, obj=None):
        if request.user.is_superuser:
            return super().get_fieldsets(request, obj)
        if obj is None:
            return self.add_fieldsets
        return [(None, {"fields": ["username", "password"]}), ("Студент", {"fields": ["first_name", "last_name", "email", "is_active", "groups"]})]

    def get_list_filter(self, request):
        return super().get_list_filter(request) if request.user.is_superuser else ["is_active", "groups"]

    def get_form(self, request, obj=None, **kwargs):
        if not request.user.is_superuser:
            kwargs["exclude"] = ["is_staff", "is_superuser", "user_permissions"]
        return super().get_form(request, obj, **kwargs)


@admin.register(Group)
class DepartmentGroupAdmin(GroupAdmin, TeachingAdmin):
    fields = ["name"]
    filter_horizontal = []

    def has_module_permission(self, request):
        return request.user.is_staff

    has_view_permission = TeachingAdmin.has_view_permission
    has_add_permission = TeachingAdmin.has_add_permission
    has_change_permission = TeachingAdmin.has_change_permission
    has_delete_permission = TeachingAdmin.has_delete_permission
