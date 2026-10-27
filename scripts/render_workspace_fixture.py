"""Render a browser-test fixture without reading or changing the application database."""
import json
import os
from datetime import timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ["DEBUG"] = "1"

import django
django.setup()
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.utils import timezone

request = RequestFactory().get("/attempts/101/")
request.user = User(username="student", first_name="Алексей", last_name="Иванов")
now = timezone.now()
closed = "--readonly" in sys.argv
source = '#include <iostream>\nusing namespace std;\n\nint main() {\n    // Напишите решение здесь\n    return 0;\n}\n'
context = {
    "attempt_id": 101, "title": "Лабораторная №2 · Основы C++", "version": 1,
    "instructions": "Решите задачи и ответьте на вопрос по теории. Для оценки отправьте код на проверку.",
    "active": not closed, "can_edit": not closed,
    "clock": {"server_now": now.isoformat(), "deadline": (now+timedelta(hours=1)).isoformat(), "closed": closed},
    "problems": [
        {"id":1, "title":"Сумма двух чисел", "statement":"Даны два целых числа a и b. Выведите их сумму.\n\nВвод\nДва целых числа через пробел: −10⁹ ≤ a, b ≤ 10⁹.\n\nВывод\nОдно целое число — сумма a и b.", "time_limit":2, "memory_mb":128, "max_score":10, "required":True, "source":source,
         "samples":[{"label":"Положительные числа", "input":"2 3\n", "expected":"5\n"}, {"label":"Отрицательное число", "input":"-8 3\n", "expected":"-5\n"}]},
        {"id":2, "title":"Максимум из трёх", "statement":"Прочитайте три целых числа. Выведите наибольшее из них.", "time_limit":1, "memory_mb":64, "max_score":10, "required":True, "source":source,
         "samples":[{"label":"Разные числа", "input":"2 9 3\n", "expected":"9\n"}]},
    ],
    "questions":[{"id":1, "text":"Какой целочисленный тип вы выбрали для суммы и почему он подходит для ограничений задачи?", "max_score":5, "answer":{"text":"", "submission_id":None}, "submissions":[], "linked":True, "problem_id":1}],
    "submissions":[], "latest_judged":{}, "results_visible":True,
}
workspace_html = render_to_string("lab/attempt.html", context, request=request)

# Teacher fixtures use unsaved objects; no working database is read or changed.
from lab.models import Assessment, AssessmentVersion, Attempt, Submission, TheoryAnswer
from lab.teacher_views import GradeForm, ExtensionForm
from lab.services import result_summary
teacher = User(pk=7, username="teacher", is_staff=True)
request.user = teacher
work = Assessment(pk=8, title=context["title"])
snapshot = {"title": context["title"], "problems": context["problems"], "questions": context["questions"], "pass_score": 10}
version = AssessmentVersion(pk=9, assessment=work, number=1, snapshot=snapshot, published_at=now)
student = User(pk=10, username="student", first_name="Алексей", last_name="Иванов")
attempt = Attempt(pk=101, student=student, assessment=work, version=version, started_at=now, deadline=now+timedelta(hours=1))
submission = Submission(pk=200, attempt=attempt, problem_id=1, source=source, kind="judge")
answer = TheoryAnswer(pk=44, attempt=attempt, question_id=1, text="Использую long long.", submission=submission, updated_at=now)
grade_form = GradeForm(prefix="q1", maximum=5, initial={"expected_updated_at":now.isoformat()})
teacher_attempt = render_to_string("teacher/attempt.html", {"item":attempt, "questions":[{"question":snapshot["questions"][0], "answer":answer, "form":grade_form, "grade_changes":[{"created_at":now,"actor":teacher,"get_reason_display":"Оценка преподавателем","old_score":None,"new_score":3,"old_feedback":"","new_feedback":"Первая проверка","answer_revision":1,"answer_text":"Первый ответ","submission_id":200}]}], "summary":result_summary(attempt, submissions=[], answers=[answer]), "extension_form":ExtensionForm(), "extensions":[], "submissions":[submission]}, request=request)
monitor = {"server_now":now.isoformat(), "worker_label":"Ожидает задания", "heartbeat_at":now.isoformat(), "fresh":True, "current_submission":None, "message":"", "queued":0, "running":0, "oldest_seconds":0, "errors":[]}
teacher_judge = render_to_string("teacher/judge.html", {"state":monitor}, request=request)
print(json.dumps({"html":workspace_html, "clock":context["clock"], "source":source,
                  "teacher_attempt":teacher_attempt, "teacher_judge":teacher_judge, "monitor":monitor}, ensure_ascii=False))
