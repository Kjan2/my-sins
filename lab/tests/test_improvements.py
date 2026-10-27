from datetime import timedelta
from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from lab import services
from lab.models import AssessmentVersion, Attempt, Draft, Problem, Submission, WorkerState
from lab.problem_content import render_text
from . import test_workflow


@override_settings(SECURE_SSL_REDIRECT=False)
class ImprovementTests(TestCase):
    setUp = test_workflow.WorkflowTests.setUp
    post = test_workflow.WorkflowTests.post

    def custom(self, text="7 8\n"):
        return services.submit_code(self.student, self.attempt.pk, self.problem.pk, "int main(){}", "custom", text)

    def test_custom_input_is_recorded_and_public_before_reveal(self):
        self.work.reveal_after_close = True
        self.work.save()
        version = services.publish_assessment(self.teacher, self.work.pk)
        Attempt.objects.filter(pk=self.attempt.pk).update(version=version)
        response = self.post("submit", {"problem": self.problem.pk, "source": "int main(){}", "kind": "custom", "custom_input": "<script>x</script>"})
        self.assertEqual(response.status_code, 201)
        item = Submission.objects.get()
        Submission.objects.filter(pk=item.pk).update(status="accepted", result={"samples": [{"stdout": "15", "stderr": ""}]})
        data = self.client.get(f"/submissions/{item.pk}/state/").json()["submission"]
        self.assertEqual(data["status_label"], "Выполнено")
        self.assertEqual(data["custom_input"], "<script>x</script>")
        self.assertEqual(data["result"]["samples"][0]["stdout"], "15")
        self.assertEqual(data["source"], "int main(){}")
        html = self.client.get(f"/submissions/{item.pk}/")
        self.assertNotContains(html, "<script>x</script>")

    def test_custom_utf8_limit_and_type(self):
        self.custom("")
        self.custom("a" * 65536)
        for value in ("я" * 32769, False, ["input"]):
            with self.assertRaises(ValidationError):
                self.custom(value)
        self.assertEqual(Submission.objects.count(), 2)
        response = self.post("submit", {"problem": self.problem.pk, "source": "s", "kind": "judge", "custom_input": None})
        self.assertEqual(response.status_code, 400)

    def test_custom_does_not_score_or_consume_judge_budget(self):
        self.work.submission_limit = 1
        self.work.save()
        version = services.publish_assessment(self.teacher, self.work.pk)
        Attempt.objects.filter(pk=self.attempt.pk).update(version=version)
        item = self.custom()
        Submission.objects.filter(pk=item.pk).update(status="accepted")
        summary = services.result_summary(Attempt.objects.get(pk=self.attempt.pk))
        self.assertEqual(summary["score"], 0)
        self.assertFalse(summary["required_ok"])
        services.submit_code(self.student, self.attempt.pk, self.problem.pk, "s", "judge")
        self.custom()
        self.custom()
        with self.assertRaises(ValidationError):
            self.custom()

    def test_custom_closed_and_foreign_access(self):
        item = self.custom()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/submissions/{item.pk}/state/").status_code, 403)
        self.assertEqual(self.post("submit", {"problem": self.problem.pk, "source": "s", "kind": "custom"}).status_code, 403)
        Attempt.objects.filter(pk=self.attempt.pk).update(deadline=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(services.Closed):
            self.custom()

    def test_markdown_safe_and_plain_compatible(self):
        text = '# Header\n\n**bold**\n\n```cpp\nint x = 1 < 2;\n```\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n![x](https://remote.test/track)'
        html = str(render_text(text, "markdown"))
        self.assertIn("<h1>Header</h1>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn('class="language-cpp"', html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn('href="data:', str(render_text('[x](data:text/html;base64,eA==)', "markdown")))
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<strong>", str(render_text("**bold**")))

    def test_content_frozen_and_starter_respects_empty_draft(self):
        self.problem.statement_format = "markdown"
        self.problem.statement = "**Frozen**"
        self.problem.input_description = "Two `int` values"
        self.problem.output_description = "Their sum"
        self.problem.constraints = "a < 100"
        self.problem.starter_code = "// starter"
        self.problem.save()
        version = services.publish_assessment(self.teacher, self.work.pk)
        Attempt.objects.filter(pk=self.attempt.pk).update(version=version)
        self.problem.statement = "changed"
        self.problem.starter_code = "// changed"
        self.problem.save()
        html = self.client.get(f"/attempts/{self.attempt.pk}/")
        self.assertContains(html, "<strong>Frozen</strong>", html=True)
        self.assertContains(html, "// starter")
        self.assertContains(html, "Входные данные")
        self.assertNotContains(html, "// changed")
        Draft.objects.create(attempt=self.attempt, problem_id=self.problem.pk, source="")
        html = self.client.get(f"/attempts/{self.attempt.pk}/")
        self.assertNotContains(html, "// starter")
        # A legacy snapshot has neither format nor starter fields.
        snapshot = dict(self.version.snapshot)
        snapshot["problems"] = [{k: v for k, v in p.items() if k not in
            ("statement_format", "starter_code", "input_description", "output_description", "constraints")} for p in snapshot["problems"]]
        legacy = AssessmentVersion.objects.create(assessment=self.work, number=99, snapshot=snapshot, published_by=self.teacher)
        Attempt.objects.filter(pk=self.attempt.pk).update(version=legacy)
        self.assertContains(self.client.get(f"/attempts/{self.attempt.pk}/"), "Add numbers")

    def test_admin_preview_uses_renderer_and_does_not_save(self):
        self.client.force_login(self.teacher)
        response = self.client.post("/admin/lab/problem/preview/", {"title":"preview", "statement":"**Hello**", "statement_format":"markdown"})
        self.assertContains(response, "<strong>Hello</strong>", html=True)
        self.assertEqual(Problem.objects.count(), 1)
        self.assertEqual(self.client.get("/admin/lab/problem/preview/").status_code, 405)

    def test_teacher_pages_and_mutations_deny_students(self):
        for url in ("/teacher/", f"/teacher/assessments/{self.work.pk}/", f"/teacher/attempts/{self.attempt.pk}/", "/teacher/judge/", "/teacher/judge/state/"):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(f"/teacher/attempts/{self.attempt.pk}/", {"action":"extend"}).status_code, 403)

    def test_gradebook_not_started_removed_group_and_old_versions(self):
        self.client.force_login(self.teacher)
        self.student.groups.clear()
        response = self.client.get(f"/teacher/assessments/{self.work.pk}/")
        self.assertContains(response, "student")
        self.assertContains(response, "other")
        self.assertContains(response, "Не начал")
        new = services.publish_assessment(self.teacher, self.work.pk)
        response = self.client.get(f"/teacher/assessments/{self.work.pk}/")
        self.assertNotContains(response, f'/teacher/attempts/{self.attempt.pk}/')
        response = self.client.get(f"/teacher/assessments/{self.work.pk}/?version={self.version.pk}")
        self.assertContains(response, f'/teacher/attempts/{self.attempt.pk}/')
        self.assertNotContains(response, "Не начал")
        response = self.client.get(f"/teacher/assessments/{self.work.pk}/?version={self.version.pk}&group={self.group.pk}")
        self.assertNotContains(response, f'/teacher/attempts/{self.attempt.pk}/')
        self.assertEqual(self.client.get(f"/teacher/assessments/{self.work.pk}/?version=99999").status_code, 404)

    def grade_data(self, answer, score=3, feedback="Comment"):
        prefix = f"q{self.question.pk}"
        return {"action":"grade", "question":self.question.pk, f"{prefix}-score":score,
                f"{prefix}-feedback":feedback, f"{prefix}-expected_updated_at":answer.updated_at.isoformat()}

    def test_teacher_grade_and_stale_form_preserves_feedback(self):
        self.client.force_login(self.teacher)
        answer = self.attempt.answers.get()
        url = f"/teacher/attempts/{self.attempt.pk}/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, self.grade_data(answer)).status_code, 302)
        answer.refresh_from_db()
        self.assertEqual(answer.score, 3)
        self.assertEqual(answer.graded_by, self.teacher)
        stale_data = self.grade_data(answer, feedback="Keep this comment")
        services.save_answer(self.student, self.attempt.pk, self.question.pk, "Changed")
        response = self.client.post(url, stale_data)
        self.assertContains(response, "Keep this comment", status_code=400)
        self.assertContains(response, "Студент изменил ответ", status_code=400)
        answer.refresh_from_db()
        self.assertIsNone(answer.score)
        self.assertEqual(self.client.post(url, self.grade_data(answer, score=999)).status_code, 400)

    def test_missing_concurrency_token_rejected(self):
        self.client.force_login(self.teacher)
        answer = self.attempt.answers.get()
        data = self.grade_data(answer)
        del data[f"q{self.question.pk}-expected_updated_at"]
        self.assertEqual(self.client.post(f"/teacher/attempts/{self.attempt.pk}/", data).status_code, 400)
        answer.refresh_from_db()
        self.assertIsNone(answer.score)

    def test_teacher_extension_audit_and_finished_rejection(self):
        self.client.force_login(self.teacher)
        url = f"/teacher/attempts/{self.attempt.pk}/"
        data = {"action":"extend", "deadline":(self.attempt.deadline + timedelta(hours=1)).isoformat(), "reason":"Network outage"}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.attempt.extensions.get().actor, self.teacher)
        Attempt.objects.filter(pk=self.attempt.pk).update(finished_at=timezone.now())
        self.assertEqual(self.client.post(url, data).status_code, 400)
        self.assertEqual(self.attempt.extensions.count(), 1)

    def test_monitor_staleness_and_requeue(self):
        self.client.force_login(self.teacher)
        state = WorkerState.objects.create(run_id="r", status="running", heartbeat_at=timezone.now()-timedelta(seconds=31))
        data = self.client.get("/teacher/judge/state/").json()
        self.assertEqual(data["worker_label"], "Нет свежего сигнала")
        self.assertEqual(self.client.get("/teacher/judge/").status_code, 200)
        item = self.custom()
        Submission.objects.filter(pk=item.pk).update(status="infra_error", finished_at=timezone.now())
        data = self.client.get("/teacher/judge/state/").json()
        self.assertEqual(data["errors"][0]["id"], item.pk)
        self.assertEqual(self.client.get(f"/teacher/submissions/{item.pk}/requeue/").status_code, 405)
        self.assertEqual(self.client.post(f"/teacher/submissions/{item.pk}/requeue/").status_code, 302)
        self.assertEqual(Submission.objects.get(pk=item.pk).status, "queued")
        state.status = "error"
        state.message = "Cleanup failed"
        state.save()
        data = self.client.get("/teacher/judge/state/").json()
        self.assertEqual(data["worker_label"], "Остановлен с ошибкой")
        self.assertEqual(data["message"], "Cleanup failed")

    def test_teacher_posts_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.teacher)
        self.assertEqual(client.post(f"/teacher/attempts/{self.attempt.pk}/", {"action":"extend"}).status_code, 403)

    def test_gradebook_query_count_does_not_grow_with_rows(self):
        self.client.force_login(self.teacher)
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        url = f"/teacher/assessments/{self.work.pk}/"
        with CaptureQueriesContext(connection) as before:
            self.client.get(url)
        services.start_attempt(self.other, self.work.pk)
        with CaptureQueriesContext(connection) as after:
            self.client.get(url)
        self.assertEqual(len(after), len(before))
