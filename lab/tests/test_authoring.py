from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.utils import timezone

from lab import services
from lab.models import Assessment, Course, Problem, TestCase as Case, TheoryQuestion
from . import test_workflow


@override_settings(SECURE_SSL_REDIRECT=False)
class AuthoringTests(TestCase):
    def setUp(self):
        test_workflow.WorkflowTests.setUp(self)
        self.client.force_login(self.teacher)
        self.foreign_group = Group.objects.create(name="Foreign group")
        self.foreign_course = Course.objects.create(title="Foreign course")
        self.foreign_problem = Problem.objects.create(course=self.foreign_course, title="Foreign problem", statement="Private")
        self.foreign_work = Assessment.objects.create(course=self.foreign_course, title="Foreign work",
            opens_at=self.work.opens_at, closes_at=self.work.closes_at)

    def payload(self, *, editing=False):
        rule = self.work.problem_rules.get() if editing else None
        data = {"course": self.course.pk, "title": "Новая лабораторная", "kind": "lab", "instructions": "Решите задачу",
            "groups": [self.group.pk], "opens_at": timezone.localtime(self.work.opens_at).strftime("%Y-%m-%dT%H:%M"),
            "closes_at": timezone.localtime(self.work.closes_at).strftime("%Y-%m-%dT%H:%M"), "pass_score": 10,
            "rules-TOTAL_FORMS": 1, "rules-INITIAL_FORMS": int(editing),
            "rules-0-id": rule.pk if rule else "", "rules-0-problem": self.problem.pk,
            "rules-0-max_score": 10, "rules-0-required": "on", "rules-0-order": 0,
            "questions-TOTAL_FORMS": 1, "questions-INITIAL_FORMS": int(editing),
            "questions-0-question_id": self.question.pk if editing else "",
            "questions-0-text": "Почему выбран этот тип?", "questions-0-problem": self.problem.pk,
            "questions-0-max_score": 5, "action": "save"}
        return data

    def test_management_pages_render_without_admin_links(self):
        for url in ("/teacher/", "/teacher/courses/", "/teacher/courses/new/",
                    f"/teacher/courses/{self.course.pk}/edit/", "/teacher/assessments/new/",
                    f"/teacher/assessments/{self.work.pk}/edit/", "/teacher/problems/",
                    "/teacher/problems/new/", f"/teacher/problems/{self.problem.pk}/edit/"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'href="/admin/')

    def test_staff_scope_applies_to_pages_and_foreign_choices(self):
        for path in (f"/teacher/courses/{self.foreign_course.pk}/edit/",
                     f"/teacher/assessments/{self.foreign_work.pk}/edit/",
                     f"/teacher/problems/{self.foreign_problem.pk}/edit/",
                     f"/teacher/assessments/new/?course={self.foreign_course.pk}"):
            self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.client.post(path, {}).status_code, 404)
        response = self.client.get("/teacher/problems/")
        self.assertNotContains(response, "Foreign problem")
        self.client.force_login(self.student)
        self.assertEqual(self.client.get("/teacher/assessments/new/").status_code, 403)
        self.assertEqual(self.client.post("/teacher/problems/new/", {}).status_code, 403)

    def test_course_creation_assigns_teacher_and_rejects_foreign_groups(self):
        response = self.client.post("/teacher/courses/new/", {"title": "Новый курс", "groups": [self.group.pk]})
        self.assertEqual(response.status_code, 302)
        course = Course.objects.get(title="Новый курс")
        self.assertTrue(course.teachers.filter(pk=self.teacher.pk).exists())
        self.assertEqual(list(course.groups.all()), [self.group])
        response = self.client.post("/teacher/courses/new/", {"title": "Недоступная группа", "groups": [self.foreign_group.pk]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Course.objects.filter(title="Недоступная группа").exists())

    def test_work_task_and_linked_question_can_be_created_and_published_together(self):
        data = self.payload()
        data["action"] = "publish"
        response = self.client.post("/teacher/assessments/new/", data)
        self.assertEqual(response.status_code, 302)
        work = Assessment.objects.get(title=data["title"])
        self.assertIsNotNone(work.current_version_id)
        self.assertEqual(work.current_version.snapshot["questions"][0]["problem_id"], self.problem.pk)
        self.assertEqual(work.current_version.snapshot["group_ids"], [self.group.pk])
        self.assertEqual(work.current_version.snapshot["opens_at"], work.opens_at.isoformat())

    def test_draft_without_groups_and_tasks_is_saved(self):
        data = self.payload()
        data.update({"groups": [], "rules-TOTAL_FORMS": 0, "questions-TOTAL_FORMS": 0})
        response = self.client.post("/teacher/assessments/new/", data)
        self.assertEqual(response.status_code, 302)
        work = Assessment.objects.get(title=data["title"])
        self.assertIsNone(work.current_version_id)
        self.assertFalse(work.groups.exists())

    def test_save_and_republish_preserve_existing_attempt_snapshot(self):
        data = self.payload(editing=True)
        previous_version = self.attempt.version_id
        response = self.client.post(f"/teacher/assessments/{self.work.pk}/edit/", data)
        self.assertEqual(response.status_code, 302)
        self.work.refresh_from_db()
        self.assertEqual(self.work.current_version_id, previous_version)
        self.assertNotEqual(self.work.title, self.work.current_version.snapshot["title"])
        data["action"] = "publish"
        response = self.client.post(f"/teacher/assessments/{self.work.pk}/edit/", data)
        self.assertEqual(response.status_code, 302)
        self.work.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(self.work.current_version.number, 2)
        self.assertEqual(self.attempt.version_id, previous_version)

    def test_invalid_forms_never_partially_save_or_move_a_work(self):
        original_title = self.work.title
        path = f"/teacher/assessments/{self.work.pk}/edit/"
        for changes in ({"rules-0-problem": self.foreign_problem.pk},
                        {"groups": [self.foreign_group.pk]},
                        {"questions-0-question_id": 999999},
                        {"questions-0-max_score": "9" * 30},
                        {"closes_at": "invalid"}):
            data = self.payload(editing=True)
            data.update(changes)
            response = self.client.post(path, data)
            self.assertEqual(response.status_code, 400)
            self.work.refresh_from_db()
            self.assertEqual(self.work.title, original_title)
        data = self.payload(editing=True)
        data["course"] = self.foreign_course.pk
        self.assertEqual(self.client.post(path, data).status_code, 302)
        self.work.refresh_from_db()
        self.assertEqual(self.work.course_id, self.course.pk)

    def test_removing_a_linked_task_requires_updating_the_question(self):
        data = self.payload(editing=True)
        data["rules-0-DELETE"] = "on"
        path = f"/teacher/assessments/{self.work.pk}/edit/"
        self.assertEqual(self.client.post(path, data).status_code, 400)
        self.assertTrue(self.work.problem_rules.exists())
        data["questions-0-problem"] = ""
        self.assertEqual(self.client.post(path, data).status_code, 302)
        self.assertFalse(self.work.problem_rules.exists())
        self.question.refresh_from_db()
        self.assertIsNone(self.question.problem_id)

    def test_failed_publication_keeps_draft_and_previous_published_version(self):
        self.problem.tests.filter(is_sample=False).delete()
        data = self.payload(editing=True)
        data["action"] = "publish"
        response = self.client.post(f"/teacher/assessments/{self.work.pk}/edit/", data, follow=True)
        self.assertContains(response, "Публикация не выполнена")
        self.work.refresh_from_db()
        self.assertEqual(self.work.title, data["title"])
        self.assertEqual(self.work.current_version_id, self.attempt.version_id)

    def test_problem_and_tests_save_together_and_preview_escapes_html(self):
        data = {"course": self.course.pk, "title": "Новая задача", "statement": "Сложите числа", "statement_format": "plain",
            "time_limit": 2, "memory_mb": 128, "tests-TOTAL_FORMS": 2, "tests-INITIAL_FORMS": 0,
            "tests-0-label": "Пример", "tests-0-input": "1 2", "tests-0-expected": "3", "tests-0-is_sample": "on", "tests-0-order": 0,
            "tests-1-label": "Скрытый", "tests-1-input": "2 3", "tests-1-expected": "5", "tests-1-order": 1}
        response = self.client.post("/teacher/problems/new/", data)
        self.assertEqual(response.status_code, 302)
        problem = Problem.objects.get(title=data["title"])
        self.assertEqual(problem.tests.count(), 2)
        self.assertEqual(problem.tests.filter(is_sample=False).count(), 1)
        data["title"], data["tests-1-expected"] = "Нельзя сохранить", "я" * 32769
        self.assertEqual(self.client.post("/teacher/problems/new/", data).status_code, 400)
        self.assertFalse(Problem.objects.filter(title="Нельзя сохранить").exists())
        response = self.client.post("/teacher/problems/preview/", {"statement": "<script>alert(1)</script>", "statement_format": "markdown"})
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")

    def test_work_filters_are_scoped_and_searchable(self):
        Assessment.objects.create(course=self.course, title="Черновик", opens_at=self.work.opens_at, closes_at=self.work.closes_at)
        self.assertContains(self.client.get("/teacher/?status=draft"), "Черновик")
        self.assertNotContains(self.client.get("/teacher/?status=draft"), ">Exam</a>")
        self.assertNotContains(self.client.get("/teacher/?q=Exam"), ">Черновик</a>")
        self.assertNotContains(self.client.get("/teacher/"), "Foreign work")
