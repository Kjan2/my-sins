// Real browser + Django + worker + Docker. Started only by scripts/rehearse_exam.py.
import assert from "node:assert/strict";
import {chromium} from "playwright-core";

const base = process.env.REHEARSAL_BASE;
if (!base?.startsWith("http://127.0.0.1:")) throw new Error("Run through scripts/rehearse_exam.py --browser");
const browser = await chromium.launch({executablePath:process.env.BROWSER_PATH || undefined, headless:true, args:["--no-sandbox","--disable-dev-shm-usage"]});
const errors = [];
try {
  const context = await browser.newContext();
  const page = await context.newPage();
  page.on("pageerror", error => errors.push(error.message));
  async function login(username) {
    await page.goto(base+"/accounts/login/");
    await page.locator('[name="username"]').fill(username);
    await page.locator('[name="password"]').fill(process.env.REHEARSAL_PASSWORD);
    await Promise.all([page.waitForURL(base+"/"), page.locator('form button').click()]);
  }
  await login(process.env.REHEARSAL_USERNAME);
  await page.goto(base+`/assessments/${process.env.REHEARSAL_ASSESSMENT}/`);
  await page.locator('form[action$="/start/"] button').click();
  await page.waitForURL(/\/attempts\/\d+\/$/);
  const attemptId = new URL(page.url()).pathname.split("/")[2];
  const problem = process.env.REHEARSAL_PROBLEM;
  await page.locator(`#problem-${problem}[data-editor="monaco"]`).waitFor();
  await page.locator(`#problem-${problem} .monaco-editor textarea.inputarea`).focus();
  await page.keyboard.press("Control+a");
  await page.keyboard.insertText('#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}');
  await page.locator(`#problem-${problem} .save-status[data-state="saved"]`).waitFor();
  await page.locator(`#problem-${problem} .sample-run`).click();
  await page.locator("#run-result .result-heading h3.verdict-accepted").waitFor({timeout:120000});
  await page.locator(`#problem-${problem} .judge-run`).click();
  await page.waitForFunction(()=>document.querySelector("#run-result h3.verdict-accepted") && document.querySelector("#run-result .result-caption").textContent.includes("Отправка на оценку"),null,{timeout:120000});
  await page.locator("#theory-tab").click();
  await page.locator(`#answer-${process.env.REHEARSAL_QUESTION}`).fill("long long вмещает сумму двух чисел.");
  const select = page.locator(".theory-question select");
  await select.selectOption({index:1});
  await page.locator("#finish-button").click();
  await page.locator("#confirm-finish").click();
  await page.waitForURL("**/results/");
  await context.clearCookies();
  await login("rehearsal-teacher");
  await page.goto(base+`/teacher/attempts/${attemptId}/`);
  await page.locator(`[name="q${process.env.REHEARSAL_QUESTION}-score"]`).fill("5");
  await page.locator(".grade-form button").click();
  await context.clearCookies();
  await login(process.env.REHEARSAL_USERNAME);
  await page.goto(base+`/attempts/${attemptId}/results/`);
  assert.ok((await page.locator(".score-card").textContent()).includes("Зачтено"));
  assert.deepEqual(errors,[]);
} finally { await browser.close(); }
