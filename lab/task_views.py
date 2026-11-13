import json
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render, get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST
from . import services, tasks
from .models import ActivityEvent, Assessment, TaskAttempt, Submission
from .problem_content import problem_sections, render_text
from .views import owned, public_submission, json_body, api_error
from .teacher_views import teacher_required
from .permissions import scoped, require_course


def owned_task(user, pk):
    try:
        item = tasks.task_for(user, pk)
    except TaskAttempt.DoesNotExist:
        raise Http404
    tasks.expire_task(item)
    item.refresh_from_db()
    return item


def task_state(item, user):
    problem = services.problem_for(item.attempt, item.problem_id)
    deadline = tasks.question_deadline(item, problem) or item.deadline
    visible = services.results_visible(user, item.attempt)
    result = dict(item.result) if visible else {}
    if not user.is_staff:
        result.pop('private_compiler', None)
        if not problem.get('show_test_logs'):
            result.pop('tests', None)
            result.pop('test_report', None)
    return {'id': item.pk, 'status': item.status if visible or item.status == 'active' else 'received', 'revision': item.revision, 'current_question': item.current_question,
            'deadline': deadline.isoformat() if deadline else None, 'server_now': timezone.now().isoformat(),
            'result': result, 'closed': item.status != 'active'}


@login_required
def work(request, pk):
    attempt = owned(request.user, pk)
    if not attempt.snapshot.get('task_workflow'):
        return redirect('attempt', pk=pk)
    tasks.expire_tasks(attempt)
    rows = []
    for p in attempt.snapshot['problems']:
        sessions = list(attempt.task_attempts.filter(problem_id=p['id']).order_by('-number'))
        rows.append({'problem': p, 'available': tasks.available(attempt, p['id']), 'sessions': sessions,
                     'passed': tasks.task_passed(attempt, p['id']), 'active': next((s for s in sessions if s.status == 'active'), None)})
    if not services.results_visible(request.user, attempt):
        summary = {'status': 'Результаты откроются после дедлайна', 'score': '—', 'maximum': '—'}
    else:
        summary = tasks.summary(attempt)
    return render(request, 'lab/tasks.html', {'item': attempt, 'rows': rows, 'summary': summary,
        'closes_at': parse_datetime(attempt.snapshot['closes_at']), 'results_visible': services.results_visible(request.user, attempt), 'is_owner': attempt.student_id == request.user.pk})


@login_required
def task_rules(request, pk, problem_id):
    attempt = owned(request.user, pk)
    if not attempt.snapshot.get('task_workflow'):
        return redirect('attempt', pk=pk)
    p = services.problem_for(attempt, problem_id)
    if request.method == 'POST':
        try:
            item = tasks.start_task(request.user, pk, problem_id)
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
            return redirect('task_work', pk=pk)
        return redirect('task_attempt', pk=item.pk)
    labels = {'off':'Выключено', 'warning':'Замечания, не блокирующие зачёт', 'required':'Ошибки блокируют зачёт'}
    policies = [{'title':title,'label':labels[p.get(name, 'off')]} for name,title in [('format_policy','Форматирование'),('static_policy','Статический анализ'),('dynamic_policy','Динамический анализ')]]
    return render(request, 'lab/task_rules.html', {'item': attempt, 'p': p, 'policies': policies, 'closes_at': parse_datetime(attempt.snapshot['closes_at'])})


@login_required
def task_attempt(request, pk):
    item = owned_task(request.user, pk)
    p = services.problem_for(item.attempt, item.problem_id)
    questions = []
    for i, q in enumerate(p.get('quiz_questions', [])):
        if not p['allow_back'] and i != item.current_question:
            continue
        questions.append({'index': i, 'html': render_text(q['text'], 'markdown'),
            'options': [{'index': j, 'text': q['options'][j], 'selected': j in item.answers.get(str(i), [])} for j in item.option_order[str(i)]]})
    return render(request, 'lab/task_attempt.html', {'item': item, 'p': p, 'sections': problem_sections(p), 'questions': questions,
        'clock': task_state(item, request.user), 'editable': item.status == 'active' and item.attempt.student_id == request.user.pk,
        'display_status': item.get_status_display() if services.results_visible(request.user, item.attempt) or item.status == 'active' else 'Результаты после дедлайна',
        'display_result': task_state(item, request.user)['result'],
        'latest_submission': item.attempt.submissions.filter(problem_id=item.problem_id).order_by('-pk').values_list('pk', flat=True).first(),
        'submissions': [public_submission(request.user, s, item.attempt, include_result=False) for s in item.attempt.submissions.filter(problem_id=item.problem_id).order_by('-pk')[:50]],
        'files': [{'name': name, 'source': source} for name, source in item.files.items()]})


