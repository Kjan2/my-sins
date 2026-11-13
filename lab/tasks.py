"""Server-owned task clocks, immutable submissions and automatic quiz marking."""
from datetime import timedelta
from functools import wraps
import random
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import ActivityEvent, ExtraAttempt, Submission, TaskAttempt
from . import services
from .permissions import require_course

CONFIG_FIELDS = ('kind', 'solution_minutes', 'attempt_limit', 'allow_back', 'shuffle_options',
                 'question_seconds', 'correct_threshold', 'quiz_questions', 'check_mode', 'task_name',
                 'starter_header', 'starter_implementation', 'unit_tests', 'format_policy',
                 'static_policy', 'dynamic_policy', 'format_style', 'show_test_logs', 'diagnostic_policies')
def clocked(function):
    @wraps(function)
    def wrapped(user, task_id, *args, **kwargs):
        item = task_for(user, task_id)
        require_owner(user, item.attempt)
        expire_task(item)
        return function(user, task_id, *args, **kwargs)
    return wrapped


PENDING = ('active', 'queued', 'running', 'infra_error')


def initial_files(problem):
    if problem.get('kind') == 'quiz':
        return {}
    files = {'main.cpp': problem.get('starter_code') or '#include <iostream>\nint main() { return 0; }\n'}
    if problem.get('check_mode') == 'unit':
        files[problem['task_name'] + '.cpp'] = problem.get('starter_implementation', '')
        files[problem['task_name'] + '.h'] = problem.get('starter_header', '')
    return files


def completed(attempt, problem_id):
    return attempt.task_attempts.filter(problem_id=problem_id).exclude(status__in=PENDING).order_by('-number').first()


def task_passed(attempt, problem_id):
    last = completed(attempt, problem_id)
    return bool(last and last.status == 'accepted')


def available(attempt, problem_id):
    problems = attempt.snapshot['problems']
    index = next(i for i, p in enumerate(problems) if p['id'] == problem_id)
    if not attempt.snapshot.get('linear') or index == 0:
        return True
    # Already started tasks stay accessible; retroactive relocking needs separate agreement.
    return attempt.task_attempts.filter(problem_id=problem_id).exists() or task_passed(attempt, problems[index - 1]['id'])


def require_owner(user, attempt):
    if attempt.student_id != user.pk or not user.is_active:
        raise PermissionDenied


def start_task(user, attempt_id, problem_id):
    attempt = services.accessible_attempt(user, attempt_id)
    require_owner(user, attempt)
    expire_tasks(attempt)
    return _start_task(user, attempt_id, problem_id)


@transaction.atomic
def _start_task(user, attempt_id, problem_id):
    attempt = services.accessible_attempt(user, attempt_id)
    require_owner(user, attempt)
    problem = services.problem_for(attempt, problem_id)
    if not attempt.snapshot.get('task_workflow'):
        raise ValidationError('Эта работа использует прежний режим прохождения.')
    current = attempt.task_attempts.filter(problem_id=problem['id'], status='active').first()
    if current:
        return current
    now = timezone.now()
    if now < parse_datetime(attempt.snapshot['opens_at']):
        raise services.Closed('Работа ещё не открыта.')
    if attempt.finished_at or (attempt.deadline and now >= attempt.deadline) or now >= parse_datetime(attempt.snapshot['closes_at']):
        raise services.Closed('Дедлайн начала новых попыток прошёл.')
    if not available(attempt, problem['id']):
        raise ValidationError('Сначала успешно выполните предыдущее задание.')
    if attempt.task_attempts.filter(problem_id=problem['id'], status__in=['queued', 'running']).exists():
        raise ValidationError('Дождитесь результата предыдущей отправки.')
    limit = problem.get('attempt_limit') or attempt.snapshot.get('submission_limit')
    used = attempt.task_attempts.filter(problem_id=problem['id']).exclude(status='infra_error').count()
    extra = attempt.extra_attempts.filter(problem_id=problem['id']).count()
    if limit and used >= limit + extra:
        raise ValidationError('Попытки закончились. Преподаватель может выдать дополнительную попытку на это задание.')
    previous = attempt.task_attempts.filter(problem_id=problem['id']).order_by('-number').first()
    per_question = problem.get('question_seconds') or any(q.get('seconds') for q in problem.get('quiz_questions', []))
    minutes = problem.get('solution_minutes') or (attempt.snapshot.get('duration_minutes') if not per_question else None)
    deadline = now + timedelta(minutes=minutes) if minutes else None
    if attempt.deadline:
        deadline = min(deadline, attempt.deadline) if deadline else attempt.deadline
    order = {}
    for i, q in enumerate(problem.get('quiz_questions', [])):
        indices = list(range(len(q['options'])))
        if problem.get('shuffle_options'):
            random.SystemRandom().shuffle(indices)
        order[str(i)] = indices
    return TaskAttempt.objects.create(attempt=attempt, problem_id=problem['id'], number=previous.number + 1 if previous else 1,
        started_at=now, question_started_at=now, deadline=deadline, option_order=order,
        files=previous.files if previous and problem.get('kind') != 'quiz' else initial_files(problem))


