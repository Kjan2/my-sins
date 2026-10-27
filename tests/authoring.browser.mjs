// Real teacher pages and writes against a disposable SQLite database.
import {after, before, test} from "node:test";
import assert from "node:assert/strict";
import {spawn, spawnSync} from "node:child_process";
import {mkdtemp, mkdir, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
import net from "node:net";
import {chromium} from "playwright-core";

let directory, server, browser, origin, storageState;
let logs = "";
const password = "Browser-test-local-2026!";
before(async () => {
  directory = await mkdtemp(path.join(tmpdir(), "contest-authoring-"));
  const env = {...process.env, DEBUG:"1", HTTPS:"0", DATABASE_PATH:path.join(directory, "db.sqlite3"), ALLOWED_HOSTS:"127.0.0.1,localhost"};
  const python = process.env.PYTHON || ".venv/bin/python";
  const setup = spawnSync(python, ["-c", `
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.core.management import call_command
from django.contrib.auth import get_user_model
call_command('migrate', verbosity=0)
get_user_model().objects.create_user('ui-teacher', password='${password}', is_staff=True, first_name='Анна', last_name='Соколова')
call_command('seed_demo', teacher='ui-teacher', verbosity=0)
`], {env, encoding:"utf8"});
  assert.equal(setup.status, 0, setup.stderr);
  const listener = net.createServer();
  await new Promise(resolve => listener.listen(0, "127.0.0.1", resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  origin = `http://127.0.0.1:${port}`;
  server = spawn(python, ["manage.py", "runserver", `127.0.0.1:${port}`, "--noreload"], {env, stdio:["ignore", "pipe", "pipe"]});
  server.stdout.on("data", chunk => { logs = (logs + chunk).slice(-10000); });
  server.stderr.on("data", chunk => { logs = (logs + chunk).slice(-10000); });
  let ready = false;
  for (let attempt=0; attempt<100; attempt++) {
    try { if ((await fetch(origin+"/accounts/login/")).ok) { ready = true; break; } } catch (_) {}
    if (server.exitCode !== null) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(ready, logs);
  browser = await chromium.launch({executablePath:process.env.BROWSER_PATH || undefined, headless:true, args:["--no-sandbox", "--disable-dev-shm-usage"]});
  await mkdir("test-results", {recursive:true});
  const page = await browser.newPage();
  await page.goto(origin+"/accounts/login/?next=/teacher/");
  await page.locator('[name="username"]').fill("ui-teacher");
  await page.locator('[name="password"]').fill(password);
  await page.getByRole("button", {name:"Войти", exact:true}).click();
  await page.waitForURL(origin+"/teacher/");
  storageState = await page.context().storageState();
  await page.close();
}, {timeout:30000});
after(async () => {
  await browser?.close();
  if (server && server.exitCode === null) {
    const stopped = new Promise(resolve => server.once("exit", resolve));
    server.kill(); await stopped;
  }
  if (directory) await rm(directory, {recursive:true, force:true});
});
async function teacherPage(mobile=false) {
  const context = await browser.newContext({storageState, viewport:{width:mobile?390:1440, height:mobile?844:1000}});
  const page = await context.newPage(), errors=[];
  page.on("pageerror", error => errors.push(error.message));
  return {page, context, errors};
}
test("teacher creates, saves and publishes a work with a linked question through the cabinet", async () => {
  const {page,context,errors}=await teacherPage();
  try {
    await page.goto(origin+"/teacher/");
    await page.getByRole("link",{name:"+ Создать работу"}).click();
    await page.locator('[name="title"]').fill("Лабораторная №3 · Ветвления");
    await page.locator('[name="instructions"]').fill("Решите задачу и объясните выбор целочисленного типа.");
    await page.locator('[name="pass_score"]').fill("10");
    await page.locator('[name="groups"]').check();
    await page.locator('[data-formset="rules"] [data-add-form]').click();
    await page.locator('[name="rules-0-problem"]').selectOption({label:"Сумма двух чисел"});
    await page.locator('[data-formset="questions"] [data-add-form]').click();
    await page.locator('[name="questions-0-text"]').fill("Почему для суммы подходит long long?");
    await page.locator('[name="questions-0-problem"]').selectOption({label:"Сумма двух чисел"});
    await page.getByRole("button",{name:"Сохранить черновик", exact:true}).click();
    await page.waitForURL(/\/teacher\/assessments\/\d+\/edit\/$/);
    await page.getByText("Изменения сохранены. Для доступа студентов опубликуйте работу.",{exact:true}).waitFor();
    assert.equal(await page.locator('[name="questions-0-text"]').inputValue(),"Почему для суммы подходит long long?");
    await page.getByRole("button",{name:"Сохранить и опубликовать",exact:true}).click();
    await page.getByText(/Опубликована версия 1\. Работа доступна/).waitFor();
    assert.ok(await page.getByRole("button",{name:"Опубликовать новую версию"}).count());
    await page.screenshot({path:"test-results/teacher-assessment-desktop.png",fullPage:true});
    await page.getByRole("link",{name:"Работы",exact:true}).click();
    await page.screenshot({path:"test-results/teacher-management-desktop.png",fullPage:true});
    await page.getByRole("link",{name:/Черновики/}).click();
    assert.equal(await page.locator(".management-row").count(),0);
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});
test("teacher creates tests, excludes an empty row and previews an unsaved statement", async () => {
  const {page,context,errors}=await teacherPage();
  try {
    await page.goto(origin+"/teacher/problems/new/");
    await page.locator('[name="title"]').fill("Максимум из трёх чисел");
    await page.locator('[name="statement_format"]').selectOption("markdown");
    await page.locator('[name="statement"]').fill("Прочитайте **три целых числа** и выведите наибольшее.");
    for (const [index,label,input,expected] of [[0,"Пример","2 9 3","9"],[1,"Отрицательные числа","-5 -2 -8","-2"]]) {
      await page.locator('[data-formset="tests"] [data-add-form]').click();
      await page.locator(`[name="tests-${index}-label"]`).fill(label);
      await page.locator(`[name="tests-${index}-input"]`).fill(input);
      await page.locator(`[name="tests-${index}-expected"]`).fill(expected);
    }
    await page.locator('[name="tests-0-is_sample"]').check();
    await page.locator('[data-formset="tests"] [data-add-form]').click();
    await page.locator('[name="tests-2-DELETE"]').check();
    await page.getByRole("button",{name:"Сохранить задачу и тесты"}).click();
    await page.getByText("Задача и тесты сохранены.",{exact:true}).waitFor();
    assert.equal(await page.locator('[name="tests-TOTAL_FORMS"]').inputValue(),"2");
    await page.locator('[name="statement"]').fill("Новая **несохранённая** версия условия.");
    const popup = page.waitForEvent("popup");
    await page.getByRole("button",{name:/Предпросмотр условия/}).click();
    const preview = await popup;
    await preview.getByText("несохранённая",{exact:true}).waitFor();
    await preview.close();
    await page.getByRole("button",{name:"Сохранить задачу и тесты"}).click();
    await page.getByText("Задача и тесты сохранены.",{exact:true}).waitFor();
    await page.screenshot({path:"test-results/teacher-problem-editor.png",fullPage:true});
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});
test("management and authoring fit a phone and reject invalid dates without losing input", async () => {
  const {page,context,errors}=await teacherPage(true);
  try {
    await page.goto(origin+"/teacher/");
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:"test-results/teacher-management-mobile.png",fullPage:true});
    await page.getByRole("link",{name:"+ Создать работу"}).click();
    await page.locator('[name="title"]').fill("Работа с неверным сроком");
    await page.locator('[name="opens_at"]').fill("2027-01-02T12:00");
    await page.locator('[name="closes_at"]').fill("2027-01-01T12:00");
    await page.getByRole("button",{name:"Сохранить черновик",exact:true}).click();
    await page.getByText("Закрытие должно быть позже открытия.",{exact:true}).waitFor();
    assert.equal(await page.locator('[name="title"]').inputValue(),"Работа с неверным сроком");
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:"test-results/teacher-assessment-mobile.png",fullPage:true});
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});