@login_required
@require_POST
def task_action(request, pk):
    item = owned_task(request.user, pk)
    try:
        data = json_body(request)
        action = data.get('action')
        if action == 'save':
            item = tasks.save_task(request.user, pk, data)
        elif action == 'send':
            item = tasks.send_task(request.user, pk)
        elif action == 'next':
            item = tasks.advance_question(request.user, pk)
        elif action == 'run':
            submission = tasks.run_task(request.user, pk, data.get('command'), data.get('stdin', ''))
            return JsonResponse({'submission': public_submission(request.user, submission, item.attempt), **task_state(item, request.user)}, status=201)
        elif action == 'event':
            tasks.require_owner(request.user, item.attempt)
            if data.get('kind') not in ('blur', 'hidden', 'paste'):
                raise ValidationError('Неизвестное событие.')
            if item.status == 'active' and item.attempt.activity_events.count() < 10000:
                ActivityEvent.objects.create(attempt=item.attempt, task_attempt=item, kind=data['kind'], detail=str(data.get('detail', ''))[:200])
        else:
            raise ValidationError('Неизвестное действие.')
    except (ValidationError, ValueError, TypeError) as exc:
        return api_error(exc)
    return JsonResponse(task_state(item, request.user))


@login_required
def task_poll(request, pk):
    item = owned_task(request.user, pk)
    return JsonResponse(task_state(item, request.user))


@login_required
def download_log(request, pk):
    s = get_object_or_404(Submission, pk=pk)
    attempt = owned(request.user, s.attempt_id)
    public = public_submission(request.user, s, attempt)
    response = HttpResponse(json.dumps(public, ensure_ascii=False, indent=2), content_type='text/plain; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="submission-{s.pk}.log"'
    return response


@teacher_required
def review_work(request, pk):
    item = owned(request.user, pk)
    require_course(request.user, item.assessment.course_id)
    if not item.snapshot.get('task_workflow'):
        return redirect('teacher_attempt', pk=pk)
    tasks.expire_tasks(item)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'extra':
                tasks.give_extra(request.user, pk, request.POST.get('problem'), request.POST.get('reason', ''))
                messages.success(request, 'Добавлена попытка на выбранное задание.')
            else:
                tasks.review(request.user, pk, request.POST.get('status', ''), request.POST.get('comment', ''))
                messages.success(request, 'Комментарий и статус сохранены.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        return redirect('teacher_task_review', pk=pk)
    task_rows = []
    for task in item.task_attempts.all():
        p = services.problem_for(item, task.problem_id)
        selected = []
        for i, q in enumerate(p.get('quiz_questions', [])):
            selected.append({'text': q['text'], 'answers': [q['options'][j] for j in task.answers.get(str(i), [])]})
        task_rows.append({'task': task, 'problem': p, 'answers': selected,
                          'seconds': int((task.finished_at - task.started_at).total_seconds()) if task.finished_at else None})
    return render(request, 'teacher/task_review.html', {'item': item, 'summary': tasks.summary(item), 'task_rows': task_rows,
        'events': item.activity_events.order_by('pk')[:10000], 'extras': item.extra_attempts.select_related('actor'),
        'submissions': item.submissions.order_by('-pk')[:200], 'acceptance': render_text(item.assessment.course.acceptance, 'markdown')})


@teacher_required
@require_POST
def invite_student(request, pk):
    from django.contrib.auth import get_user_model
    assessment = get_object_or_404(scoped(request.user, Assessment.objects.all()), pk=pk)
    student = get_user_model().objects.filter(username=request.POST.get('username', ''), is_staff=False, is_active=True).first()
    if student:
        assessment.invited_students.add(student)
        messages.success(request, f'Студент {student.username} допущен к работе.')
    else:
        messages.error(request, 'Активный студент с таким логином не найден. Создайте его через импорт студентов.')
    return redirect('teacher_gradebook', pk=pk)


@teacher_required
def activity_events(request, pk):
    item = owned(request.user, pk)
    require_course(request.user, item.assessment.course_id)
    return render(request, 'teacher/events.html', {'item': item, 'events': item.activity_events.order_by('pk')[:10000]})


@login_required
@require_POST
def work_event(request, pk):
    attempt = owned(request.user, pk)
    tasks.require_owner(request.user, attempt)
    try:
        data = json_body(request)
        if data.get('kind') not in ('blur', 'hidden', 'paste'):
            raise ValidationError('Неизвестное событие.')
        if not attempt.finished_at and attempt.activity_events.count() < 10000:
            ActivityEvent.objects.create(attempt=attempt, kind=data['kind'], detail=str(data.get('detail', ''))[:200])
    except (ValidationError, ValueError, TypeError) as exc:
        return api_error(exc)
    return JsonResponse({'recorded': True})
