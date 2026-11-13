"""Forms for the teacher's course, task and assessment editors."""
from django import forms
from django.core.exceptions import ValidationError
from django.forms import BaseFormSet, formset_factory, inlineformset_factory

from .models import Assessment, AssessmentProblem, Course, Problem, TestCase
from django.contrib.auth import get_user_model
from .permissions import groups_for
from .validators import validate_utf8_size


class CourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ["title", "groups", "teachers", "description", "faq", "acceptance"]
        widgets = {"groups": forms.CheckboxSelectMultiple}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["teachers"].queryset = get_user_model().objects.filter(is_staff=True, is_active=True)
        self.fields["teachers"].required = False
        self.fields["groups"].queryset = groups_for(user).order_by("name").distinct()
        self.fields["groups"].help_text = "Новые группы можно добавить через импорт студентов."


class AssessmentForm(forms.ModelForm):
    class Meta:
        model = Assessment
        fields = ["title", "kind", "instructions", "groups", "opens_at", "closes_at",
                  "duration_minutes", "pass_score", "submission_limit", "reveal_after_close", "task_workflow", "linear", "requires_confirmation", "invited_students", "grade_thresholds"]
        widgets = {
            "task_workflow": forms.HiddenInput,
            "instructions": forms.Textarea(attrs={"rows": 4}),
            "groups": forms.CheckboxSelectMultiple,
            **{name: forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"})
               for name in ("opens_at", "closes_at")},
        }

    def __init__(self, *args, course, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["invited_students"].queryset = get_user_model().objects.filter(is_staff=False, is_active=True).order_by("username")
        self.fields["invited_students"].help_text = "Индивидуальный допуск действует сразу после сохранения, в том числе для уже опубликованной работы."
        self.fields["groups"].queryset = course.groups.order_by("name")
        self.fields["closes_at"].label = "Дедлайн начала попыток (МСК)"
        self.fields["groups"].required = False
        self.fields["duration_minutes"].help_text = "В новом режиме — время по умолчанию для каждого задания, если в нём не указан свой таймер. Дедлайн начала не сокращает время решения."
        self.fields["submission_limit"].help_text = "Оставьте пустым, чтобы не ограничивать число отправок."
        self.fields["reveal_after_close"].label = "Показывать оценки только после общего закрытия"


class RuleForm(forms.ModelForm):
    class Meta:
        model = AssessmentProblem
        fields = ["problem", "max_score", "required", "order"]
        widgets = {"order": forms.NumberInput(attrs={"min": 0})}

    def __init__(self, *args, course, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["problem"].queryset = course.problems.order_by("title", "pk")
        self.fields["problem"].empty_label = "Выберите задачу"
        self.fields["order"].label = "Порядок"
        self.fields["required"].label = "Обязательная (в новом режиме обязательны все задания)"


RuleFormSet = inlineformset_factory(Assessment, AssessmentProblem, form=RuleForm,
    extra=0, can_delete=True, max_num=10000, validate_max=False, absolute_max=10000)


class QuestionForm(forms.Form):
    question_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    text = forms.CharField(label="Вопрос", max_length=20000, widget=forms.Textarea(attrs={"rows": 3}))
    problem = forms.ModelChoiceField(label="Связанная задача", queryset=Problem.objects.none(),
                                    required=False, empty_label="Ко всей работе")
    max_score = forms.IntegerField(label="Максимум баллов", min_value=1, max_value=2147483647, initial=5)

    def __init__(self, *args, course, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["problem"].queryset = course.problems.order_by("title", "pk")


class BaseQuestionFormSet(BaseFormSet):
    def __init__(self, *args, existing_ids=(), problem_ids=(), **kwargs):
        self.existing_ids = set(existing_ids)
        self.problem_ids = set(problem_ids)
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        seen = set()
        for form in self.forms:
            data = form.cleaned_data
            if not data or data.get("DELETE"):
                continue
            pk = data.get("question_id")
            if pk is not None and (pk not in self.existing_ids or pk in seen):
                raise ValidationError("Вопрос не относится к этой работе или указан дважды.")
            if pk is not None:
                seen.add(pk)
            if data.get("problem") and data["problem"].pk not in self.problem_ids:
                form.add_error("problem", "Добавьте эту задачу в состав работы или выберите «Ко всей работе».")


QuestionFormSet = formset_factory(QuestionForm, formset=BaseQuestionFormSet,
    extra=0, can_delete=True, max_num=100, validate_max=True, absolute_max=150)


class ProblemForm(forms.ModelForm):
    reference_file = forms.FileField(label="Загрузить эталон .cpp", required=False, help_text="UTF-8, до 64 КиБ. Загрузка заменит текст эталона.")
    class Meta:
        model = Problem
        fields = ["title", "statement", "statement_format", "input_description", "output_description",
                  "constraints", "starter_code", "reference_source", "time_limit", "memory_mb",
                  "kind", "solution_minutes", "attempt_limit", "allow_back", "shuffle_options", "question_seconds",
                  "correct_threshold", "quiz_questions", "check_mode", "task_name", "starter_header", "starter_implementation",
                  "unit_tests", "format_policy", "static_policy", "dynamic_policy", "format_style", "show_test_logs", "diagnostic_policies"]
        widgets = {name: forms.Textarea(attrs={"rows": 4 if name not in ("statement", "reference_source") else 8})
                   for name in ("statement", "input_description", "output_description", "constraints", "starter_code", "reference_source")}

    def __init__(self, *args, **kwargs):
        if args and args[0] is not None:
            data = args[0].copy()
            for name, default in (("kind", "code"), ("check_mode", "stdout"), ("task_name", "task"), ("correct_threshold", 1),
                                  ("format_policy", "off"), ("static_policy", "off"), ("dynamic_policy", "off"), ("format_style", "LLVM")):
                if name not in data:
                    data[name] = default
            args = (data, *args[1:])
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        file = data.get('reference_file')
        if file:
            try:
                source = file.read(65537).decode('utf-8-sig')
                validate_utf8_size(source)
            except (UnicodeDecodeError, ValidationError):
                self.add_error('reference_file', 'Файл должен быть UTF-8 размером не больше 64 КиБ.')
            else:
                data['reference_source'] = source
        return data

    def clean_starter_code(self):
        value = self.cleaned_data["starter_code"]
        validate_utf8_size(value)
        return value


TestFormSet = inlineformset_factory(Problem, TestCase,
    fields=["label", "input", "expected", "is_sample", "order"],
    widgets={"input": forms.Textarea(attrs={"rows": 3, "spellcheck": "false"}),
             "expected": forms.Textarea(attrs={"rows": 3, "spellcheck": "false"})},
    extra=0, can_delete=True, max_num=100, validate_max=True, absolute_max=150)