def task_for(user, task_id):
    item = TaskAttempt.objects.select_related('attempt__version', 'attempt__assessment').get(pk=task_id)
    services.accessible_attempt(user, item.attempt_id)
    return item


def question_deadline(item, problem):
    questions = problem.get('quiz_questions', [])
    seconds = questions[item.current_question].get('seconds', problem.get('question_seconds')) if questions else None
    if seconds:
        return item.question_started_at + timedelta(seconds=seconds)
    return None


def writable(item):
    if item.status != 'active':
        raise services.Closed('Попытка уже отправлена. Её содержимое нельзя изменить.')


@clocked
@transaction.atomic
def save_task(user, task_id, payload):
    item = task_for(user, task_id)
    require_owner(user, item.attempt)
    expire_task(item)
    item.refresh_from_db()
    writable(item)
    if type(payload.get('revision')) is not int or payload['revision'] != item.revision:
        raise ValidationError('Версия устарела. Перезагрузите страницу, чтобы не затереть сохранение из другой вкладки.')
    problem = services.problem_for(item.attempt, item.problem_id)
    if problem.get('kind') == 'quiz':
        answers = payload.get('answers')
        if not isinstance(answers, dict):
            raise ValidationError('Некорректные ответы.')
        for key, value in answers.items():
            if key not in {str(i) for i in range(len(problem['quiz_questions']))}:
                raise ValidationError('Неизвестный вопрос.')
            if not problem['allow_back'] and int(key) != item.current_question:
                raise ValidationError('Изменение предыдущих или будущих вопросов запрещено.')
            if not isinstance(value, list) or any(type(i) is not int or i not in item.option_order[key] for i in value):
                raise ValidationError('Некорректный вариант ответа.')
            item.answers[key] = sorted(set(value))
    else:
        files = payload.get('files')
        if not isinstance(files, dict) or set(files) != set(initial_files(problem)):
            raise ValidationError('Передайте только разрешённые файлы решения.')
        for source in files.values():
            services.validate_source(source)
        item.files = files
    item.revision += 1
    item.save(update_fields=['files', 'answers', 'revision'])
    return item


@clocked
@transaction.atomic
def advance_question(user, task_id):
    item = task_for(user, task_id)
    require_owner(user, item.attempt)
    expire_task(item)
    item.refresh_from_db()
    writable(item)
    problem = services.problem_for(item.attempt, item.problem_id)
    if problem.get('kind') != 'quiz' or problem['allow_back']:
        raise ValidationError('Последовательное прохождение не включено.')
    if item.current_question + 1 >= len(problem['quiz_questions']):
        return finalize_task(item)
    item.current_question += 1
    item.question_started_at = timezone.now()
    item.revision += 1
    item.save(update_fields=['current_question', 'question_started_at', 'revision'])
    return item


@transaction.atomic
def finalize_task(item, *, at=None):
    item.refresh_from_db()
    if item.status != 'active':
        return item
    problem = services.problem_for(item.attempt, item.problem_id)
    item.finished_at = at or timezone.now()
    if problem.get('kind') == 'quiz':
        correct = sum(set(item.answers.get(str(i), [])) == set(q['correct']) for i, q in enumerate(problem['quiz_questions']))
        item.status = 'accepted' if correct >= problem['correct_threshold'] else 'wrong_answer'
        item.result = {'correct': correct, 'total': len(problem['quiz_questions']), 'threshold': problem['correct_threshold']}
    else:
        Submission.objects.create(attempt=item.attempt, task_attempt=item, problem_id=item.problem_id,
                                  source=item.files['main.cpp'], files=item.files, kind='judge')
        item.status = 'queued'
    item.save(update_fields=['finished_at', 'status', 'result'])
    return item


@transaction.atomic
def expire_task(item, now=None):
    item.refresh_from_db()
    if item.status != 'active':
        return
    now = now or timezone.now()
    problem = services.problem_for(item.attempt, item.problem_id)
    # Question deadlines are measured on the server, including while the tab is closed.
    q_deadline = question_deadline(item, problem)
    while q_deadline and now >= q_deadline and (not item.deadline or q_deadline <= item.deadline):
        if item.current_question + 1 >= len(problem['quiz_questions']):
            finalize_task(item, at=q_deadline)
            return
        item.current_question += 1
        item.question_started_at = q_deadline
        item.revision += 1
        item.save(update_fields=['current_question', 'question_started_at', 'revision'])
        q_deadline = question_deadline(item, problem)
    if item.deadline and now >= item.deadline:
        finalize_task(item, at=item.deadline)


def expire_tasks(attempt=None):
    items = TaskAttempt.objects.filter(status='active').select_related('attempt__version')
    if attempt:
        items = items.filter(attempt=attempt)
    for item in items:
        expire_task(item)


