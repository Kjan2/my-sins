from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from lab import services
from lab.models import (Assessment, AssessmentProblem, Course, Problem, Submission,
                        TestCase as Case, TheoryQuestion, WorkerState)
from lab.reference_checks import queue_reference_check
from lab.student_import import import_students
from . import test_publication, test_workflow


@override_settings(SECURE_SSL_REDIRECT=False)
class CourseAccessTests(TestCase):
    def setUp(self):
        test_workflow.WorkflowTests.setUp(self)
        self.rule = self.work.problem_rules.get()
        self.colleague = User.objects.create_user("colleague", is_staff=True)
        self.admin = User.objects.create_superuser("admin", password="a-strong-local-password")
        self.foreign_student = User.objects.create_user("foreign-student", first_name="FOREIGN_STUDENT")
        self.foreign_group = Group.objects.create(name="FOREIGN_GROUP")
        self.foreign_student.groups.add(self.foreign_group)
        self.foreign_course = Course.objects.create(title="FOREIGN_COURSE")
        self.foreign_course.teachers.add(self.colleague)
        self.foreign_course.groups.add(self.foreign_group)
        self.foreign_problem = Problem.objects.create(course=self.foreign_course, title="FOREIGN_PROBLEM",
            statement="FOREIGN_STATEMENT", reference_source="int main(){}")
        Case.objects.create(problem=self.foreign_problem, label="hidden", expected="0")
        now = timezone.now()
        self.foreign_work = Assessment.objects.create(course=self.foreign_course, title="FOREIGN_WORK",
            opens_at=now-timedelta(hours=1), closes_at=now+timedelta(hours=1))
        self.foreign_work.groups.add(self.foreign_group)
        AssessmentProblem.objects.create(assessment=self.foreign_work, problem=self.foreign_problem)
        TheoryQuestion.objects.create(assessment=self.foreign_work, problem=self.foreign_problem, text="FOREIGN_QUESTION")
        # The reference is optional; omit it for this publication, then restore it.
        self.foreign_problem.reference_source = ""
        self.foreign_problem.save()
        services.publish_assessment(self.colleague, self.foreign_work.pk)
        self.foreign_work.refresh_from_db()
        self.foreign_problem.reference_source = "int main(){}"
        self.foreign_problem.save()
        self.foreign_attempt = services.start_attempt(self.foreign_student, self.foreign_work.pk)
        self.foreign_answer = self.foreign_attempt.answers.get()
        self.own_submission = services.submit_code(self.student, self.attempt.pk, self.problem.pk, "own code", "judge")
        self.foreign_submission = services.submit_code(self.foreign_student, self.foreign_attempt.pk,
            self.foreign_problem.pk, "FOREIGN_SOURCE", "judge")
        Submission.objects.update(status="infra_error", finished_at=now)
        self.client.force_login(self.teacher)

    def test_dashboard_and_course_filter_hide_foreign_work(self):
        for url in ("/teacher/", "/", f"/teacher/?course={self.course.pk}"):
            response = self.client.get(url)
            self.assertContains(response, self.work.title)
            self.assertNotContains(response, "FOREIGN_")
        for value in (self.foreign_course.pk, "bad", "9" * 30):
            self.assertEqual(self.client.get(f"/teacher/?course={value}").status_code, 404)

    def test_direct_urls_and_exports_deny_foreign_course(self):
        urls = [f"/teacher/assessments/{self.foreign_work.pk}/",
            f"/teacher/assessments/{self.foreign_work.pk}/?format=csv",
            f"/teacher/attempts/{self.foreign_attempt.pk}/"]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 404, url)
            self.assertEqual(self.client.post(url, {"action": "extend"}).status_code, 404, url)
        for suffix in ("", "state/", "results/", "history/"):
            self.assertEqual(self.client.get(f"/attempts/{self.foreign_attempt.pk}/{suffix}").status_code, 403)
        for suffix in ("", "state/"):
            self.assertEqual(self.client.get(f"/submissions/{self.foreign_submission.pk}/{suffix}").status_code, 403)
        self.assertEqual(self.client.post(f"/teacher/submissions/{self.foreign_submission.pk}/requeue/").status_code, 404)
        self.assertEqual(self.client.get(f"/assessments/{self.foreign_work.pk}/").status_code, 403)
        self.assertEqual(self.client.post(f"/assessments/{self.foreign_work.pk}/start/").status_code, 403)

    def test_services_enforce_scope_independently_of_views(self):
        actions = [
            lambda: services.publish_assessment(self.teacher, self.foreign_work.pk),
            lambda: services.grade_answer(self.teacher, self.foreign_answer.pk, 1, "changed"),
            lambda: services.extend_deadline(self.teacher, self.foreign_attempt.pk,
                timezone.now()+timedelta(days=1), "changed"),
            lambda: services.requeue_submission(self.teacher, self.foreign_submission.pk),
            lambda: queue_reference_check(self.teacher, self.foreign_problem.pk),
            lambda: import_students(self.teacher, b"username,password,group\nnew,Z7!mR4@qT9#vL2,G\n", self.foreign_course.pk),
        ]
        for action in actions:
            with self.assertRaises(PermissionDenied):
                action()
        self.foreign_answer.refresh_from_db()
        self.assertIsNone(self.foreign_answer.score)
        self.assertFalse(self.foreign_attempt.extensions.exists())
        self.assertFalse(self.foreign_problem.reference_checks.exists())
        self.assertFalse(User.objects.filter(username="new").exists())

    def test_membership_revocation_takes_effect_on_existing_attempts(self):
        self.foreign_course.teachers.add(self.teacher)
        self.assertEqual(self.client.get(f"/teacher/attempts/{self.foreign_attempt.pk}/").status_code, 200)
        self.foreign_course.teachers.remove(self.teacher)
        self.assertEqual(self.client.get(f"/teacher/attempts/{self.foreign_attempt.pk}/").status_code, 404)
        with self.assertRaises(PermissionDenied):
            services.accessible_attempt(self.teacher, self.foreign_attempt.pk)
        self.teacher.groups.add(self.foreign_group)
        with self.assertRaises(PermissionDenied):
            services.start_attempt(self.teacher, self.foreign_work.pk)

    def test_colleagues_and_superuser_can_manage_assigned_courses(self):
        self.course.teachers.add(self.colleague)
        self.client.force_login(self.colleague)
        self.assertEqual(self.client.get(f"/teacher/attempts/{self.attempt.pk}/").status_code, 200)
        services.grade_answer(self.colleague, self.attempt.answers.get().pk, 2, "OK")
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/teacher/"), "FOREIGN_WORK")
        self.assertEqual(self.client.get(f"/teacher/attempts/{self.foreign_attempt.pk}/").status_code, 200)
        services.grade_answer(self.admin, self.foreign_answer.pk, 2, "OK")

    def test_queue_does_not_disclose_foreign_submission_or_student(self):
        WorkerState.objects.create(run_id="test", status="running", heartbeat_at=timezone.now(),
            current_submission=self.foreign_submission, message=f"Проверка эталона задачи #{self.foreign_problem.pk}")
        response = self.client.get("/teacher/judge/state/")
        state = response.json()
        self.assertIsNone(state["current_submission"])
        self.assertEqual(state["message"], "Выполняется проверка.")
        self.assertEqual([error["id"] for error in state["errors"]], [self.own_submission.pk])
        self.assertNotIn("FOREIGN_", response.content.decode())
        Submission.objects.filter(pk=self.foreign_submission.pk).update(status="queued")
        self.assertEqual(self.client.get("/teacher/judge/state/").json()["queued"], 0)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/teacher/judge/state/").json()["queued"], 1)

    def test_admin_lists_and_direct_objects_are_scoped(self):
        for model, obj in [("course", self.foreign_course), ("problem", self.foreign_problem),
                ("assessment", self.foreign_work), ("assessmentversion", self.foreign_work.current_version),
                ("attempt", self.foreign_attempt), ("submission", self.foreign_submission),
                ("theoryanswer", self.foreign_answer)]:
            response = self.client.get(f"/admin/lab/{model}/")
            self.assertEqual(response.status_code, 200, model)
            self.assertNotContains(response, "FOREIGN_", msg_prefix=model)
            response = self.client.get(f"/admin/lab/{model}/{obj.pk}/change/", follow=True)
            self.assertNotContains(response, "FOREIGN_", msg_prefix=model)
        self.assertEqual(self.client.post(f"/admin/lab/attempt/{self.foreign_attempt.pk}/extend/", {}).status_code, 404)

    def test_admin_rejects_foreign_course_and_cross_course_relations(self):
        data = test_publication.PublicationTests.form_data(self, adding=True)
        data["title"] = "Should not exist"
        data["course"] = self.foreign_course.pk
        response = self.client.post("/admin/lab/assessment/add/", data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Assessment.objects.filter(title=data["title"]).exists())
        data = test_publication.PublicationTests.form_data(self)
        data["problem_rules-0-problem"] = self.foreign_problem.pk
        response = self.client.post(f"/admin/lab/assessment/{self.work.pk}/change/", data)
        self.assertEqual(response.status_code, 200)
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.problem_id, self.problem.pk)
        data = test_publication.PublicationTests.form_data(self)
        data["groups"] = [self.foreign_group.pk]
        self.client.post(f"/admin/lab/assessment/{self.work.pk}/change/", data)
        self.assertEqual(list(self.work.groups.all()), [self.group])
        self.assertEqual(self.work.versions.count(), 1)

    def test_admin_bulk_actions_ignore_foreign_selected_ids(self):
        response = self.client.post("/admin/lab/submission/", {"action": "requeue",
            "_selected_action": [self.own_submission.pk, self.foreign_submission.pk]})
        self.assertEqual(response.status_code, 302)
        self.own_submission.refresh_from_db()
        self.foreign_submission.refresh_from_db()
        self.assertEqual(self.own_submission.status, "queued")
        self.assertEqual(self.foreign_submission.status, "infra_error")
        response = self.client.post("/admin/lab/assessment/", {"action": "publish",
            "_selected_action": [self.foreign_work.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.foreign_work.versions.count(), 1)

    def test_add_form_selects_course_before_showing_groups_and_problems(self):
        response = self.client.get("/admin/lab/assessment/add/")
        self.assertEqual(response.status_code, 200)
        self.assertQuerySetEqual(response.context["adminform"].form.fields["groups"].queryset, [self.group])
        self.foreign_course.teachers.add(self.teacher)
        response = self.client.get("/admin/lab/assessment/add/")
        self.assertContains(response, "Выберите курс")
        response = self.client.get(f"/admin/lab/assessment/add/?course={self.course.pk}")
        self.assertQuerySetEqual(response.context["adminform"].form.fields["groups"].queryset, [self.group])
        rules = response.context["inline_admin_formsets"][0].formset
        self.assertQuerySetEqual(rules.forms[0].fields["problem"].queryset, [self.problem])
        self.foreign_course.teachers.remove(self.teacher)
        self.assertEqual(self.client.get(f"/admin/lab/assessment/add/?course={self.foreign_course.pk}").status_code, 404)

    def test_publication_rejects_cross_course_problem_and_group(self):
        self.foreign_course.teachers.add(self.teacher)
        rule = AssessmentProblem(assessment=self.work, problem=self.foreign_problem)
        with self.assertRaises(ValidationError):
            rule.full_clean()
        rule.save()  # Publication must revalidate even data inserted outside forms.
        with self.assertRaises(ValidationError):
            services.publish_assessment(self.teacher, self.work.pk)
        rule.delete()
        self.work.groups.add(self.foreign_group)
        with self.assertRaises(ValidationError):
            services.publish_assessment(self.teacher, self.work.pk)

    def test_course_creation_assigns_creator_and_cannot_grant_membership(self):
        response = self.client.post("/admin/lab/course/add/", {
            "title": "New course", "teachers": [self.colleague.pk], "_save": "1"})
        self.assertEqual(response.status_code, 302)
        course = Course.objects.get(title="New course")
        self.assertEqual(list(course.teachers.all()), [self.teacher])
        self.client.post(f"/admin/lab/course/{course.pk}/change/", {
            "title": "Renamed", "teachers": [self.colleague.pk], "groups": [self.foreign_group.pk], "_save": "1"})
        course.refresh_from_db()
        self.assertEqual(course.title, "New course")
        self.assertFalse(course.groups.exists())
        self.assertEqual(list(course.teachers.all()), [self.teacher])
        self.client.force_login(self.admin)
        response = self.client.post(f"/admin/lab/course/{course.pk}/change/", {
            "title": "Renamed", "teachers": [self.teacher.pk, self.colleague.pk], "_save": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(course.teachers.filter(pk=self.colleague.pk).exists())

    def test_global_accounts_and_groups_are_read_only_for_teacher(self):
        self.assertEqual(self.client.get("/admin/auth/user/add/").status_code, 403)
        self.assertEqual(self.client.post(f"/admin/auth/user/{self.student.pk}/password/", {
            "password1": "new-password", "password2": "new-password"}).status_code, 403)
        for model, obj in [("user", self.student), ("group", self.group)]:
            self.assertEqual(self.client.post(f"/admin/auth/{model}/{obj.pk}/delete/", {"post": "yes"}).status_code, 403)
        response = self.client.post(f"/admin/auth/user/{self.student.pk}/change/", {"is_staff": "on"})
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(self.client.get("/admin/auth/user/"), "FOREIGN_STUDENT")
        self.assertNotContains(self.client.get("/admin/auth/group/"), "FOREIGN_GROUP")
        self.assertNotContains(self.client.get(f"/admin/auth/user/{self.foreign_student.pk}/change/", follow=True), "FOREIGN_STUDENT")

    def test_import_assigns_course_and_rejects_foreign_groups_atomically(self):
        data = b"username,password,group\nnew-person,Z7!mR4@qT9#vL2,New course group\n"
        response = self.client.post("/teacher/students/import/", {
            "course": self.foreign_course.pk, "file": SimpleUploadedFile("students.csv", data)})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username="new-person").exists())
        foreign_group_data = data + b"second-person,Z7!mR4@qT9#vL2,FOREIGN_GROUP\n"
        with self.assertRaises(ValidationError):
            import_students(self.teacher, foreign_group_data, self.course.pk)
        self.assertFalse(User.objects.filter(username="new-person").exists())
        self.assertEqual(import_students(self.teacher, data, self.course.pk), 1)
        group = self.course.groups.get(name="New course group")
        self.assertTrue(group.user_set.filter(username="new-person").exists())
        self.assertFalse(self.foreign_course.groups.filter(pk=group.pk).exists())

    def test_demo_command_creates_accessible_course_and_is_repeatable(self):
        call_command("seed_demo", teacher=self.teacher.username, stdout=StringIO())
        call_command("seed_demo", teacher=self.teacher.username, stdout=StringIO())
        work = Assessment.objects.get(title="Демонстрация · сумма двух чисел")
        self.assertTrue(work.course.teachers.filter(pk=self.teacher.pk).exists())
        self.assertTrue(work.course.groups.filter(pk__in=work.groups.values("pk")).exists())


class CourseMigrationTests(TransactionTestCase):
    def test_existing_publication_and_attempt_survive_migration(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("lab", "0004_project_improvements")])
        self.addCleanup(lambda: MigrationExecutor(connection).migrate([("lab", "0005_courses")]))
        old = executor.loader.project_state([("lab", "0004_project_improvements")]).apps
        UserModel = old.get_model("auth", "User")
        teacher = UserModel.objects.create(username="old-teacher", is_staff=True, is_active=True)
        student = UserModel.objects.create(username="old-student")
        group = old.get_model("auth", "Group").objects.create(name="Old group")
        student.groups.add(group)
        problem = old.get_model("lab", "Problem").objects.create(title="Old problem", statement="Old text")
        now = timezone.now()
        work = old.get_model("lab", "Assessment").objects.create(title="Old work", opens_at=now, closes_at=now+timedelta(hours=1))
        work.groups.add(group)
        snapshot = {"title": "Frozen title", "problems": [{"id": problem.pk, "statement": "Frozen text"}], "group_ids": [group.pk]}
        version = old.get_model("lab", "AssessmentVersion").objects.create(assessment=work, number=1,
            snapshot=snapshot, published_by=teacher)
        old.get_model("lab", "Assessment").objects.filter(pk=work.pk).update(current_version=version)
        attempt = old.get_model("lab", "Attempt").objects.create(student=student, assessment=work,
            version=version, started_at=now, deadline=now+timedelta(minutes=30))
        executor = MigrationExecutor(connection)
        executor.migrate([("lab", "0005_courses")])
        migrated = Assessment.objects.get(pk=work.pk)
        self.assertEqual(migrated.current_version.snapshot, snapshot)
        self.assertEqual(migrated.attempts.get().pk, attempt.pk)
        self.assertEqual(migrated.attempts.get().deadline, attempt.deadline)
        self.assertEqual(Problem.objects.get(pk=problem.pk).course_id, migrated.course_id)
        self.assertEqual(list(migrated.course.teachers.values_list("pk", flat=True)), [teacher.pk])
        self.assertEqual(list(migrated.course.groups.values_list("pk", flat=True)), [group.pk])
