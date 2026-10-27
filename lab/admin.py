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
from .permissions import courses_for, groups_for, scoped

admin.site.site_header = "Кафедра · управление работами"
admin.site.site_title = "Кафедра"


class TeachingAdmin(admin.ModelAdmin):
    course_lookup = "course"

    def get_queryset(self, request):
        return scoped(request.user, super().get_queryset(request), self.course_lookup)

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request) and (obj is None or self.get_queryset(request).filter(pk=obj.pk).exists())

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_view_permission(request, obj)

    def get_list_filter(self, request):
        filters = super().get_list_filter(request)
        if request.user.is_superuser:
            return filters
        # Django's default relation filters enumerate objects outside the changelist.
        return [field for field in filters if isinstance(field, str) and "__" not in field
                and not self.model._meta.get_field(field).is_relation]

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and self.model in (models.Problem, models.Assessment):
            fields.append("course")
        return fields

    def get_form(self, request, obj=None, **kwargs):
        if self.model in (models.Problem, models.Assessment):
            course_id = obj.course_id if obj else request.POST.get("course") or request.GET.get("course")
            courses = courses_for(request.user)
            if not course_id:
                choices = list(courses.values_list("pk", flat=True)[:2])
                course_id = choices[0] if len(choices) == 1 else None
            request.teaching_course_id = courses.filter(pk=course_id).values_list("pk", flat=True).first() if str(course_id).isdecimal() and len(str(course_id)) <= 19 else None
        return super().get_form(request, obj, **kwargs)

    def add_view(self, request, form_url="", extra_context=None):
        if self.model in (models.Problem, models.Assessment) and request.method == "GET":
            courses = courses_for(request.user)
            selected = request.GET.get("course")
            if selected:
                if not selected.isdecimal() or len(selected) > 19:
                    from django.http import Http404
                    raise Http404
                get_object_or_404(courses, pk=selected)
            elif courses.count() != 1:
                return TemplateResponse(request, "admin/lab/select_course.html", {
                    **self.admin_site.each_context(request), "title": "Выберите курс", "courses": courses,
                    "opts": self.model._meta})
        return super().add_view(request, form_url, extra_context)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        if self.model in (models.Problem, models.Assessment) and "course" not in initial:
            courses = list(courses_for(request.user)[:2])
            if len(courses) == 1:
                initial["course"] = courses[0].pk
        return initial

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.related_model == models.Course:
            kwargs["queryset"] = courses_for(request.user)
            course_id = getattr(request, "teaching_course_id", None)
            if course_id:
                kwargs["queryset"] = kwargs["queryset"].filter(pk=course_id)
                kwargs["empty_label"] = None
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.related_model == Group:
            groups = groups_for(request.user)
            if self.model == models.Assessment:
                groups = groups.filter(courses__pk=getattr(request, "teaching_course_id", None))
            kwargs["queryset"] = groups.distinct()
        return super().formfield_for_manytomany(db_field, request, **kwargs)


@admin.register(models.Course)
class CourseAdmin(TeachingAdmin):
    course_lookup = "pk"
    list_display = ["title"]
    filter_horizontal = ["teachers", "groups"]

    def get_readonly_fields(self, request, obj=None):
        return [] if request.user.is_superuser else ["teachers"]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if not request.user.is_superuser and not change:
            form.instance.teachers.add(request.user)


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

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.related_model == models.Problem:
            kwargs["queryset"] = scoped(request.user, models.Problem.objects.all()).filter(
                course_id=getattr(request, "teaching_course_id", None))
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(models.Problem)
class ProblemAdmin(TeachingAdmin):
    change_form_template = "admin/lab/problem/change_form.html"
    list_display = ["title", "course", "time_limit", "memory_mb"]
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
    list_display = ["title", "course", "kind", "opens_at", "closes_at", "publication_status"]
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
    course_lookup = "assessment__course"
    list_display = ["__str__", "published_at", "published_by"]
    readonly_fields = ["assessment", "number", "snapshot", "published_at", "published_by"]


class ExtensionForm(forms.Form):
    new_deadline = forms.DateTimeField(label="Новый срок", help_text="Часовой пояс: Europe/Moscow. Формат: ГГГГ-ММ-ДД ЧЧ:ММ:СС")
    reason = forms.CharField(label="Причина", widget=forms.Textarea)


@admin.register(models.Attempt)
class AttemptAdmin(ReadOnlyAdmin):
    course_lookup = "assessment__course"
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
        item = get_object_or_404(self.get_queryset(request), pk=pk)
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
    course_lookup = "attempt__assessment__course"
    list_display = ["attempt", "actor", "old_deadline", "new_deadline", "created_at"]
    readonly_fields = ["attempt", "actor", "old_deadline", "new_deadline", "reason", "created_at"]


@admin.register(models.GradeChange)
class GradeChangeAdmin(ReadOnlyAdmin):
    course_lookup = "answer__attempt__assessment__course"
    list_display = ["answer", "actor", "reason", "old_score", "new_score", "created_at"]
    list_filter = ["reason", "created_at"]


@admin.register(models.Submission)
class SubmissionAdmin(ReadOnlyAdmin):
    course_lookup = "attempt__assessment__course"
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
    course_lookup = "attempt__assessment__course"
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
    course_lookup = "groups__courses"

    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def get_queryset(self, request):
        qs = User.objects.all()
        return qs if request.user.is_superuser else scoped(request.user,
            qs.filter(is_staff=False, is_superuser=False), self.course_lookup)

    def has_view_permission(self, request, obj=None):
        return TeachingAdmin.has_view_permission(self, request, obj)

    def get_fieldsets(self, request, obj=None):
        if request.user.is_superuser:
            return super().get_fieldsets(request, obj)
        return [("Студент", {"fields": ["username", "first_name", "last_name", "email"]})]

    def get_readonly_fields(self, request, obj=None):
        return super().get_readonly_fields(request, obj) if request.user.is_superuser else ["username", "first_name", "last_name", "email"]

    def get_list_filter(self, request):
        return super().get_list_filter(request) if request.user.is_superuser else ["is_active"]

    def get_form(self, request, obj=None, **kwargs):
        if not request.user.is_superuser:
            kwargs["exclude"] = ["is_staff", "is_superuser", "user_permissions"]
        return super().get_form(request, obj, **kwargs)


@admin.register(Group)
class DepartmentGroupAdmin(GroupAdmin, TeachingAdmin):
    course_lookup = "courses"
    fields = ["name"]
    filter_horizontal = []

    has_view_permission = TeachingAdmin.has_view_permission

    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    has_delete_permission = has_change_permission
