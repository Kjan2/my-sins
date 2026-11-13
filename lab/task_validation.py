import re
from django.core.exceptions import ValidationError


def validate_problem(problem):
    errors = {}
    if problem.quiz_questions is None:
        problem.quiz_questions = []
    if problem.diagnostic_policies is None:
        problem.diagnostic_policies = {}
    if not isinstance(problem.diagnostic_policies, dict) or any(
        not isinstance(key, str) or not re.fullmatch(r'(format|static|dynamic):[A-Za-z0-9_.-]+', key) or value not in ('warning', 'required')
        for key, value in problem.diagnostic_policies.items()):
        errors['diagnostic_policies'] = 'Укажите пары "инструмент:код": "warning" или "required".'

    question_timers = isinstance(problem.quiz_questions, list) and any(isinstance(q, dict) and q.get('seconds') is not None for q in problem.quiz_questions)
    if problem.allow_back and (problem.question_seconds or question_timers):
        errors['question_seconds'] = 'Таймер вопроса доступен только при запрете возвращения.'
    if problem.solution_minutes and (problem.question_seconds or question_timers):
        errors['question_seconds'] = 'Выберите общий таймер или таймер каждого вопроса.'
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,49}', problem.task_name or '') or problem.task_name == 'main':
        errors['task_name'] = 'Имя файлов: латинские буквы, цифры и _, начиная с буквы; кроме main.'
    if problem.kind == 'quiz':
        questions = problem.quiz_questions
        if not isinstance(questions, list) or not questions:
            errors['quiz_questions'] = 'Добавьте хотя бы один вопрос.'
        else:
            for q in questions:
                if (not isinstance(q, dict) or not isinstance(q.get('text'), str) or not q['text'].strip()
                        or not isinstance(q.get('options'), list) or len(q['options']) < 2
                        or any(not isinstance(o, str) or not o.strip() for o in q['options'])
                        or not isinstance(q.get('correct'), list) or not q['correct']
                        or any(type(i) is not int or not 0 <= i < len(q['options']) for i in q['correct'])
                        or len(set(q['correct'])) != len(q['correct'])):
                    errors['quiz_questions'] = 'У каждого вопроса нужны текст, минимум два ответа и минимум один правильный вариант.'
                    break
            if question_timers:
                for q in questions:
                    seconds = q.get('seconds', problem.question_seconds) if isinstance(q, dict) else None
                    if type(seconds) is not int or seconds < 1:
                        errors['quiz_questions'] = 'Укажите положительное время в seconds для каждого вопроса или общий таймер вопроса по умолчанию.'
                        break
            if problem.correct_threshold > len(questions):
                errors['correct_threshold'] = 'Порог не должен превышать число вопросов.'
    elif problem.check_mode == 'unit' and not problem.unit_tests.strip():
        errors['unit_tests'] = 'Добавьте закрытый main.cpp с unit-тестами.'
    if errors:
        raise ValidationError(errors)
