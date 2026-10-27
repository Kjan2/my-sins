from datetime import timedelta
from io import StringIO
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth.models import User, Group
from django.core.management import call_command, CommandError
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from judge.docker_runner import InfrastructureError
from lab import services
from lab.models import Assessment, AssessmentProblem, Problem, Submission, TestCase, WorkerState, ReferenceCheck


class WorkerTests(TransactionTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings_override=override_settings(WORKER_LOCK=str(Path(self.temp.name)/"worker.lock"))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        user=User.objects.create_user("student",is_staff=True)
        group=Group.objects.create(name="group")
        p=Problem.objects.create(title="p",statement="s")
        TestCase.objects.create(problem=p,label="hidden",expected="0")
        now=timezone.now()
        work=Assessment.objects.create(title="work",opens_at=now-timedelta(hours=1),closes_at=now+timedelta(hours=1))
        work.groups.add(group)
        AssessmentProblem.objects.create(assessment=work,problem=p)
        services.publish_assessment(user,work.pk)
        a=services.start_attempt(user,work.pk)
        self.item=services.submit_code(user,a.pk,p.pk,"int main(){}","judge")

    @patch("lab.management.commands.judge_worker.DockerJudge")
    def test_docker_runs_outside_transaction(self,runner):
        def judge(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(Submission.objects.get().status,"running")
            return {"status":"accepted","result":{}}
        runner.return_value.judge.side_effect=judge
        call_command("judge_worker",once=True,stdout=StringIO())
        self.item.refresh_from_db()
        self.assertEqual(self.item.status,"accepted")
        self.assertIsNotNone(self.item.finished_at)
        self.assertEqual(runner.return_value.cleanup_stale.call_count,2)
        self.assertEqual(WorkerState.objects.get().status, "stopped")

    @patch("lab.management.commands.judge_worker.DockerJudge")
    def test_restart_marks_interrupted_job_infra(self,runner):
        Submission.objects.filter(pk=self.item.pk).update(status="running")
        call_command("judge_worker",once=True,stdout=StringIO())
        self.item.refresh_from_db()
        self.assertEqual(self.item.status,"infra_error")
        runner.return_value.judge.assert_not_called()

    @patch("lab.management.commands.judge_worker.DockerJudge")
    def test_custom_input_passed_to_runner(self, runner):
        Submission.objects.filter(pk=self.item.pk).update(kind="custom", custom_input="1 2")
        runner.return_value.judge.return_value = {"status":"accepted", "result":{"samples":[{"stdout":"3", "stderr":""}]}}
        call_command("judge_worker", once=True, stdout=StringIO())
        self.assertEqual(runner.return_value.judge.call_args.kwargs["custom_input"], "1 2")

    @patch("lab.management.commands.judge_worker.DockerJudge")
    def test_heartbeat_while_runner_is_busy(self, runner):
        import time
        from lab.worker_monitor import WorkerMonitor
        def run(*args, **kwargs):
            before = WorkerState.objects.get()
            self.assertEqual(before.status, "running")
            self.assertEqual(before.current_submission_id, self.item.pk)
            limit = time.monotonic() + 3
            while time.monotonic() < limit:
                time.sleep(.02)
                if WorkerState.objects.get().heartbeat_at > before.heartbeat_at:
                    return {"status":"accepted", "result":{}}
            self.fail("Heartbeat did not update during execution")
        runner.return_value.judge.side_effect = run
        with patch.object(WorkerMonitor, "interval", .02):
            call_command("judge_worker", once=True, stdout=StringIO())

    @patch("lab.management.commands.judge_worker.DockerJudge")
    def test_failed_cleanup_records_error(self, runner):
        runner.return_value.cleanup_stale.side_effect = InfrastructureError("unavailable")
        with self.assertRaises(CommandError):
            call_command("judge_worker", once=True, stdout=StringIO())
        state = WorkerState.objects.get()
        self.assertEqual(state.status, "error")
        self.assertIn("Очистка", state.message)

    @patch("lab.management.commands.judge_worker.DockerJudge")
    def test_final_cleanup_failure_is_not_successful_stop(self, runner):
        runner.return_value.cleanup_stale.side_effect = [None, InfrastructureError("unavailable")]
        runner.return_value.judge.return_value = {"status":"accepted", "result":{}}
        with self.assertRaises(CommandError):
            call_command("judge_worker", once=True, stdout=StringIO())
        self.assertEqual(WorkerState.objects.get().status, "error")

    @patch("lab.management.commands.judge_worker.DockerJudge")
    def test_stop_during_judging_marks_current_job_interrupted(self, runner):
        from lab.management.commands.judge_worker import WorkerStopped
        runner.return_value.judge.side_effect = WorkerStopped()
        call_command("judge_worker", once=True, stdout=StringIO())
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "infra_error")
        self.assertEqual(WorkerState.objects.get().status, "stopped")
        self.assertIsNone(WorkerState.objects.get().current_submission_id)

    @patch("lab.management.commands.judge_worker.DockerJudge")
    def test_docker_unavailable_leaves_queue_and_stops(self,runner):
        runner.return_value.cleanup_stale.side_effect=InfrastructureError("unavailable")
        with self.assertRaises(CommandError): call_command("judge_worker",once=True,stdout=StringIO())
        self.item.refresh_from_db()
        self.assertEqual(self.item.status,"queued")
        runner.return_value.judge.assert_not_called()

    @patch("lab.management.commands.judge_worker.DockerJudge")
    def test_reference_uses_same_worker_outside_transaction_and_student_has_priority(self, runner):
        from lab.reference_checks import queue_reference_check
        problem = Problem.objects.get()
        problem.reference_source = "reference-code"
        problem.save()
        item = queue_reference_check(User.objects.get(), problem.pk)
        runner.return_value.judge.return_value = {"status": "accepted", "result": {}}
        call_command("judge_worker", once=True, stdout=StringIO())
        item.refresh_from_db()
        self.assertEqual(item.status, "queued")
        def execute(source, payload, kind):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(source, "reference-code")
            self.assertEqual(payload["tests"][0]["expected"], "0")
            self.assertEqual(kind, "judge")
            return {"status": "accepted", "result": {}}
        runner.return_value.judge.side_effect = execute
        call_command("judge_worker", once=True, stdout=StringIO())
        item.refresh_from_db()
        self.assertEqual(item.status, "accepted")

    @patch("lab.management.commands.judge_worker.DockerJudge")
    def test_reference_restart_marks_job_interrupted(self, runner):
        ReferenceCheck.objects.create(problem=Problem.objects.get(), actor=User.objects.get(),
            fingerprint="a" * 64, payload={}, status="running")
        Submission.objects.all().delete()
        call_command("judge_worker", once=True, stdout=StringIO())
        self.assertEqual(ReferenceCheck.objects.get().status, "infra_error")
        runner.return_value.judge.assert_not_called()
