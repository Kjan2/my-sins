from datetime import timedelta
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from lab.models import Course, Assessment, AssessmentProblem, Problem, TestCase, TheoryQuestion
from lab.services import publish_assessment
from lab.permissions import scoped
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "Create a published demonstration assessment (no accounts or known passwords)."

    def add_arguments(self, parser):
        parser.add_argument("--teacher", required=True, help="Username of an existing active staff user")

    @transaction.atomic
    def handle(self, *args, **options):
        actor = get_user_model().objects.get(username=options["teacher"], is_staff=True, is_active=True)
        group, _ = Group.objects.get_or_create(name="Демонстрационная группа")
        work = scoped(actor, Assessment.objects.all()).filter(title="Демонстрация · сумма двух чисел").first()
        if work:
            self.stdout.write("Демонстрационная работа уже существует.")
            return
        course = Course.objects.create(title="Демонстрационный курс")
        course.teachers.add(actor)
        course.groups.add(group)
        problem = Problem.objects.create(course=course, title="Сумма двух чисел", statement="Даны два целых числа a и b (−10⁹ ≤ a, b ≤ 10⁹). Выведите их сумму.\nВвод: два числа через пробел. Вывод: одно целое число.")
        TestCase.objects.create(problem=problem, label="Пример 1", input="2 3\n", expected="5\n", is_sample=True)
        TestCase.objects.create(problem=problem, label="Отрицательные", input="-9 -4\n", expected="-13\n")
        TestCase.objects.create(problem=problem, label="Граница", input="1000000000 1000000000\n", expected="2000000000\n")
        now = timezone.now()
        work = Assessment.objects.create(course=course, title="Демонстрация · сумма двух чисел", kind="exam", opens_at=now-timedelta(minutes=1), closes_at=now+timedelta(days=7), duration_minutes=60, pass_score=10, instructions="Решите задачу и объясните выбор типа данных. Отправка на проверку оценивает код; черновик сам по себе не оценивается.")
        work.groups.add(group)
        AssessmentProblem.objects.create(assessment=work, problem=problem, max_score=10)
        TheoryQuestion.objects.create(assessment=work, problem=problem, text="Какой целочисленный тип вы выбрали и почему он подходит для ограничений задачи?", max_score=5)
        publish_assessment(actor, work.pk)
        self.stdout.write(self.style.SUCCESS(f"Создана работа #{work.pk}. Добавьте студентов в группу «{group.name}»."))
