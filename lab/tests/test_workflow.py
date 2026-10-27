from lab.models import Course
from datetime import timedelta
from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch
from django.contrib.auth.models import User, Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from lab import services
from lab.models import Assessment, AssessmentProblem, AssessmentVersion, Attempt, DeadlineExtension, Draft, Problem, Submission, TestCase as Case, TheoryAnswer, TheoryQuestion


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkflowTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user("teacher", is_staff=True)
        self.student = User.objects.create_user("student")
        self.other = User.objects.create_user("other")
        self.group = Group.objects.create(name="Group")
        self.course = Course.objects.create(title="Course")
        self.course.teachers.add(self.teacher)
        self.course.groups.add(self.group)
        self.student.groups.add(self.group)
        self.other.groups.add(self.group)
        self.problem = Problem.objects.create(course=self.course, title="Sum", statement="Add numbers")
        Case.objects.create(problem=self.problem, label="Sample", input="1 2", expected="3", is_sample=True)
        Case.objects.create(problem=self.problem, label="SECRET_LABEL", input="SECRET_INPUT", expected="SECRET_EXPECTED")
        now = timezone.now()
        self.work = Assessment.objects.create(course=self.course, title="Exam", kind="exam", opens_at=now-timedelta(hours=1), closes_at=now+timedelta(hours=2), duration_minutes=60, pass_score=12)
        self.work.groups.add(self.group)
        AssessmentProblem.objects.create(assessment=self.work, problem=self.problem, max_score=10, required=True)
        self.question = TheoryQuestion.objects.create(assessment=self.work, problem=self.problem, text="Why?", max_score=5)
        self.version = services.publish_assessment(self.teacher, self.work.pk)
        self.attempt = services.start_attempt(self.student, self.work.pk)
        self.client.force_login(self.student)

    def post(self, route, data):
        data = deepcopy(data)
        def token(field, theory=False):
            key = "question" if theory else "problem"
            if type(field.get(key)) is int:
                model = TheoryAnswer if theory else Draft
                obj = model.objects.filter(attempt=self.attempt, **{key + "_id": field[key]}).first()
                field.setdefault("expected_revision", obj.revision if obj else 0)
        if isinstance(data, dict):
            if route in ("save", "submit"):
                token(data, data.get("type") == "theory")
            if route == "submit":
                data.setdefault("request_id", str(uuid4()))
            if route == "finish":
                for field in data.get("drafts", []): token(field)
                for field in data.get("answers", []): token(field, True)
        return self.client.post(f"/attempts/{self.attempt.pk}/{route}/", data, content_type="application/json")

    def expire(self):
        deadline = timezone.now()-timedelta(seconds=1)
        Attempt.objects.filter(pk=self.attempt.pk).update(deadline=deadline)
        self.attempt.refresh_from_db()

    def submit(self, source="int main(){}", kind="judge"):
        return services.submit_code(self.student, self.attempt.pk, self.problem.pk, source, kind)

    def finish(self):
        return services.finish_attempt(self.student, self.attempt.pk, [{"problem": self.problem.pk, "source": "draft"}], [{"question": self.question.pk, "text": "final"}])

    def test_repeated_start_and_deadline(self):
        second = services.start_attempt(self.student, self.work.pk)
        self.assertEqual(second.pk, self.attempt.pk)
        self.assertEqual(second.deadline, self.attempt.deadline)
        self.assertEqual(self.attempt.deadline, self.attempt.started_at+timedelta(minutes=60))
        self.assertEqual(Attempt.objects.count(), 1)

    def test_common_close_caps_duration(self):
        self.work.duration_minutes = 500
        self.work.save()
        services.publish_assessment(self.teacher, self.work.pk)
        item = services.start_attempt(self.other, self.work.pk)
        self.assertEqual(item.deadline, self.work.closes_at)

    def test_foreign_access_forbidden(self):
        self.client.force_login(self.other)
        urls = [f"/attempts/{self.attempt.pk}/", f"/attempts/{self.attempt.pk}/state/", f"/attempts/{self.attempt.pk}/results/"]
        submission = self.submit()
        urls.append(f"/submissions/{submission.pk}/")
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.post("save", {"type":"code", "problem":self.problem.pk, "source":"stolen"}).status_code, 403)
        self.assertEqual(self.post("submit", {"problem":self.problem.pk, "source":"stolen", "kind":"judge"}).status_code, 403)
        self.assertEqual(self.post("finish", {"confirm":True, "drafts":[], "answers":[]}).status_code, 403)
        with self.assertRaises(PermissionDenied):
            services.save_answer(self.other, self.attempt.pk, self.question.pk, "foreign")

    def test_hidden_tests_never_in_html_or_json(self):
        self.submit()
        for url in [f"/attempts/{self.attempt.pk}/", f"/attempts/{self.attempt.pk}/state/", f"/attempts/{self.attempt.pk}/results/", f"/assessments/{self.work.pk}/", "/"]:
            response = self.client.get(url)
            self.assertIn(response.status_code, (200,302))
            for secret in ("SECRET_LABEL", "SECRET_INPUT", "SECRET_EXPECTED"):
                self.assertNotContains(response, secret, status_code=response.status_code)

    def test_draft_is_not_submission_and_survives_refresh(self):
        response = self.post("save", {"type":"code", "problem":self.problem.pk, "source":"saved code"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Submission.objects.count(), 0)
        self.assertContains(self.client.get(f"/attempts/{self.attempt.pk}/"), "saved code")
        self.assertEqual(response.json()["deadline"], self.attempt.deadline.isoformat())
        self.assertIn("server_now", response.json())

    def test_expired_operations_rejected_but_existing_submission_claimed(self):
        submission = self.submit()
        self.expire()
        for action in [lambda: self.submit(), lambda: services.save_draft(self.student,self.attempt.pk,self.problem.pk,"late"), lambda: services.save_answer(self.student,self.attempt.pk,self.question.pk,"late"), self.finish]:
            with self.assertRaises(services.Closed): action()
        claimed = services.claim_submission()
        self.assertEqual(claimed.pk, submission.pk)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(self.client.get(f"/attempts/{self.attempt.pk}/results/").status_code, 200)

    def test_deadline_equality_rejected(self):
        with patch("lab.services.timezone.now", return_value=self.attempt.deadline):
            with self.assertRaises(services.Closed): self.submit()

    def test_queue_limit_includes_samples_across_works(self):
        for kind in ["sample","judge","sample"]: self.submit(kind=kind)
        with self.assertRaises(ValidationError): self.submit()
        self.assertEqual(Submission.objects.count(), 3)

    def test_submission_budget_and_infra_requeue(self):
        self.work.submission_limit = 1
        self.work.save()
        services.publish_assessment(self.teacher,self.work.pk)
        attempt = services.start_attempt(self.other,self.work.pk)
        item = services.submit_code(self.other,attempt.pk,self.problem.pk,"a","judge")
        with self.assertRaises(ValidationError): services.submit_code(self.other,attempt.pk,self.problem.pk,"b","judge")
        Submission.objects.filter(pk=item.pk).update(status="infra_error")
        self.assertEqual(services.requeue_submission(self.teacher,item.pk).pk,item.pk)
        with self.assertRaises(ValidationError): services.requeue_submission(self.teacher,item.pk)
        services.submit_code(self.other,attempt.pk,self.problem.pk,"samples","sample")

    def test_extension_audit_and_early_finish(self):
        self.expire()
        deadline = timezone.now()+timedelta(minutes=30)
        services.extend_deadline(self.teacher,self.attempt.pk,deadline,"Network outage")
        log = DeadlineExtension.objects.get()
        self.assertEqual(log.actor,self.teacher)
        self.assertEqual(log.old_deadline,self.attempt.deadline)
        self.assertEqual(log.new_deadline,deadline)
        self.submit()
        self.finish()
        with self.assertRaises(ValidationError): services.extend_deadline(self.teacher,self.attempt.pk,deadline+timedelta(hours=1),"Again")
        with self.assertRaises(PermissionDenied): services.extend_deadline(self.student,self.attempt.pk,deadline+timedelta(hours=1),"No")

    def test_final_save_is_atomic_and_explicit(self):
        response = self.post("finish", {"confirm":True, "drafts":[{"problem":self.problem.pk,"source":"new code"}], "answers":[{"question":self.question.pk,"text":"x"*20001}]})
        self.assertEqual(response.status_code,400)
        self.assertFalse(Draft.objects.exists())
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.finished_at)
        response = self.post("finish", {"confirm":True, "drafts":[{"problem":self.problem.pk,"source":"new code"}], "answers":[{"question":self.question.pk,"text":"answer"}]})
        self.assertEqual(response.status_code,200)
        self.assertEqual(Draft.objects.get().source,"new code")
        self.assertEqual(TheoryAnswer.objects.get().text,"answer")
        self.assertEqual(Submission.objects.count(),0)

    def test_finish_rejects_missing_fields(self):
        with self.assertRaises(ValidationError): services.finish_attempt(self.student,self.attempt.pk,[],[])

    def test_versions_do_not_follow_source_edits_or_deletions(self):
        self.problem.statement = "Changed"
        self.problem.save()
        Case.objects.filter(problem=self.problem).delete()
        Case.objects.create(problem=self.problem,label="new hidden",input="new",expected="new")
        self.question.text = "Changed question"
        self.question.save()
        newer = services.publish_assessment(self.teacher,self.work.pk)
        second = services.start_attempt(self.other,self.work.pk)
        self.assertEqual(second.version_id,newer.pk)
        self.assertEqual(self.attempt.snapshot["problems"][0]["statement"],"Add numbers")
        self.assertEqual(self.attempt.snapshot["questions"][0]["text"],"Why?")
        self.assertEqual(self.attempt.snapshot["problems"][0]["tests"][1]["expected"],"SECRET_EXPECTED")
        with self.assertRaises(ValidationError): self.version.save()
        with self.assertRaises(ValidationError): AssessmentVersion.objects.filter(pk=self.version.pk).update(snapshot={})
        with self.assertRaises(ValidationError): self.version.delete()
        self.question.delete()
        services.save_answer(self.student,self.attempt.pk,self.attempt.snapshot["questions"][0]["id"],"Still valid")

    def test_answer_link_and_regrade(self):
        submission = self.submit("first")
        self.submit("second")
        services.save_answer(self.student,self.attempt.pk,self.question.pk,"answer",submission.pk)
        answer = TheoryAnswer.objects.get()
        services.grade_answer(self.teacher,answer.pk,4,"Good",answer.updated_at)
        services.save_answer(self.student,self.attempt.pk,self.question.pk,"answer",submission.pk)
        answer.refresh_from_db()
        self.assertEqual(answer.score,4)
        services.save_answer(self.student,self.attempt.pk,self.question.pk,"changed",submission.pk)
        answer.refresh_from_db()
        self.assertIsNone(answer.score)
        self.assertEqual(answer.feedback,"")
        self.assertEqual(answer.submission.source,"first")
        with self.assertRaises(ValidationError): services.grade_answer(self.teacher,answer.pk,6,"Too many")
        with self.assertRaises(ValidationError): services.grade_answer(self.teacher,answer.pk,3,"Stale",answer.updated_at-timedelta(seconds=1))

    def test_foreign_or_sample_theory_link_rejected(self):
        sample = self.submit(kind="sample")
        with self.assertRaises(ValidationError): services.save_answer(self.student,self.attempt.pk,self.question.pk,"text",sample.pk)
        other = services.start_attempt(self.other,self.work.pk)
        submission = services.submit_code(self.other,other.pk,self.problem.pk,"other","judge")
        with self.assertRaises(ValidationError): services.save_answer(self.student,self.attempt.pk,self.question.pk,"text",submission.pk)

    def test_scores_best_submission_required_and_provisional(self):
        sample = self.submit(kind="sample")
        Submission.objects.filter(pk=sample.pk).update(status="accepted")
        self.assertEqual(services.result_summary(self.attempt)["score"],0)
        accepted = self.submit()
        Submission.objects.filter(pk=accepted.pk).update(status="accepted")
        later = self.submit()
        Submission.objects.filter(pk=later.pk).update(status="wrong_answer")
        self.finish()
        answer = TheoryAnswer.objects.get()
        self.assertTrue(services.result_summary(self.attempt)["provisional"])
        services.grade_answer(self.teacher,answer.pk,2,"OK")
        summary = services.result_summary(Attempt.objects.get(pk=self.attempt.pk))
        self.assertEqual(summary["score"],12)
        self.assertFalse(summary["provisional"])
        self.assertTrue(summary["passed"])
        Submission.objects.filter(pk=accepted.pk).update(status="wrong_answer")
        summary = services.result_summary(Attempt.objects.get(pk=self.attempt.pk))
        self.assertFalse(summary["required_ok"])
        self.assertFalse(summary["passed"])

    def test_deferred_results_all_routes_and_general_close(self):
        self.work.reveal_after_close=True
        self.work.save()
        services.publish_assessment(self.teacher,self.work.pk)
        other = services.start_attempt(self.other,self.work.pk)
        self.client.force_login(self.other)
        submission = services.submit_code(self.other,other.pk,self.problem.pk,"code","judge")
        Submission.objects.filter(pk=submission.pk).update(status="compile_error",result={"compiler":"PRIVATE_RESULT"})
        for url in [f"/attempts/{other.pk}/",f"/attempts/{other.pk}/state/",f"/attempts/{other.pk}/results/",f"/submissions/{submission.pk}/"]:
            response=self.client.get(url)
            self.assertNotContains(response,"PRIVATE_RESULT")
            self.assertNotContains(response,"compile_error")
            self.assertNotContains(response,"Ошибка компиляции")
        with patch("lab.services.timezone.now",return_value=other.deadline+timedelta(seconds=1)):
            self.assertFalse(services.results_visible(self.other,other))
        with patch("lab.services.timezone.now",return_value=self.work.closes_at+timedelta(seconds=1)):
            self.assertContains(self.client.get(f"/submissions/{submission.pk}/"),"PRIVATE_RESULT")
        self.assertTrue(services.results_visible(self.teacher,other))

    def test_login_csrf_and_admin_permissions(self):
        self.assertEqual(self.client.get("/admin/").status_code,302)
        self.client.force_login(self.teacher)
        for url in ["/admin/",f"/admin/lab/assessment/{self.work.pk}/change/",f"/admin/lab/theoryanswer/{TheoryAnswer.objects.get().pk}/change/"]:
            self.assertEqual(self.client.get(url).status_code,200,url)
        self.assertEqual(self.client.get("/admin/auth/user/add/").status_code, 403)
        response=self.client.get(f"/admin/auth/user/{self.student.pk}/change/")
        for field in ("is_staff","is_superuser","user_permissions"):
            self.assertNotContains(response,f'name="{field}"')
        self.client.logout()
        self.assertEqual(self.client.get("/").status_code,302)
        from django.test import Client
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.student)
        self.assertEqual(client.post(f"/attempts/{self.attempt.pk}/save/",{},content_type="application/json").status_code,403)

    def test_invalid_api_input_and_no_cache(self):
        self.assertEqual(self.post("save",[]).status_code,400)
        self.assertEqual(self.post("save",{"type":"code","problem":"bad","source":"code"}).status_code,400)
        self.assertEqual(self.post("save",{"type":"code","problem":self.problem.pk,"source":"я"*40000}).status_code,400)
        self.assertEqual(self.post("save",{"type":"code","problem":self.problem.pk,"source":"\ud800"}).status_code,400)
        self.assertIn("no-store",self.client.get("/")["Cache-Control"])
