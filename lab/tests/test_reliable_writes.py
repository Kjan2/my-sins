from datetime import timedelta
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from lab import services
from lab.models import Attempt, Draft, Submission, TheoryAnswer
from . import test_workflow


@override_settings(SECURE_SSL_REDIRECT=False)
class ReliableWriteTests(TestCase):
    setUp = test_workflow.WorkflowTests.setUp

    def post(self, route, data):
        return self.client.post(f"/attempts/{self.attempt.pk}/{route}/", data, content_type="application/json")

    def code(self, text="first", revision=0):
        return {"type": "code", "problem": self.problem.pk, "source": text, "expected_revision": revision}

    def theory(self, text="first", revision=0, submission=None):
        return {"type": "theory", "question": self.question.pk, "text": text,
                "submission": submission, "expected_revision": revision}

    def submission(self, kind="judge"):
        return {**self.code("int main(){}"), "kind": kind, "request_id": str(uuid4())}

    def test_stale_code_is_rejected_with_owner_only_server_text(self):
        self.assertEqual(self.post("save", self.code()).json()["revision"], 1)
        response = self.post("save", self.code("stale text"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["conflict"], {"type": "code", "problem": self.problem.pk,
                                                       "source": "first", "revision": 1})
        self.assertEqual(Draft.objects.get().source, "first")
        self.client.force_login(self.other)
        response = self.post("save", self.code("foreign"))
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "first", status_code=403)

    def test_same_save_can_be_retried_and_explicit_resolution_increments_revision(self):
        self.post("save", self.code())
        self.assertEqual(self.post("save", self.code()).json()["revision"], 1)
        self.assertEqual(self.post("save", self.code("resolved", 1)).json()["revision"], 2)
        self.assertEqual(Draft.objects.get().source, "resolved")

    def test_missing_null_boolean_negative_and_string_revision_rejected(self):
        for value in (None, True, -1, "0"):
            data = self.code()
            data["expected_revision"] = value
            self.assertEqual(self.post("save", data).status_code, 400)
        data.pop("expected_revision")
        self.assertEqual(self.post("save", data).status_code, 400)
        self.assertFalse(Draft.objects.exists())

    def test_stale_theory_does_not_erase_answer_or_grade(self):
        self.assertEqual(self.post("save", self.theory()).json()["revision"], 1)
        answer = TheoryAnswer.objects.get()
        services.grade_answer(self.teacher, answer.pk, 4, "Reviewed", answer.updated_at)
        response = self.post("save", self.theory("stale answer"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["conflict"]["text"], "first")
        self.assertNotIn("score", response.json()["conflict"])
        answer.refresh_from_db()
        self.assertEqual((answer.text, answer.score, answer.revision), ("first", 4, 1))
        self.assertEqual(self.post("save", self.theory("resolved", 1)).status_code, 200)
        answer.refresh_from_db()
        self.assertIsNone(answer.score)

    def test_link_change_is_part_of_theory_revision(self):
        submission = services.submit_code(self.student, self.attempt.pk, self.problem.pk, "code", "judge")
        self.post("save", self.theory(submission=submission.pk))
        response = self.post("save", self.theory())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["conflict"]["submission"], submission.pk)

    def test_duplicate_submission_returns_one_id_and_consumes_one_budget_slot(self):
        self.work.submission_limit = 1
        self.work.save()
        version = services.publish_assessment(self.teacher, self.work.pk)
        Attempt.objects.filter(pk=self.attempt.pk).update(version=version)
        data = self.submission()
        first = self.post("submit", data)
        second = self.post("submit", data)
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(first.json()["submission"]["id"], second.json()["submission"]["id"])
        self.assertEqual(Submission.objects.count(), 1)
        self.assertEqual(Draft.objects.get().revision, 1)

    def test_replay_after_deadline_and_finish_does_not_overwrite_new_draft(self):
        data = self.submission()
        first = self.post("submit", data)
        self.post("save", self.code("new draft", 1))
        Attempt.objects.filter(pk=self.attempt.pk).update(deadline=timezone.now() - timedelta(seconds=1), finished_at=timezone.now())
        second = self.post("submit", data)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["submission"]["id"], second.json()["submission"]["id"])
        self.assertEqual(Draft.objects.get().source, "new draft")
        fresh = self.submission()
        self.assertEqual(self.post("submit", fresh).status_code, 409)

    def test_request_id_cannot_be_reused_for_different_content_or_kind(self):
        data = self.submission()
        self.post("submit", data)
        for change in ({"source": "different"}, {"kind": "sample"}, {"custom_input": "different"}):
            self.assertEqual(self.post("submit", {**data, **change}).status_code, 400)
        self.assertEqual(Submission.objects.count(), 1)

    def test_custom_input_replay_matches_input_and_does_not_create_second_job(self):
        data = {**self.submission("custom"), "custom_input": "1 2"}
        self.assertEqual(self.post("submit", data).status_code, 201)
        self.assertEqual(self.post("submit", data).status_code, 201)
        self.assertEqual(self.post("submit", {**data, "custom_input": "3 4"}).status_code, 400)
        self.assertEqual(Submission.objects.count(), 1)

    def test_missing_or_invalid_request_id_rejected(self):
        for value in (None, "", "invalid", [], False):
            self.assertEqual(self.post("submit", {**self.submission(), "request_id": value}).status_code, 400)
        data = self.submission()
        data.pop("request_id")
        self.assertEqual(self.post("submit", data).status_code, 400)
        self.assertFalse(Submission.objects.exists())

    def test_submission_cannot_overwrite_a_newer_draft(self):
        self.post("save", self.code())
        response = self.post("submit", self.submission())
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Submission.objects.exists())
        self.assertEqual(Draft.objects.get().source, "first")

    def test_finish_conflict_rolls_back_other_fields_and_can_be_resolved(self):
        self.post("save", self.theory("newer theory"))
        data = {"confirm": True, "drafts": [self.code("final code")], "answers": [self.theory("stale theory")]}
        response = self.post("finish", data)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Draft.objects.exists())
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.finished_at)
        data["answers"][0]["expected_revision"] = 1
        self.assertEqual(self.post("finish", data).status_code, 200)
        finished = Attempt.objects.get().finished_at
        self.assertEqual(self.post("finish", data).status_code, 200)
        self.assertEqual(Attempt.objects.get().finished_at, finished)
        self.assertEqual(Draft.objects.get().revision, 1)

    def test_finish_requires_tokens_in_all_fields(self):
        data = {"confirm": True, "drafts": [self.code()], "answers": [self.theory()]}
        data["answers"][0]["expected_revision"] = None
        self.assertEqual(self.post("finish", data).status_code, 400)
        self.assertFalse(Draft.objects.exists())

    def test_initial_page_contains_current_tokens(self):
        self.post("save", self.code())
        self.post("save", self.theory())
        html = self.client.get(f"/attempts/{self.attempt.pk}/")
        self.assertContains(html, f'data-problem="{self.problem.pk}" data-revision="1"')
        self.assertContains(html, f'data-question="{self.question.pk}" data-revision="1"')
