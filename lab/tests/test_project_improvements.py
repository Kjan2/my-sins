from datetime import timedelta
from io import StringIO
import csv
import json
import tempfile
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from lab import services
from lab.models import GradeChange, LoginRateBucket, ReferenceCheck, Submission, TestCase as Case, TheoryAnswer, WorkerState
from lab.reference_checks import queue_reference_check
from lab.student_import import import_students
from . import test_workflow


@override_settings(SECURE_SSL_REDIRECT=False)
class ImprovementTests(TestCase):
    setUp = test_workflow.WorkflowTests.setUp

    def test_testcases_validate_bytes_and_publication_revalidates(self):
        for field in ("input", "expected"):
            item = Case(problem=self.problem, label="utf8", **{field: "я" * 32768})
            item.full_clean()
            setattr(item, field, "я" * 32769)
            with self.assertRaises(ValidationError):
                item.full_clean()
        self.problem.tests.filter(is_sample=False).update(expected="я" * 40000)
        with self.assertRaises(ValidationError):
            services.publish_assessment(self.teacher, self.work.pk)

    def test_grade_history_preserves_changes_removal_and_answer_reset(self):
        answer = services.save_answer(self.student, self.attempt.pk, self.question.pk, "Original")
        services.grade_answer(self.teacher, answer.pk, 4, "First", answer.updated_at)
        answer.refresh_from_db()
        with self.assertRaises(ValidationError):
            services.grade_answer(self.teacher, answer.pk, 999, "Invalid", answer.updated_at)
        services.grade_answer(self.teacher, answer.pk, None, "Removed", answer.updated_at)
        answer.refresh_from_db()
        services.grade_answer(self.teacher, answer.pk, 3, "Regraded", answer.updated_at)
        services.save_answer(self.student, self.attempt.pk, self.question.pk, "New text")
        changes = list(answer.grade_changes.order_by("pk"))
        self.assertEqual([(c.old_score, c.new_score) for c in changes], [(None, 4), (4, None), (None, 3), (3, None)])
        self.assertEqual(changes[-1].actor, self.student)
        self.assertEqual(changes[-1].answer_text, "Original")
        self.assertEqual(changes[-1].reason, "answer_changed")
        self.assertEqual(changes[-1].old_feedback, "Regraded")
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get(f"/teacher/attempts/{self.attempt.pk}/"), "История оценок")
        self.assertEqual(self.client.post(f"/admin/lab/gradechange/{changes[0].pk}/change/", {}).status_code, 403)

    def test_stale_grade_creates_no_history(self):
        answer = TheoryAnswer.objects.get(attempt=self.attempt)
        with self.assertRaises(ValidationError):
            services.grade_answer(self.teacher, answer.pk, 1, "stale", answer.updated_at - timedelta(seconds=1))
        self.assertFalse(GradeChange.objects.exists())

    def test_migration_preserves_existing_grade_as_baseline(self):
        from importlib import import_module
        from types import SimpleNamespace
        from django.apps import apps
        answer = TheoryAnswer.objects.get(attempt=self.attempt)
        TheoryAnswer.objects.filter(pk=answer.pk).update(score=2, feedback="Existing", graded_by=self.teacher)
        migration = import_module("lab.migrations.0004_project_improvements")
        migration.preserve_current_grades(apps, SimpleNamespace(connection=connection))
        change = GradeChange.objects.get(answer=answer)
        self.assertEqual(change.reason, "baseline")
        self.assertEqual(change.new_score, 2)
        self.assertEqual(change.actor, self.teacher)

    def test_page_and_state_do_not_fetch_submission_bodies(self):
        Submission.objects.bulk_create([Submission(attempt=self.attempt, problem_id=self.problem.pk,
            source="private-large-body", result={"private": "large"}, kind="judge") for _ in range(65)])
        for suffix in ("", "state/"):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(f"/attempts/{self.attempt.pk}/{suffix}")
            self.assertEqual(response.status_code, 200)
            for query in queries:
                if 'FROM "lab_submission"' in query["sql"]:
                    self.assertNotIn('"lab_submission"."source"', query["sql"])
                    self.assertNotIn('"lab_submission"."result"', query["sql"])
        state = self.client.get(f"/attempts/{self.attempt.pk}/state/").json()
        self.assertEqual(len(state["submissions"]), 50)
        self.assertEqual(state["task_progress"][str(self.problem.pk)], "Отправлено")

    def test_history_pages_retain_privacy_and_enforce_ownership(self):
        Submission.objects.bulk_create([Submission(attempt=self.attempt, problem_id=self.problem.pk,
            source="code", kind="judge", status="accepted") for _ in range(55)])
        # Snapshot immutability is intentional; create a fresh published version instead.
        self.work.reveal_after_close = True
        self.work.save()
        services.publish_assessment(self.teacher, self.work.pk)
        hidden = services.start_attempt(self.other, self.work.pk)
        Submission.objects.create(attempt=hidden, problem_id=self.problem.pk, source="code", kind="judge", status="wrong_answer")
        response = self.client.get(f"/attempts/{self.attempt.pk}/history/?page=2&problem={self.problem.pk}")
        self.assertEqual(len(response.context["items"]), 5)
        self.assertEqual(self.client.get(f"/attempts/{self.attempt.pk}/history/?problem=999999").status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/attempts/{self.attempt.pk}/history/").status_code, 403)
        response = self.client.get(f"/attempts/{hidden.pk}/history/")
        self.assertContains(response, "Отправка принята")
        self.assertNotContains(response, "Неверный ответ")
        self.assertEqual(self.client.get(f"/attempts/{hidden.pk}/state/").json()["task_progress"][str(self.problem.pk)], "Отправлено")

    def test_csv_import_is_atomic_and_never_updates_existing_users(self):
        valid = "username,password,group,first_name\nnew-one,Z7!mR4@qT9#vL2,New group,Имя\n"
        invalid = valid + "student,Z7!mR4@qT9#vL2,New group,Existing\n"
        with self.assertRaises(ValidationError):
            import_students(self.teacher, invalid.encode(), self.course.pk)
        self.assertFalse(User.objects.filter(username="new-one").exists())
        self.assertEqual(import_students(self.teacher, valid.encode("utf-8-sig"), self.course.pk), 1)
        user = User.objects.get(username="new-one")
        self.assertTrue(user.check_password("Z7!mR4@qT9#vL2"))
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertEqual(list(user.groups.values_list("name", flat=True)), ["New group"])

    def test_import_rejects_bad_files_and_student_access(self):
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            import_students(self.student, b"", self.course.pk)
        for content in (b"\xff", b"username,password,group\na,x,g,extra", b"username,password,group\na,123,g", b"username,password,group,group\na,x,g,g"):
            with self.subTest(content=content), self.assertRaises(ValidationError):
                import_students(self.teacher, content, self.course.pk)
        normalized_duplicates = "username,password,group\nAlice,Z7!mR4@qT9#vL2,Group\nＡlice,Z7!mR4@qT9#vL2,Group\n"
        with self.assertRaises(ValidationError):
            import_students(self.teacher, normalized_duplicates.encode(), self.course.pk)
        self.assertFalse(User.objects.filter(username="Alice").exists())
        self.assertEqual(self.client.get("/teacher/students/import/").status_code, 403)
        self.client.force_login(self.teacher)
        response = self.client.post("/teacher/students/import/", {"course": self.course.pk, "file": SimpleUploadedFile("students.csv", b"bad")})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/teacher/students/import/", {"course": self.course.pk, "file": SimpleUploadedFile("students.csv",
            b"username,password,group\nimported-user,Z7!mR4@qT9#vL2,Imported group\n")})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username="imported-user", is_staff=False).exists())

    def test_csv_export_respects_filters_and_escapes_spreadsheet_formulas(self):
        self.student.first_name = "=1+1"
        self.student.save()
        self.client.force_login(self.teacher)
        url = f"/teacher/assessments/{self.work.pk}/?format=csv&version={self.version.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(StringIO(response.content.decode("utf-8-sig"))))
        self.assertTrue(any("'=1+1" in row for row in rows))
        self.assertIn("attachment", response["Content-Disposition"])
        response = self.client.get(url + "&group=99999")
        self.assertEqual(response.status_code, 404)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_reference_check_freezes_payload_and_invalidates_on_changes(self):
        self.problem.reference_source = "int main(){}"
        self.problem.save()
        with self.assertRaises(ValidationError):
            services.publish_assessment(self.teacher, self.work.pk)
        item = queue_reference_check(self.teacher, self.problem.pk)
        self.assertEqual(queue_reference_check(self.teacher, self.problem.pk).pk, item.pk)
        ReferenceCheck.objects.filter(pk=item.pk).update(status="accepted")
        version = services.publish_assessment(self.teacher, self.work.pk)
        self.assertNotIn("reference_source", version.snapshot["problems"][0])
        retried = queue_reference_check(self.teacher, self.problem.pk)
        with self.assertRaises(ValidationError):
            services.publish_assessment(self.teacher, self.work.pk)
        ReferenceCheck.objects.filter(pk=retried.pk).update(status="accepted")
        self.problem.tests.filter(is_sample=False).update(expected="changed")
        with self.assertRaises(ValidationError):
            services.publish_assessment(self.teacher, self.work.pk)
        item.refresh_from_db()
        self.assertEqual(item.payload["tests"][1]["expected"], "SECRET_EXPECTED")

    def test_reference_admin_action_queues_saved_source(self):
        self.client.force_login(self.teacher)
        response = self.client.get(f"/admin/lab/problem/{self.problem.pk}/change/")
        self.assertContains(response, "Сохранить и проверить эталон")
        self.assertContains(response, "Текущие тесты, лимиты и эталон ещё не проверены")
        tests = list(self.problem.tests.all())
        data = {"title": self.problem.title, "statement": self.problem.statement, "statement_format": "plain",
            "reference_source": "int main(){}", "time_limit": "2", "memory_mb": "128",
            "tests-TOTAL_FORMS": str(len(tests)), "tests-INITIAL_FORMS": str(len(tests)), "_check_reference": "1"}
        for index, test in enumerate(tests):
            data.update({f"tests-{index}-id": str(test.pk), f"tests-{index}-label": test.label,
                f"tests-{index}-input": test.input, f"tests-{index}-expected": "new-expected",
                f"tests-{index}-order": str(index)})
            if test.is_sample:
                data[f"tests-{index}-is_sample"] = "on"
        response = self.client.post(f"/admin/lab/problem/{self.problem.pk}/change/", data, follow=True)
        self.assertContains(response, "поставлена в очередь")
        item = ReferenceCheck.objects.get()
        self.assertEqual(item.payload["source"], "int main(){}")
        self.assertEqual(item.payload["tests"][1]["expected"], "new-expected")

    def test_health_check_worker_queue_and_backup(self):
        now = timezone.now()
        WorkerState.objects.create(run_id="healthy", status="idle", heartbeat_at=now)
        output = StringIO()
        call_command("check_exam", stdout=output)
        self.assertTrue(json.loads(output.getvalue())["healthy"])
        Submission.objects.create(attempt=self.attempt, problem_id=self.problem.pk, kind="judge", source="code",
                                  created_at=now-timedelta(seconds=150))
        with self.assertRaises(CommandError):
            call_command("check_exam", stdout=StringIO())
        Submission.objects.all().delete()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(CommandError):
                call_command("check_exam", backup_directory=folder, stdout=StringIO())


