// Complete teacher/student journey against disposable Django + a real Docker worker.
import {before, after, test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {mkdtemp, rm, mkdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import net from 'node:net';
import {chromium} from 'playwright-core';
let directory, server, worker, browser, origin, env, logs = '';
const python = process.env.PYTHON || '.venv/bin/python', password = 'Practical-browser-2026!';
before(async () => {
  directory = await mkdtemp(path.join(tmpdir(), 'contest-practical-'));
  env = {...process.env, DEBUG:'1', HTTPS:'0', DATABASE_PATH:path.join(directory,'db.sqlite3'),
    WORKER_LOCK:path.join(directory,'worker.lock'), JUDGE_PREFIX:`practical-${Date.now()}`,
    JUDGE_IMAGE:process.env.JUDGE_IMAGE || 'contest-judge:local', ALLOWED_HOSTS:'127.0.0.1,localhost'};
  const setup = spawnSync(python, ['-c', `
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django
django.setup()
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from lab.models import Course
call_command('migrate',verbosity=0)
U=get_user_model()
t=U.objects.create_user('teacher',password='${password}',is_staff=True)
s=U.objects.create_user('student',password='${password}')
U.objects.create_user('late',password='${password}')
g=Group.objects.create(name='101')
s.groups.add(g)
c=Course.objects.create(title='Учебный доступ')
c.teachers.add(t)
c.groups.add(g)
`], {env,encoding:'utf8'});
  assert.equal(setup.status,0,setup.stderr);
  const listener = net.createServer(); await new Promise(resolve => listener.listen(0,'127.0.0.1',resolve));
  const port = listener.address().port; await new Promise(resolve => listener.close(resolve)); origin = `http://127.0.0.1:${port}`;
  const log = chunk => { logs = (logs + chunk).slice(-20000); };
  server = spawn(python,['manage.py','runserver',`127.0.0.1:${port}`,'--noreload'],{env});
  server.stdout.on('data',log); server.stderr.on('data',log);
  worker = spawn(python,['manage.py','judge_worker'],{env}); worker.stdout.on('data',log); worker.stderr.on('data',log);
  for(let i=0;i<100;i++) { try { if ((await fetch(origin+'/accounts/login/')).ok) break; } catch(_) {} await new Promise(resolve => setTimeout(resolve,100)); }
  browser = await chromium.launch({headless:true, executablePath:process.env.BROWSER_PATH || undefined, args:['--no-sandbox','--disable-dev-shm-usage']});
  await mkdir('test-results',{recursive:true});
}, {timeout:30000});
after(async () => {
  await browser?.close();
  for(const process of [worker,server]) if(process && process.exitCode === null) { const done = new Promise(resolve => process.once('exit',resolve)); process.kill(); await done; }
  if(directory) await rm(directory,{recursive:true,force:true});
});
async function login(username) {
  const context = await browser.newContext({viewport:{width:1440,height:1000}}), page = await context.newPage();
  page.setDefaultTimeout(20000); await page.goto(origin+'/accounts/login/');
  await page.locator('[name=username]').fill(username); await page.locator('[name=password]').fill(password);
  await page.getByRole('button',{name:'Войти',exact:true}).click(); await page.waitForURL(origin+'/');
  return page;
}
test('teacher creates a discipline and exam; student builds, tests, submits; teacher comments and grants credit', async () => {
  const teacher = await login('teacher'), student = await login('student'), late = await login('late'), errors=[];
  for (const page of [teacher,student,late]) page.on('pageerror', e => errors.push(e.message));
  teacher.on('dialog', dialog => dialog.accept()); student.on('dialog', dialog => dialog.accept());
  await teacher.goto(origin+'/teacher/courses/new/');
  await teacher.locator('[name=title]').fill('Алгоритмы · зачёт'); await teacher.locator('[name=groups]').check();
  await teacher.locator('[name=description]').fill('Критерии: решите все задания. Формула $x^2$.');
  await teacher.locator('[name=faq]').fill('Можно ли запускать примеры? Да.');
  await teacher.locator('[name=acceptance]').fill('Проверьте корректность решения и отчёт анализаторов.');
  await teacher.getByRole('button',{name:'Сохранить курс'}).click();
  await teacher.locator('.course-card').filter({has:teacher.getByRole('heading',{name:'Алгоритмы · зачёт',exact:true})}).getByRole('link',{name:'Настроить курс',exact:true}).click();
  const course = new URL(teacher.url()).pathname.match(/courses\/(\d+)/)[1];
  await teacher.goto(`${origin}/teacher/problems/new/?course=${course}`);
  await teacher.locator('[name=title]').fill('Входной тест'); await teacher.locator('[name=kind]').selectOption('quiz');
  await teacher.locator('[name=statement]').fill('Ответьте на вопрос.'); await teacher.locator('[name=solution_minutes]').fill('5');
  await teacher.locator('[name=attempt_limit]').fill('2');
  await teacher.getByRole('button',{name:'+ Добавить вопрос теста',exact:true}).click();
  await teacher.locator('[data-question-text]').fill('Сколько будет $2+2$?');
  await teacher.locator('[data-option-text]').nth(0).fill('4'); await teacher.locator('[data-option-text]').nth(1).fill('5');
  await teacher.getByRole('button',{name:'Сохранить задачу и тесты'}).click();
  await teacher.getByText('Задача и тесты сохранены.',{exact:true}).waitFor();
  const quiz = teacher.url().match(/problems\/(\d+)/)[1];
  await teacher.goto(`${origin}/teacher/problems/new/?course=${course}`);
  await teacher.locator('[name=title]').fill('Сумма'); await teacher.locator('[name=statement_format]').selectOption('markdown');
  await teacher.locator('[name=statement]').fill('Вычислите $a+b$.');
  await teacher.locator('[name=starter_code]').fill('#include <iostream>\nint main() { return 0; }');
  await teacher.locator('[name=solution_minutes]').fill('10'); await teacher.locator('[name=attempt_limit]').fill('2');
  for(const policy of ['format','static','dynamic']) await teacher.locator(`[name=${policy}_policy]`).selectOption('warning');
  for(const [i,label,input,expected] of [[0,'Пример','2 3','5'],[1,'Закрытый','20 22','42']]) {
    await teacher.locator('[data-formset=tests] [data-add-form]').click();
    for(const [field,value] of Object.entries({label,input,expected})) await teacher.locator(`[name=tests-${i}-${field}]`).fill(value);
  }
  await teacher.locator('[name=tests-0-is_sample]').check(); await teacher.locator('[name=show_test_logs]').check();
  await teacher.getByRole('button',{name:'Сохранить задачу и тесты'}).click(); await teacher.getByText('Задача и тесты сохранены.',{exact:true}).waitFor();
  const code = teacher.url().match(/problems\/(\d+)/)[1];
  await teacher.goto(`${origin}/teacher/assessments/new/?course=${course}`);
  await teacher.locator('[name=title]').fill('Зачёт по алгоритмам'); await teacher.locator('[name=kind]').selectOption('exam');
  await teacher.locator('[name=pass_score]').fill('20'); await teacher.locator('[name=grade_thresholds]').fill('{"3": 10, "4": 15, "5": 20}');
  await teacher.locator('[name=groups]').check(); await teacher.locator('[name=linear]').check(); await teacher.locator('[name=requires_confirmation]').check();
  for(const [i,problem] of [[0,quiz],[1,code]]) {
    await teacher.locator('[data-formset=rules] [data-add-form]').click(); await teacher.locator(`[name=rules-${i}-problem]`).selectOption(problem);
    await teacher.locator(`[name=rules-${i}-max_score]`).fill('10'); await teacher.locator(`[name=rules-${i}-order]`).fill(String(i));
  }
  await teacher.getByRole('button',{name:'Сохранить и опубликовать',exact:true}).click();
  await teacher.getByText(/Опубликована версия 1/).waitFor(); const work = teacher.url().match(/assessments\/(\d+)/)[1];
  await teacher.getByRole('link',{name:'Открыть ведомость →'}).click();
  await teacher.locator('[name=username]').fill('late'); await teacher.getByRole('button',{name:'Открыть доступ к работе'}).click();
  await late.reload(); await late.getByText('Зачёт по алгоритмам',{exact:true}).waitFor();
  await student.goto(origin+'/'); await student.getByRole('link',{name:'Алгоритмы · зачёт',exact:true}).click();
  await student.getByRole('link',{name:'Ознакомиться и начать',exact:true}).click();
  await student.getByRole('button',{name:'Начать работу',exact:true}).click();
  await student.getByRole('link',{name:'Условия и начало попытки'}).first().click();
  await student.getByRole('button',{name:'Начать попытку',exact:true}).click();
  await student.locator('.katex').first().waitFor();
  const quizTask = student.url(); await student.getByRole('checkbox',{name:'4',exact:true}).check();
  await student.getByText('Сохранено на сервере',{exact:true}).waitFor(); await student.reload();
  assert.equal(await student.getByRole('checkbox',{name:'4',exact:true}).isChecked(),true);
  await student.getByRole('button',{name:'Скрыть таймер'}).click(); assert.equal(await student.locator('#task-timer').isHidden(),true);
  await student.getByRole('button',{name:'Показать таймер'}).click();
  await student.getByRole('button',{name:'Отправить на проверку',exact:true}).click(); await student.getByText('Успешно выполнено',{exact:true}).waitFor();
  await student.getByRole('link',{name:'← Задания работы'}).click();
  await student.getByRole('link',{name:'Условия и начало попытки'}).last().click(); await student.getByRole('button',{name:'Начать попытку',exact:true}).click();
  const codeTask = student.url();
  // Use the visible Monaco input, exercising the real editor and autosave integration.
  await student.locator('.monaco-editor').first().waitFor();
  await student.locator('.monaco-editor textarea').first().focus(); await student.keyboard.press('Control+a');
  await student.keyboard.insertText('#include <iostream>\nint main() { long long a,b; std::cin>>a>>b; std::cout<<a+b; }');
  await student.getByText('Сохранено на сервере',{exact:true}).waitFor(); await student.reload();
  assert.ok((await student.locator('.task-code').inputValue()).includes('std::cout<<a+b'));
  await student.locator('#terminal-command').fill('make'); await student.getByRole('button',{name:'Выполнить',exact:true}).click();
  await student.locator('#task-output').filter({hasText:'Сборка завершена успешно'}).waitFor({timeout:30000});
  await student.locator('#task-stdin').fill('7 8'); await student.getByRole('button',{name:'Запустить',exact:true}).click();
  await student.locator('#task-output').filter({hasText:'"stdout": "15"'}).waitFor({timeout:30000});
  await student.locator('#terminal-command').fill('make test'); await student.getByRole('button',{name:'Выполнить',exact:true}).click();
  await student.locator('#task-output').filter({hasText:'"stdout": "5"'}).waitFor({timeout:30000});
  await student.evaluate(() => document.dispatchEvent(new ClipboardEvent('paste')));
  await student.getByRole('button',{name:'Отправить на проверку',exact:true}).click();
  await student.locator('#task-status').filter({hasText:'Успешно выполнено'}).waitFor({timeout:60000});
  await student.screenshot({path:'test-results/practical-code.png',fullPage:true});
  await student.getByRole('link',{name:'← Задания работы'}).click(); await student.getByRole('heading',{name:'Ожидает проверки',exact:true}).waitFor();
  const attempt = student.url().match(/attempts\/(\d+)/)[1];
  await teacher.goto(`${origin}/teacher/attempts/${attempt}/review/`);
  await teacher.getByRole('heading',{name:'Журнал активности',exact:true}).waitFor();
  assert.ok((await teacher.locator('main').innerText()).includes('make test'));
  await teacher.locator('[name=status]').selectOption('revision'); await teacher.locator('[name=comment]').fill('Решение верное. Исправьте оформление.');
  await teacher.getByRole('button',{name:'Сохранить',exact:true}).click();
  await student.goto(`${origin}/attempts/${attempt}/results/`); await student.getByRole('heading',{name:'На доработке',exact:true}).waitFor();
  await student.getByText('Решение верное. Исправьте оформление.',{exact:true}).waitFor();
  await student.screenshot({path:'test-results/practical-revision.png',fullPage:true});
  await teacher.locator('[name=status]').selectOption('accepted'); await teacher.locator('[name=comment]').fill('Всё верно. Зачтено.');
  await teacher.getByRole('button',{name:'Сохранить',exact:true}).click();
  await student.reload(); await student.getByRole('heading',{name:'Зачтено',exact:true}).waitFor();
  assert.ok((await student.locator('main').innerText()).includes('Оценка: 5'));
  await student.screenshot({path:'test-results/practical-accepted.png',fullPage:true});
  await teacher.screenshot({path:'test-results/practical-teacher-review.png',fullPage:true});
  assert.deepEqual(errors,[],logs);
}, {timeout:180000});
