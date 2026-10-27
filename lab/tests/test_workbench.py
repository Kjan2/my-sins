from lab.models import Course
from datetime import timedelta
from html.parser import HTMLParser

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.utils import timezone

from lab import services
from lab.models import Assessment, AssessmentProblem, Attempt, Problem, Submission, TestCase as ProblemTest, TheoryQuestion


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids = []
        self.controls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if "aria-controls" in attrs:
            self.controls.append(attrs["aria-controls"])


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkbenchTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user("teacher", is_staff=True)
        self.student = User.objects.create_user("student")
        self.other = User.objects.create_user("other")
        group = Group.objects.create(name="Students")
        self.course = Course.objects.create(title="Course")
        self.course.teachers.add(self.teacher)
        self.course.groups.add(group)
        self.student.groups.add(group)
        now = timezone.now()
        self.work = Assessment.objects.create(course=self.course, title="Workbench", opens_at=now-timedelta(hours=1), closes_at=now+timedelta(hours=2))
        self.work.groups.add(group)
        self.problems = []
        for title in ["Sum", "Maximum"]:
            problem = Problem.objects.create(course=self.course, title=title, statement="Read stdin and write stdout.")
            ProblemTest.objects.create(problem=problem, label="Example", input="2 3", expected="5", is_sample=True)
            ProblemTest.objects.create(problem=problem, label="PRIVATE_TEST", input="PRIVATE_INPUT", expected="PRIVATE_EXPECTED")
            AssessmentProblem.objects.create(assessment=self.work, problem=problem)
            self.problems.append(problem)
        TheoryQuestion.objects.create(assessment=self.work, problem=self.problems[0], text="Explain your type choice.")
        services.publish_assessment(self.teacher, self.work.pk)
        self.attempt = services.start_attempt(self.student, self.work.pk)
        self.client.force_login(self.student)

    def submission(self, kind="judge", status="accepted", result=None):
        item = services.submit_code(self.student, self.attempt.pk, self.problems[0].pk, "int main(){}", kind)
        Submission.objects.filter(pk=item.pk).update(status=status, result=result or {})
        return item

    def test_workspace_uses_local_editor_and_accessible_unique_panels(self):
        response = self.client.get(f"/attempts/{self.attempt.pk}/")
        for path in ["/static/editor/editor.js", "/static/editor/editor.css", "/static/workspace.css", "/static/workspace.js"]:
            self.assertContains(response, path)
        self.assertContains(response, 'class="workspace-page"')
        self.assertContains(response, 'id="task-select"')
        self.assertContains(response, 'id="finish-dialog"')
        self.assertContains(response, 'id="theory-panel"')
        self.assertNotContains(response, "PRIVATE_")
        markup = Markup(response.content.decode())
        self.assertEqual(len(markup.ids), len(set(markup.ids)))
        self.assertTrue(set(markup.controls).issubset(markup.ids))
        self.assertEqual(len(response.context["problems"]), 2)

    def test_polling_metadata_does_not_repeat_output_but_detail_has_public_result(self):
        item = self.submission(kind="sample", result={"samples": [{"label":"Example", "stdout":"<script>literal</script>", "stderr":"", "status":"accepted"}]})
        response = self.client.get(f"/attempts/{self.attempt.pk}/state/")
        row = response.json()["submissions"][0]
        self.assertNotIn("result", row)
        self.assertEqual(row["problem_title"], "Sum")
        self.assertEqual(row["kind_code"], "sample")
        detail = self.client.get(f"/submissions/{item.pk}/state/").json()["submission"]
        self.assertEqual(detail["result"]["samples"][0]["stdout"], "<script>literal</script>")
        page = self.client.get(f"/attempts/{self.attempt.pk}/")
        self.assertNotContains(page, "<script>literal</script>")
        self.assertNotIn("result", page.context["submissions"][0])

    def test_result_detail_requires_owner_or_teacher(self):
        item = self.submission(result={"compiler":"my compiler message"})
        url = f"/submissions/{item.pk}/state/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_deferred_result_detail_preserves_embargo(self):
        self.work.reveal_after_close = True
        self.work.save()
        services.publish_assessment(self.teacher, self.work.pk)
        user = User.objects.create_user("deferred")
        user.groups.set(self.student.groups.all())
        attempt = services.start_attempt(user, self.work.pk)
        item = services.submit_code(user, attempt.pk, self.problems[0].pk, "int main(){}", "judge")
        Submission.objects.filter(pk=item.pk).update(status="compile_error", result={"compiler":"PRIVATE_COMPILER"})
        self.client.force_login(user)
        for url in [f"/attempts/{attempt.pk}/", f"/attempts/{attempt.pk}/state/", f"/submissions/{item.pk}/state/"]:
            response = self.client.get(url)
            self.assertNotContains(response, "PRIVATE_")
            self.assertNotContains(response, "compile_error")
        detail = self.client.get(f"/submissions/{item.pk}/state/").json()["submission"]
        self.assertEqual(detail["status"], "received")
        self.assertEqual(detail["result"], {})
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get(f"/submissions/{item.pk}/state/"), "PRIVATE_COMPILER")

    def test_finished_attempt_has_readonly_workbench(self):
        Attempt.objects.filter(pk=self.attempt.pk).update(finished_at=timezone.now())
        response = self.client.get(f"/attempts/{self.attempt.pk}/")
        self.assertContains(response, 'data-can-edit="false"')
        self.assertNotContains(response, 'class="primary judge-run"')
        self.assertFalse(response.context["active"])
        self.assertFalse(response.context["can_edit"])

    def test_teacher_inspection_does_not_enable_student_controls(self):
        self.client.force_login(self.teacher)
        response = self.client.get(f"/attempts/{self.attempt.pk}/")
        self.assertFalse(response.context["can_edit"])
        self.assertNotContains(response, 'id="finish-button"')

    def test_workspace_without_theory_still_has_complete_tabs(self):
        # A new publication excludes the source question; the current attempt keeps its version.
        self.work.questions.all().delete()
        services.publish_assessment(self.teacher, self.work.pk)
        self.other.groups.set(self.student.groups.all())
        attempt = services.start_attempt(self.other, self.work.pk)
        self.client.force_login(self.other)
        response = self.client.get(f"/attempts/{attempt.pk}/")
        self.assertNotContains(response, 'id="theory-tab"')
        self.assertContains(response, 'id="statement-tab"')
        self.assertContains(response, 'id="result-tab"')