@override_settings(SECURE_SSL_REDIRECT=False, LOGIN_ACCOUNT_LIMIT=2, LOGIN_IP_LIMIT=40, LOGIN_WINDOW_SECONDS=900)
class LoginLimitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="a-good-local-password", is_staff=True)

    def fail_login(self, username="alice", path="/accounts/login/", client=None, **extra):
        return (client or self.client).post(path, {"username": username, "password": "wrong"}, **extra)

    def test_shared_account_limit_across_login_forms_and_addresses(self):
        self.assertEqual(self.fail_login().status_code, 200)
        self.assertEqual(self.fail_login(path="/admin/login/", REMOTE_ADDR="127.0.0.2").status_code, 200)
        response = self.fail_login(REMOTE_ADDR="127.0.0.3")
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response["Retry-After"]), 0)
        self.assertTrue(all("alice" not in key and "127.0.0" not in key for key in LoginRateBucket.objects.values_list("key", flat=True)))

    def test_success_clears_account_limit_and_expiry_restores_access(self):
        self.fail_login()
        response = self.client.post("/accounts/login/", {"username": "alice", "password": "a-good-local-password"})
        self.assertEqual(response.status_code, 302)
        self.client.logout()
        self.assertEqual(self.fail_login().status_code, 200)
        self.assertEqual(self.fail_login().status_code, 200)
        self.assertEqual(self.fail_login().status_code, 429)
        LoginRateBucket.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.fail_login().status_code, 200)

    def test_classroom_users_have_separate_account_counters(self):
        self.fail_login()
        self.fail_login()
        self.assertEqual(self.fail_login("bob").status_code, 200)

    @override_settings(LOGIN_IP_LIMIT=2)
    def test_ip_limit_cannot_be_bypassed_by_forwarded_headers(self):
        self.fail_login("one", HTTP_X_REAL_IP="1.2.3.4")
        self.fail_login("two", HTTP_X_REAL_IP="5.6.7.8")
        self.assertEqual(self.fail_login("three", HTTP_X_REAL_IP="9.10.11.12").status_code, 429)

    @override_settings(LOGIN_IP_LIMIT=1, LOGIN_TRUST_PROXY=True)
    def test_explicit_trusted_proxy_uses_real_client_address(self):
        self.fail_login("one", HTTP_X_REAL_IP="1.2.3.4")
        self.assertEqual(self.fail_login("two", HTTP_X_REAL_IP="5.6.7.8").status_code, 200)

    def test_csrf_rejection_does_not_consume_limit(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(self.fail_login(client=client).status_code, 403)
        self.assertFalse(LoginRateBucket.objects.exists())
