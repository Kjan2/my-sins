from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from lab.models import Assessment, AssessmentProblem, Problem, TestCase as ProblemTest


@override_settings(SECURE_SSL_REDIRECT=False)
class PublicationTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user("teacher", is_staff=True)
        self.student = User.objects.create_user("student")
        self.group = Group.objects.create(name="Students")
        self.student.groups.add(self.group)
        self.problem = Problem.objects.create(title="Sum", statement="Add two numbers.")
        now = timezone.now()
        self.work = Assessment.objects.create(
            title="Publication test", opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=1),
        )
        self.work.groups.add(self.group)
        self.rule = AssessmentProblem.objects.create(assessment=self.work, problem=self.problem)
        self.url = reverse("admin:lab_assessment_change", args=[self.work.pk])

    def form_data(self, *, adding=False):
        opens = timezone.localtime(self.work.opens_at)
        closes = timezone.localtime(self.work.closes_at)
        return {
            "title": self.work.title,
            "kind": "lab",
            "groups": [self.group.pk],
            "opens_at_0": opens.strftime("%Y-%m-%d"),
            "opens_at_1": opens.strftime("%H:%M:%S"),
            "closes_at_0": closes.strftime("%Y-%m-%d"),
            "closes_at_1": closes.strftime("%H:%M:%S"),
            "pass_score": "1",
            "problem_rules-TOTAL_FORMS": "1",
            "problem_rules-INITIAL_FORMS": "0" if adding else "1",
            "problem_rules-0-id": "" if adding else str(self.rule.pk),
            "problem_rules-0-problem": str(self.problem.pk),
            "problem_rules-0-max_score": "25",
            "problem_rules-0-required": "on",
            "problem_rules-0-order": "0",
            "questions-TOTAL_FORMS": "0",
            "questions-INITIAL_FORMS": "0",
            "_publish": "1",
        }

    def add_hidden_test(self):
        ProblemTest.objects.create(problem=self.problem, label="Hidden", input="2 3", expected="5")

    def test_draft_hidden_and_publication_button_visible_to_teacher(self):
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get("/"), self.work.title)
        self.client.force_login(self.teacher)
        response = self.client.get(self.url)
        self.assertContains(response, "Черновик — студентам не видна")
        self.assertContains(response, 'name="_publish"')
        self.assertContains(self.client.get(reverse("admin:lab_assessment_changelist")), "Черновик · студентам не видна")

    def test_missing_tests_prevent_publication_and_explain_why(self):
        self.client.force_login(self.teacher)
        response = self.client.post(self.url, self.form_data(), follow=True)
        self.assertContains(response, "Изменения сохранены, но не опубликованы")
        self.assertContains(response, "добавьте хотя бы один скрытый тест")
        self.work.refresh_from_db()
        self.assertIsNone(self.work.current_version_id)
        self.assertEqual(self.work.problem_rules.get().max_score, 25)

    def test_publish_saved_inline_rules_then_show_to_assigned_student(self):
        self.add_hidden_test()
        self.client.force_login(self.teacher)
        response = self.client.post(self.url, self.form_data(), follow=True)
        self.assertContains(response, "Опубликована версия 1")
        self.work.refresh_from_db()
        self.assertEqual(self.work.current_version.snapshot["problems"][0]["max_score"], 25)
        self.client.force_login(self.student)
        self.assertContains(self.client.get("/"), self.work.title)
        self.student.groups.clear()
        self.assertNotContains(self.client.get("/"), self.work.title)

    def test_plain_save_keeps_work_unpublished(self):
        self.add_hidden_test()
        self.client.force_login(self.teacher)
        data = self.form_data()
        data.pop("_publish")
        data["_save"] = "1"
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302)
        self.work.refresh_from_db()
        self.assertIsNone(self.work.current_version_id)

    def test_publish_new_assessment_from_add_form(self):
        self.add_hidden_test()
        self.client.force_login(self.teacher)
        data = self.form_data(adding=True)
        data["title"] = "New published work"
        response = self.client.post(reverse("admin:lab_assessment_add"), data, follow=True)
        self.assertContains(response, "Опубликована версия 1")
        work = Assessment.objects.get(title=data["title"])
        self.assertIsNotNone(work.current_version_id)
        self.assertEqual(work.current_version.snapshot["problems"][0]["max_score"], 25)
