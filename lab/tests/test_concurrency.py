from lab.models import Course
"""Run on a file-backed test DB; in-memory SQLite does not honor lock waits identically."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
import time
from uuid import uuid4
from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, Client, override_settings
from django.utils import timezone
from lab.models import Assessment, AssessmentProblem, Attempt, Problem, Submission, TestCase
from lab.services import claim_submission, publish_assessment, start_attempt, submit_code


@override_settings(SECURE_SSL_REDIRECT=False)
class ConcurrentWorkflowTests(TransactionTestCase):
    def setUp(self):
        name = connection.settings_dict["NAME"]
        if str(name) == ":memory:" or "mode=memory" in str(name):
            self.skipTest("Set TEST_DATABASE_PATH=/tmp/contest-tests.sqlite3 for real SQLite concurrency")
        self.teacher=User.objects.create_user("teacher",is_staff=True)
        self.group=Group.objects.create(name="group")
        self.course = Course.objects.create(title="Course")
        self.course.teachers.add(self.teacher)
        self.course.groups.add(self.group)
        self.problem=Problem.objects.create(course=self.course, title="sum",statement="sum")
        TestCase.objects.create(problem=self.problem,label="hidden",expected="0")
        now=timezone.now()
        self.work=Assessment.objects.create(course=self.course, title="load",opens_at=now-timedelta(hours=1),closes_at=now+timedelta(hours=1))
        self.work.groups.add(self.group)
        AssessmentProblem.objects.create(assessment=self.work,problem=self.problem)
        publish_assessment(self.teacher,self.work.pk)

    def parallel(self, count, action):
        barrier=threading.Barrier(count)
        def invoke(index):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                return action(index)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=count) as executor:
            return list(executor.map(invoke,range(count)))

    @override_settings(LOGIN_ACCOUNT_LIMIT=8, LOGIN_IP_LIMIT=100)
    def test_parallel_login_attempts_share_atomic_limit(self):
        responses = self.parallel(12, lambda _: Client().post("/accounts/login/",
            {"username": "same-unknown-account", "password": "wrong"}).status_code)
        self.assertEqual(responses.count(200), 8)
        self.assertEqual(responses.count(429), 4)

    def test_thirty_students_start_and_submit_over_http(self):
        clients=[]
        for n in range(30):
            user=User.objects.create_user(f"student{n}")
            user.groups.add(self.group)
            client=Client()
            client.force_login(user)
            clients.append(client)
        starts=self.parallel(30,lambda i: clients[i].post(f"/assessments/{self.work.pk}/start/").status_code)
        self.assertEqual(starts,[302]*30)
        attempts=list(Attempt.objects.order_by("student_id"))
        self.assertEqual(len(attempts),30)
        submits=self.parallel(30,lambda i: clients[i].post(f"/attempts/{attempts[i].pk}/submit/",{"problem":self.problem.pk,"source":"int main(){}","kind":"judge","request_id":str(uuid4()),"expected_revision":0},content_type="application/json").status_code)
        self.assertEqual(submits,[201]*30)
        self.assertEqual(Submission.objects.count(),30)
        claimed=self.parallel(30,lambda _:claim_submission().pk)
        self.assertEqual(len(set(claimed)),30)
        self.assertEqual(Submission.objects.filter(status="running").count(),30)
        self.assertIsNone(claim_submission())

    def test_parallel_duplicate_start_and_queue_limit(self):
        user=User.objects.create_user("single")
        user.groups.add(self.group)
        ids=self.parallel(12,lambda _:start_attempt(User.objects.get(pk=user.pk),self.work.pk).pk)
        self.assertEqual(len(set(ids)),1)
        def submit(index):
            try:
                submit_code(User.objects.get(pk=user.pk),ids[0],self.problem.pk,"int main(){}","custom" if index % 2 else "judge")
                return True
            except ValidationError:
                return False
        accepted=self.parallel(12,submit)
        self.assertEqual(sum(accepted),3)
        self.assertEqual(Submission.objects.count(),3)

    def test_parallel_submission_budget(self):
        self.work.submission_limit=1
        self.work.save()
        publish_assessment(self.teacher,self.work.pk)
        user=User.objects.create_user("single")
        user.groups.add(self.group)
        attempt=start_attempt(user,self.work.pk)
        def submit(_):
            try:
                submit_code(User.objects.get(pk=user.pk),attempt.pk,self.problem.pk,"int main(){}","judge")
                return True
            except ValidationError:
                return False
        self.assertEqual(sum(self.parallel(10,submit)),1)

    def test_repeated_submission_request_creates_one_job(self):
        user = User.objects.create_user("repeat")
        user.groups.add(self.group)
        attempt = start_attempt(user, self.work.pk)
        request_id = uuid4()
        def submit(_):
            return submit_code(User.objects.get(pk=user.pk), attempt.pk, str(self.problem.pk),
                               "int main(){}", "judge", request_id=request_id, expected_revision=0).pk
        ids = self.parallel(12, submit)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(Submission.objects.count(), 1)

    def test_two_stale_editors_cannot_both_write(self):
        user = User.objects.create_user("edit")
        user.groups.add(self.group)
        attempt = start_attempt(user, self.work.pk)
        from lab.services import save_draft, Conflict
        def save(index):
            try:
                save_draft(User.objects.get(pk=user.pk), attempt.pk, self.problem.pk,
                           f"draft from tab {index}", expected_revision=0)
                return True
            except Conflict:
                return False
        self.assertEqual(sum(self.parallel(8, save)), 1)

    def test_request_waiting_for_database_cannot_cross_deadline(self):
        user=User.objects.create_user("single")
        user.groups.add(self.group)
        attempt=start_attempt(user,self.work.pk)
        Attempt.objects.filter(pk=attempt.pk).update(deadline=timezone.now()+timedelta(seconds=.5))
        started=threading.Event()
        def submit():
            close_old_connections()
            try:
                started.set()
                with self.assertRaises(ValidationError):
                    submit_code(User.objects.get(pk=user.pk),attempt.pk,self.problem.pk,"int main(){}","judge")
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                future=executor.submit(submit)
                self.assertTrue(started.wait(timeout=5))
                time.sleep(.7)
            future.result(timeout=10)
        self.assertEqual(Submission.objects.count(),0)