@clocked
@transaction.atomic
def send_task(user, task_id):
    item = task_for(user, task_id)
    require_owner(user, item.attempt)
    expire_task(item)
    return finalize_task(item)


@clocked
@transaction.atomic
def run_task(user, task_id, command, stdin=''):
    item = task_for(user, task_id)
    require_owner(user, item.attempt)
    expire_task(item)
    item.refresh_from_db()
    writable(item)
    services.validate_source(stdin, 'Ввод')
    if command not in ('make', 'make test', './main', 'g++ main.cpp -o main'):
        raise ValidationError('Доступны make, make test, ./main, g++ main.cpp -o main.')
    problem = services.problem_for(item.attempt, item.problem_id)
    if command == 'make test' and not any(t['is_sample'] for t in problem['tests']):
        raise ValidationError('Преподаватель не добавил открытые примеры. Используйте ./main со своим вводом; закрытые unit-тесты запускаются при отправке.')
    if problem.get('kind') == 'quiz':
        raise ValidationError('В тесте нет запуска программ.')
    if Submission.objects.filter(attempt__student=user, status__in=['queued', 'running']).count() >= 3:
        raise ValidationError('У вас уже три запуска в очереди.')
    ActivityEvent.objects.create(attempt=item.attempt, task_attempt=item, kind='command', detail=command)
    return Submission.objects.create(attempt=item.attempt, problem_id=item.problem_id, source=item.files['main.cpp'], files=item.files,
        custom_input=stdin, kind='sample' if command == 'make test' else 'build' if command in ('make', 'g++ main.cpp -o main') else 'custom', result={'command': command})


@transaction.atomic
def give_extra(actor, attempt_id, problem_id, reason):
    services.teacher(actor)
    attempt = services.accessible_attempt(actor, attempt_id)
    require_course(actor, attempt.assessment.course_id)
    problem = services.problem_for(attempt, problem_id)
    if not reason.strip():
        raise ValidationError('Укажите причину.')
    return ExtraAttempt.objects.create(attempt=attempt, problem_id=problem['id'], actor=actor, reason=reason[:2000])


@transaction.atomic
def review(actor, attempt_id, status, comment):
    services.teacher(actor)
    attempt = services.accessible_attempt(actor, attempt_id)
    require_course(actor, attempt.assessment.course_id)
    if status not in ('accepted', 'revision', ''):
        raise ValidationError('Некорректный статус.')
    if status and not attempt.snapshot.get('requires_confirmation'):
        raise ValidationError('Работа засчитывается автоматически. Можно оставить комментарий.')
    if status == 'accepted' and not all(task_passed(attempt, p['id']) for p in attempt.snapshot['problems']):
        raise ValidationError('Для зачёта должны быть успешно выполнены все задания.')
    attempt.review_status, attempt.teacher_comment = status, comment[:10000]
    attempt.reviewed_by, attempt.reviewed_at = actor, timezone.now()
    attempt.save(update_fields=['review_status', 'teacher_comment', 'reviewed_by', 'reviewed_at'])


def summary(attempt):
    problems = []
    for p in attempt.snapshot['problems']:
        last = completed(attempt, p['id'])
        accepted = bool(last and last.status == 'accepted')
        problems.append({'id': p['id'], 'title': p['title'], 'score': p['max_score'] if accepted else 0,
                         'max_score': p['max_score'], 'required': True, 'accepted': accepted})
    ok = bool(problems) and all(p['accepted'] for p in problems)
    waiting = attempt.task_attempts.filter(status__in=['active', 'queued', 'running', 'infra_error']).exists()
    manual = attempt.snapshot.get('requires_confirmation')
    status = ('На доработке' if attempt.review_status == 'revision' else 'Зачтено' if ok and (not manual or attempt.review_status == 'accepted')
              else 'Ожидает проверки' if ok and manual else 'Выполняется' if waiting else 'Не зачтено')
    score = sum(p['score'] for p in problems)
    grade = max(((threshold, label) for label, threshold in attempt.snapshot.get('grade_thresholds', {}).items() if score >= threshold), default=(0, '—'))[1]
    return {'grade': grade, 'problems': problems, 'theory': [], 'score': score, 'maximum': sum(p['max_score'] for p in problems),
            'provisional': waiting or (ok and manual and attempt.review_status != 'accepted'), 'passed': status == 'Зачтено',
            'pass_score': attempt.snapshot['pass_score'], 'required_ok': ok, 'status': status, 'comment': attempt.teacher_comment}


@transaction.atomic
def record_outcome(submission, outcome):
    changed = Submission.objects.filter(pk=submission.pk, status='running').update(**outcome, finished_at=timezone.now())
    if changed and submission.task_attempt_id:
        TaskAttempt.objects.filter(pk=submission.task_attempt_id).update(status=outcome['status'], result=outcome.get('result', {}))
