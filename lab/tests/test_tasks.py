from datetime import timedelta
import json
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone
from lab import services, tasks
from lab.models import Assessment, AssessmentProblem, Attempt, Course, Problem, Submission, TaskAttempt, TestCase as CodeTest, ExtraAttempt


class TaskWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.teacher = User.objects.create_user('teacher', is_staff=True)
        cls.student = User.objects.create_user('student')
        cls.other = User.objects.create_user('outsider')
        cls.foreign_teacher = User.objects.create_user('other-teacher', is_staff=True)
        cls.group = Group.objects.create(name='101')
        cls.student.groups.add(cls.group)
        cls.course = Course.objects.create(title='Алгоритмы', description='Критерии $x^2$', faq='FAQ', acceptance='PRIVATE_TEACHER_CRITERIA')
        cls.course.teachers.add(cls.teacher)
        cls.course.groups.add(cls.group)
        cls.quiz = Problem.objects.create(course=cls.course, title='Тест', statement='Проверьте знания', kind='quiz',
            solution_minutes=10, attempt_limit=1, shuffle_options=True, quiz_questions=[
                {'text':'$2+2$?', 'options':['4','5'], 'correct':[0]},
                {'text':'Выберите чётные', 'options':['2','4','7'], 'correct':[0,1]}], correct_threshold=2)
        cls.code = Problem.objects.create(course=cls.course, title='Сумма', statement='Сложите числа', solution_minutes=20,
            attempt_limit=1, starter_code='int main(){}', format_policy='warning')
        CodeTest.objects.create(problem=cls.code, label='public', input='1 2', expected='3', is_sample=True)
        CodeTest.objects.create(problem=cls.code, label='PRIVATE_LABEL', input='PRIVATE_INPUT', expected='PRIVATE_EXPECTED')
        now = timezone.now()
        cls.work = Assessment.objects.create(course=cls.course, title='Зачёт', kind='exam', opens_at=now-timedelta(hours=1),
            closes_at=now+timedelta(hours=1), pass_score=20, task_workflow=True, linear=True, requires_confirmation=True)
        cls.work.groups.add(cls.group)
        AssessmentProblem.objects.create(assessment=cls.work, problem=cls.quiz, order=0, max_score=10)
        AssessmentProblem.objects.create(assessment=cls.work, problem=cls.code, order=1, max_score=10)
        services.publish_assessment(cls.teacher, cls.work.pk)

    def setUp(self):
        self.attempt = services.start_attempt(self.student, self.work.pk)

    def post(self, item, data):
        return self.client.post(f'/tasks/{item.pk}/action/', json.dumps(data), content_type='application/json')

    def pass_quiz(self):
        item = tasks.start_task(self.student, self.attempt.pk, self.quiz.pk)
        tasks.save_task(self.student, item.pk, {'revision':0,'answers':{'0':[0],'1':[0,1]}})
        return tasks.send_task(self.student, item.pk)

    def code_session(self):
        self.pass_quiz()
        return tasks.start_task(self.student, self.attempt.pk, self.code.pk)

    def test_linear_unlock_does_not_wait_for_teacher(self):
        with self.assertRaises(ValidationError):
            tasks.start_task(self.student, self.attempt.pk, self.code.pk)
        quiz = self.pass_quiz()
        self.assertEqual(quiz.status, 'accepted')
        self.assertTrue(tasks.available(self.attempt, self.code.pk))
        self.assertIsNotNone(tasks.start_task(self.student, self.attempt.pk, self.code.pk))

    def test_last_completed_quiz_attempt_counts_not_best(self):
        item = self.pass_quiz()
        tasks.give_extra(self.teacher, self.attempt.pk, self.quiz.pk, 'Дополнительная попытка')
        newer = tasks.start_task(self.student, self.attempt.pk, self.quiz.pk)
        self.assertTrue(tasks.task_passed(self.attempt, self.quiz.pk))
        tasks.send_task(self.student, newer.pk)
        self.assertFalse(tasks.task_passed(self.attempt, self.quiz.pk))
        self.assertEqual(tasks.completed(self.attempt, self.quiz.pk).pk, newer.pk)
        self.assertEqual(TaskAttempt.objects.get(pk=item.pk).status, 'accepted')

    def test_start_in_last_minute_keeps_full_task_duration(self):
        close = timezone.now()+timedelta(seconds=30)
        self.work.closes_at = close
        self.work.save()
        services.publish_assessment(self.teacher, self.work.pk)
        self.work.invited_students.add(self.other)
        attempt = services.start_attempt(self.other, self.work.pk)
        item = tasks.start_task(self.other, attempt.pk, self.quiz.pk)
        self.assertEqual(item.deadline, item.started_at+timedelta(minutes=10))
        self.assertGreater(item.deadline, close)
        with patch('lab.tasks.timezone.now', return_value=close+timedelta(seconds=1)):
            tasks.save_task(self.other, item.pk, {'revision':0,'answers':{'0':[0]}})
        self.assertEqual(TaskAttempt.objects.get(pk=item.pk).answers, {'0':[0]})

    def test_new_task_after_start_deadline_is_rejected(self):
        self.pass_quiz()
        with patch('lab.tasks.timezone.now', return_value=self.work.closes_at):
            with self.assertRaises(services.Closed):
                tasks.start_task(self.student, self.attempt.pk, self.code.pk)

    def test_reload_resumes_same_task_and_order(self):
        item = tasks.start_task(self.student, self.attempt.pk, self.quiz.pk)
        order, deadline = item.option_order, item.deadline
        restored = tasks.start_task(self.student, self.attempt.pk, self.quiz.pk)
        self.assertEqual(restored.pk, item.pk)
        self.assertEqual(restored.option_order, order)
        self.assertEqual(restored.deadline, deadline)

    def test_expiry_automatically_marks_last_server_answers(self):
        item = tasks.start_task(self.student, self.attempt.pk, self.quiz.pk)
        tasks.save_task(self.student, item.pk, {'revision':0,'answers':{'0':[0],'1':[0,1]}})
        with patch('lab.tasks.timezone.now', return_value=item.deadline):
            with self.assertRaises(services.Closed):
                tasks.save_task(self.student, item.pk, {'revision':1,'answers':{'0':[1]}})
        item.refresh_from_db()
        self.assertEqual(item.status, 'accepted')
        self.assertEqual(item.answers['0'], [0])
        self.assertEqual(item.finished_at, item.deadline)

    def test_expired_code_submits_saved_snapshot_only_once(self):
        item = self.code_session()
        tasks.save_task(self.student, item.pk, {'revision':0,'files':{'main.cpp':'SAVED_CODE'}})
        with patch('lab.tasks.timezone.now', return_value=item.deadline):
            tasks.expire_tasks(self.attempt)
            tasks.expire_tasks(self.attempt)
        s = Submission.objects.get(task_attempt=item)
        self.assertEqual(s.files, {'main.cpp':'SAVED_CODE'})
        self.assertEqual(s.source, 'SAVED_CODE')
        self.assertEqual(s.kind, 'judge')

    def test_submitted_files_cannot_change_and_retry_is_idempotent(self):
        item = self.code_session()
        tasks.save_task(self.student, item.pk, {'revision':0,'files':{'main.cpp':'FIRST'}})
        tasks.send_task(self.student, item.pk)
        tasks.send_task(self.student, item.pk)
        with self.assertRaises(services.Closed):
            tasks.save_task(self.student, item.pk, {'revision':1,'files':{'main.cpp':'SECOND'}})
        self.assertEqual(Submission.objects.get(task_attempt=item).files['main.cpp'], 'FIRST')

    def test_ordinary_runs_do_not_grade_or_consume_attempt(self):
        item = self.code_session()
        run = tasks.run_task(self.student, item.pk, './main', '1 2')
        build = tasks.run_task(self.student, item.pk, 'make')
        sample = tasks.run_task(self.student, item.pk, 'make test')
        self.assertEqual([run.kind, build.kind, sample.kind], ['custom','build','sample'])
        self.assertEqual(item.attempt.activity_events.filter(kind='command').count(), 3)
        self.assertFalse(tasks.task_passed(self.attempt, self.code.pk))
        self.assertEqual(TaskAttempt.objects.get(pk=item.pk).status, 'active')

    def test_terminal_disallows_arbitrary_shell_commands(self):
        item = self.code_session()
        for command in ['rm -rf /', 'make; echo secret', 'curl localhost', 'g++ main.cpp -o main && ./main']:
            with self.assertRaises(ValidationError):
                tasks.run_task(self.student, item.pk, command)
        self.assertEqual(self.attempt.submissions.count(), 0)

    def test_extra_attempt_is_specific_to_one_task(self):
        item = self.pass_quiz()
        with self.assertRaises(ValidationError):
            tasks.start_task(self.student, self.attempt.pk, self.quiz.pk)
        tasks.give_extra(self.teacher, self.attempt.pk, self.code.pk, 'Для кода')
        with self.assertRaises(ValidationError):
            tasks.start_task(self.student, self.attempt.pk, self.quiz.pk)
        self.assertEqual(ExtraAttempt.objects.get().problem_id, self.code.pk)

    def test_manual_confirmation_comment_and_revision(self):
        item = self.code_session()
        tasks.send_task(self.student, item.pk)
        s = Submission.objects.get(task_attempt=item)
        services.claim_submission()
        tasks.record_outcome(s, {'status':'accepted','result':{'tests':{'passed':2,'total':2}}})
        self.assertEqual(tasks.summary(self.attempt)['status'], 'Ожидает проверки')
        tasks.review(self.teacher, self.attempt.pk, 'revision', 'Исправьте оформление')
        self.attempt.refresh_from_db()
        self.assertEqual(tasks.summary(self.attempt)['status'], 'На доработке')
        self.client.force_login(self.student)
        self.assertContains(self.client.get(f'/attempts/{self.attempt.pk}/results/'), 'Исправьте оформление')
        tasks.review(self.teacher, self.attempt.pk, 'accepted', 'Всё верно')
        self.attempt.refresh_from_db()
        self.assertEqual(tasks.summary(self.attempt)['status'], 'Зачтено')

    def test_teacher_cannot_confirm_incomplete_work(self):
        with self.assertRaises(ValidationError):
            tasks.review(self.teacher, self.attempt.pk, 'accepted', '')

    def test_technical_error_does_not_consume_task_limit(self):
        item = self.code_session()
        tasks.send_task(self.student, item.pk)
        s = Submission.objects.get(task_attempt=item)
        services.claim_submission()
        tasks.record_outcome(s, {'status':'infra_error', 'result':{}})
        next_attempt = tasks.start_task(self.student, self.attempt.pk, self.code.pk)
        self.assertEqual(next_attempt.number, 2)
        services.requeue_submission(self.teacher, s.pk)
        item.refresh_from_db()
        self.assertEqual(item.status, 'queued')

    def test_quiz_timer_validation_and_correct_answer_required(self):
        self.quiz.question_seconds = 20
        with self.assertRaises(ValidationError):
            self.quiz.full_clean()
        self.quiz.question_seconds = None
        self.quiz.quiz_questions[0]['correct'] = []
        with self.assertRaises(ValidationError):
            self.quiz.full_clean()

    def sequential_quiz(self):
        self.quiz.allow_back = False
        self.quiz.solution_minutes = None
        self.quiz.question_seconds = 15
        self.quiz.save()
        services.publish_assessment(self.teacher, self.work.pk)
        self.work.invited_students.add(self.other)
        a = services.start_attempt(self.other, self.work.pk)
        return tasks.start_task(self.other, a.pk, self.quiz.pk)

    def test_question_clock_advances_and_locks_previous(self):
        item = self.sequential_quiz()
        with patch('lab.tasks.timezone.now', return_value=item.question_started_at+timedelta(seconds=16)):
            tasks.expire_task(item)
            item.refresh_from_db()
            self.assertEqual(item.current_question, 1)
            with self.assertRaises(ValidationError):
                tasks.save_task(self.other, item.pk, {'revision':item.revision,'answers':{'0':[0]}})
            tasks.save_task(self.other, item.pk, {'revision':item.revision,'answers':{'1':[0,1]}})
        item.refresh_from_db()
        self.assertEqual(item.answers, {'1':[0,1]})

    def test_all_question_timers_expire_when_tab_closed(self):
        item = self.sequential_quiz()
        with patch('lab.tasks.timezone.now', return_value=item.started_at+timedelta(seconds=50)):
            tasks.expire_task(item)
        item.refresh_from_db()
        self.assertEqual(item.status, 'wrong_answer')
        self.assertEqual(item.finished_at, item.started_at+timedelta(seconds=30))

    def test_stale_save_cannot_overwrite_server(self):
        item = tasks.start_task(self.student, self.attempt.pk, self.quiz.pk)
        tasks.save_task(self.student, item.pk, {'revision':0,'answers':{'0':[0]}})
        with self.assertRaises(ValidationError):
            tasks.save_task(self.student, item.pk, {'revision':0,'answers':{'0':[1]}})
        self.assertEqual(TaskAttempt.objects.get(pk=item.pk).answers, {'0':[0]})

    def test_unit_mode_accepts_only_three_named_files(self):
        self.code.check_mode = 'unit'
        self.code.task_name = 'sum'
        self.code.unit_tests = 'int main(){return 0;}'
        self.code.save()
        services.publish_assessment(self.teacher, self.work.pk)
        self.work.invited_students.add(self.other)
        a = services.start_attempt(self.other, self.work.pk)
        quiz = tasks.start_task(self.other, a.pk, self.quiz.pk)
        tasks.save_task(self.other, quiz.pk, {'revision':0,'answers':{'0':[0],'1':[0,1]}})
        tasks.send_task(self.other, quiz.pk)
        item = tasks.start_task(self.other, a.pk, self.code.pk)
        self.assertEqual(set(item.files), {'main.cpp','sum.cpp','sum.h'})
        with self.assertRaises(ValidationError):
            tasks.save_task(self.other, item.pk, {'revision':0,'files':{'../../private.cpp':'x'}})

    def test_student_cannot_open_another_students_task_or_teacher_actions(self):
        item = self.code_session()
        self.client.force_login(self.other)
        for url in [f'/tasks/{item.pk}/', f'/tasks/{item.pk}/state/', f'/attempts/{self.attempt.pk}/tasks/']:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.post(item, {'action':'send'}).status_code, 403)
        with self.assertRaises(PermissionDenied):
            tasks.review(self.foreign_teacher, self.attempt.pk, '', '')

    def test_private_inputs_and_teacher_criteria_not_in_student_html(self):
        item = self.code_session()
        self.client.force_login(self.student)
        for url in [f'/tasks/{item.pk}/', f'/attempts/{self.attempt.pk}/tasks/', f'/courses/{self.course.pk}/']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            for secret in ['PRIVATE_INPUT','PRIVATE_LABEL','PRIVATE_EXPECTED','PRIVATE_TEACHER_CRITERIA']:
                self.assertNotContains(response, secret)

    def test_late_student_can_be_invited_without_republishing(self):
        self.client.force_login(self.teacher)
        response = self.client.post(f'/teacher/assessments/{self.work.pk}/invite/', {'username':self.other.username})
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.other)
        self.assertContains(self.client.get('/'), self.work.title)
        self.assertContains(self.client.get(f'/assessments/{self.work.pk}/'), self.work.title)
        self.assertIsNotNone(services.start_attempt(self.other, self.work.pk))

    def test_teacher_review_pages_and_activity_log(self):
        item = self.code_session()
        self.client.force_login(self.student)
        self.assertEqual(self.post(item, {'action':'event','kind':'paste','detail':'Символов: 7'}).status_code, 200)
        self.client.force_login(self.teacher)
        for url in [f'/teacher/attempts/{self.attempt.pk}/review/', f'/teacher/attempts/{self.attempt.pk}/events/']:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertContains(self.client.get(f'/teacher/attempts/{self.attempt.pk}/review/'), 'Вставка')

    def test_download_has_same_visibility_as_submission(self):
        item = self.code_session()
        tasks.send_task(self.student, item.pk)
        s = Submission.objects.get(task_attempt=item)
        Submission.objects.filter(pk=s.pk).update(status='wrong_answer', result={'compiler':'visible compiler', 'tests':{'passed':0}, 'checks':[{'message':'VISIBLE_CHECK'}], 'test_report':[{'status':'wrong_answer'}]})
        self.client.force_login(self.student)
        for url in [f'/submissions/{s.pk}/state/', f'/submissions/{s.pk}/log/']:
            response = self.client.get(url)
            self.assertContains(response, 'visible compiler')
            self.assertContains(response, 'VISIBLE_CHECK')
            self.assertNotContains(response, 'test_report')

    def test_each_question_can_have_its_own_time(self):
        self.quiz.allow_back = False
        self.quiz.solution_minutes = None
        self.quiz.quiz_questions[0]['seconds'] = 10
        self.quiz.quiz_questions[1]['seconds'] = 25
        self.quiz.full_clean()
        self.quiz.save()
        services.publish_assessment(self.teacher, self.work.pk)
        self.work.invited_students.add(self.other)
        a = services.start_attempt(self.other, self.work.pk)
        item = tasks.start_task(self.other, a.pk, self.quiz.pk)
        self.assertEqual(tasks.question_deadline(item, item.attempt.snapshot['problems'][0]), item.started_at+timedelta(seconds=10))
        with patch('lab.tasks.timezone.now', return_value=item.started_at+timedelta(seconds=12)):
            tasks.expire_task(item)
        item.refresh_from_db()
        self.assertEqual(item.current_question, 1)
        self.assertEqual(tasks.question_deadline(item, item.attempt.snapshot['problems'][0]), item.started_at+timedelta(seconds=35))
        with patch('lab.tasks.timezone.now', return_value=item.started_at+timedelta(seconds=35)):
            tasks.expire_task(item)
        self.assertEqual(TaskAttempt.objects.get(pk=item.pk).status, 'wrong_answer')

    def test_automatic_credit_and_grade_thresholds(self):
        self.work.requires_confirmation = False
        self.work.grade_thresholds = {'3':10,'4':15,'5':20}
        self.work.save()
        services.publish_assessment(self.teacher, self.work.pk)
        self.work.invited_students.add(self.other)
        a = services.start_attempt(self.other, self.work.pk)
        quiz = tasks.start_task(self.other, a.pk, self.quiz.pk)
        tasks.save_task(self.other, quiz.pk, {'revision':0,'answers':{'0':[0],'1':[0,1]}})
        tasks.send_task(self.other, quiz.pk)
        item = tasks.start_task(self.other, a.pk, self.code.pk)
        tasks.send_task(self.other, item.pk)
        s = services.claim_submission()
        tasks.record_outcome(s, {'status':'accepted', 'result':{}})
        self.assertEqual(tasks.summary(a)['status'], 'Зачтено')
        self.assertEqual(tasks.summary(a)['grade'], '5')

    def test_deferred_results_are_hidden_in_all_task_views_and_downloads(self):
        self.work.linear = False
        self.work.reveal_after_close = True
        self.work.save()
        services.publish_assessment(self.teacher, self.work.pk)
        self.work.invited_students.add(self.other)
        a = services.start_attempt(self.other, self.work.pk)
        item = tasks.start_task(self.other, a.pk, self.code.pk)
        tasks.send_task(self.other, item.pk)
        s = services.claim_submission()
        tasks.record_outcome(s, {'status':'compile_error', 'result':{'compiler':'PRIVATE_DIAGNOSTIC','private_compiler':'UNIT_SECRET'}})
        self.client.force_login(self.other)
        for url in [f'/tasks/{item.pk}/', f'/tasks/{item.pk}/state/', f'/attempts/{a.pk}/tasks/', f'/attempts/{a.pk}/results/', f'/submissions/{s.pk}/log/']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            for secret in ['PRIVATE_DIAGNOSTIC','UNIT_SECRET','Ошибка компиляции','compile_error']:
                self.assertNotContains(response, secret)

    def test_private_unit_compiler_data_is_never_returned_to_student(self):
        item = self.code_session()
        tasks.send_task(self.student, item.pk)
        s = services.claim_submission()
        tasks.record_outcome(s, {'status':'compile_error', 'result':{'compiler':'Public summary','private_compiler':'SECRET_HARNESS_SOURCE'}})
        self.client.force_login(self.student)
        for url in [f'/tasks/{item.pk}/', f'/tasks/{item.pk}/state/', f'/submissions/{s.pk}/', f'/submissions/{s.pk}/state/', f'/submissions/{s.pk}/log/']:
            self.assertNotContains(self.client.get(url), 'SECRET_HARNESS_SOURCE')
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get(f'/submissions/{s.pk}/log/'), 'SECRET_HARNESS_SOURCE')

    def test_upload_reference_solution_as_utf8_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from lab.teacher_forms import ProblemForm
        data = {'title':'Загрузка','statement':'Сумма','statement_format':'plain','time_limit':2,'memory_mb':128}
        form = ProblemForm(data, {'reference_file':SimpleUploadedFile('solution.cpp', b'int main(){}')}, instance=Problem(course=self.course))
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        self.assertEqual(p.reference_source, 'int main(){}')
        invalid = ProblemForm(data, {'reference_file':SimpleUploadedFile('bad.cpp', b'\xff')}, instance=Problem(course=self.course))
        self.assertFalse(invalid.is_valid())

    def test_practice_activity_can_be_viewed_only_by_its_teacher(self):
        self.client.force_login(self.student)
        response = self.client.post(f'/attempts/{self.attempt.pk}/activity/', {'kind':'hidden'}, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.attempt.activity_events.get().kind, 'hidden')
        self.client.force_login(self.foreign_teacher)
        self.assertEqual(self.client.get(f'/teacher/attempts/{self.attempt.pk}/events/').status_code, 403)

    def test_diagnostic_policy_validation(self):
        self.code.diagnostic_policies = {'static:unusedVariable':'warning', 'dynamic:address':'required'}
        self.code.full_clean()
        self.code.diagnostic_policies = {'static:unusedVariable':'ignore-everything'}
        with self.assertRaises(ValidationError):
            self.code.full_clean()

    def test_work_can_contain_more_than_one_hundred_tasks(self):
        from lab.teacher_forms import RuleFormSet
        problems = Problem.objects.bulk_create([Problem(course=self.course, title=f'Task {i}', statement='Task') for i in range(101)])
        data = {'rules-TOTAL_FORMS':'101', 'rules-INITIAL_FORMS':'0', 'rules-MIN_NUM_FORMS':'0', 'rules-MAX_NUM_FORMS':'10000'}
        for i, problem in enumerate(problems):
            data.update({f'rules-{i}-problem':str(problem.pk),f'rules-{i}-max_score':'1',f'rules-{i}-order':str(i),f'rules-{i}-required':'on'})
        formset = RuleFormSet(data, instance=Assessment(course=self.course), prefix='rules', form_kwargs={'course':self.course})
        self.assertTrue(formset.is_valid(), formset.errors)
