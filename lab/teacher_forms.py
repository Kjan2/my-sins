"""Forms for the teacher's course, task and assessment editors."""
from django import forms
from django.core.exceptions import ValidationError
from django.forms import BaseFormSet, formset_factory, inlineformset_factory

from .models import Assessment, AssessmentProblem, Course, Problem, TestCase
from .permissions import groups_for
from .validators import validate_utf8_size


class CourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ["title", "groups"]
        widgets = {"groups": forms.CheckboxSelectMultiple}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["groups"].queryset = groups_for(user).order_by("name").distinct()
        self.fields["groups"].help_text = "Новые группы можно добавить через импорт студентов."


class AssessmentForm(forms.ModelForm):
    class Meta:
        model = Assessment
        fields = ["title", "kind", "instructions", "groups", "opens_at", "closes_at",
                  "duration_minutes", "pass_score", "submission_limit", "reveal_after_close"]
        widgets = {
            "instructions": forms.Textarea(attrs={"rows": 4}),
            "groups": forms.CheckboxSelectMultiple,
            **{name: forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"})
               for name in ("opens_at", "closes_at")},
        }

    def __init__(self, *args, course, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["groups"].queryset = course.groups.order_by("name")
        self.fields["groups"].required = False
        self.fields["duration_minutes"].help_text = "Для зачёта обязательно. Время отсчитывается с начала попытки."
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


RuleFormSet = inlineformset_factory(Assessment, AssessmentProblem, form=RuleForm,
    extra=0, can_delete=True, max_num=100, validate_max=True, absolute_max=150)


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
    class Meta:
        model = Problem
        fields = ["title", "statement", "statement_format", "input_description", "output_description",
                  "constraints", "starter_code", "reference_source", "time_limit", "memory_mb"]
        widgets = {name: forms.Textarea(attrs={"rows": 4 if name not in ("statement", "reference_source") else 8})
                   for name in ("statement", "input_description", "output_description", "constraints", "starter_code", "reference_source")}

    def clean_starter_code(self):
        value = self.cleaned_data["starter_code"]
        validate_utf8_size(value)
        return value


TestFormSet = inlineformset_factory(Problem, TestCase,
    fields=["label", "input", "expected", "is_sample", "order"],
    widgets={"input": forms.Textarea(attrs={"rows": 3, "spellcheck": "false"}),
             "expected": forms.Textarea(attrs={"rows": 3, "spellcheck": "false"})},
    extra=0, can_delete=True, max_num=100, validate_max=True, absolute_max=150)
