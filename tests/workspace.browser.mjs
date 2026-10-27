// Real Chromium + local assets. API responses are fixtures; Django covers authorization and deadlines.
import {after, before, test} from "node:test";
import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {readFile, mkdir} from "node:fs/promises";
import path from "node:path";
import {chromium} from "playwright-core";

const python = process.env.PYTHON || ".venv/bin/python";
const rendered = spawnSync(python, ["scripts/render_workspace_fixture.py"], {encoding: "utf8"});
if (rendered.status !== 0) throw new Error(rendered.stderr);
const fixture = JSON.parse(rendered.stdout);
const origin = "http://contest.test";
let browser;
before(async () => {
  browser = await chromium.launch({executablePath: process.env.BROWSER_PATH || undefined, headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"]});
  await mkdir("test-results", {recursive:true});
});
after(async () => { await browser?.close(); });

async function workbench({mobile=false, fallback=false, fastTimeout=false} = {}) {
  const context = await browser.newContext({viewport: mobile ? {width:390, height:844} : {width:1440, height:1000}});
  const page = await context.newPage(), errors = [];
  if (fastTimeout) await context.addInitScript(() => {
    const schedule = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay, ...args) => schedule(callback, delay === 15000 ? 100 : delay, ...args);
  });
  page.on("pageerror", error => errors.push(error.message));
  const state = {saves:[], submissions:[], requests:[], fields:new Map(), taskProgress:{}, resultsVisible:true, finish:null, grade:null, monitor:{...fixture.monitor}, offline:false, hangSubmit:false, hangSave:false, closed:false, deadline:new Date(Date.now()+3600000).toISOString()};
  const escape = value => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
  const html = () => {
    let result = fixture.html;
    const latestJudged = {};
    for (const item of state.submissions.filter(item => item.kind_code === "judge")) {
      const previous = latestJudged[item.problem_id];
      if (!previous || item.id > previous.id) latestJudged[item.problem_id] = {id:item.id};
    }
    result = result.replace(/(<script id="latest-judge-data"[^>]*>)[\s\S]*?(<\/script>)/, (_, start, end) => start + JSON.stringify(latestJudged) + end);
    for (const field of state.fields.values()) {
      const code = field.type === "code", id = field.problem ?? field.question;
      result = result.replace(`data-${code ? "problem" : "question"}="${id}" data-revision="0"`, `data-${code ? "problem" : "question"}="${id}" data-revision="${field.revision}"`);
      result = result.replace(new RegExp(`(<textarea[^>]*id="${code ? "code" : "answer"}-${id}"[^>]*>)[\\s\\S]*?(</textarea>)`), (_, before, after) => before + escape(field.source ?? field.text) + after);
    }
    return result;
  };
  const clock = () => ({server_now:new Date().toISOString(), deadline:state.deadline, closed:state.closed});
  await context.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.abort();
    const json = (data, status=200) => route.fulfill({status, contentType:"application/json", body:JSON.stringify(data)});
    if (url.pathname.startsWith("/static/")) {
      if (fallback && url.pathname === "/static/editor/editor.js") return route.fulfill({status:404, body:""});
      const filename = path.resolve("static", url.pathname.slice(8));
      if (!filename.startsWith(path.resolve("static")+path.sep)) return route.abort();
      const type = filename.endsWith(".css") ? "text/css" : filename.endsWith(".ttf") ? "font/ttf" : "application/javascript";
      return route.fulfill({contentType:type, body:await readFile(filename)});
    }
    if (url.pathname === "/attempts/101/") return route.fulfill({contentType:"text/html", body:html()});
    if (url.pathname === "/teacher/attempts/101/") {
      if (route.request().method() === "POST") {
        state.grade=Object.fromEntries(new URLSearchParams(route.request().postData()));
        return route.fulfill({contentType:"text/html; charset=utf-8",body:"<p>Оценка сохранена.</p>"});
      }
      return route.fulfill({contentType:"text/html",body:fixture.teacher_attempt});
    }
    if (url.pathname === "/teacher/judge/") return route.fulfill({contentType:"text/html",body:fixture.teacher_judge});
    if (url.pathname === "/teacher/judge/state/") {
      if (state.offline) return route.abort();
      return json(state.monitor);
    }
    if (url.pathname === "/attempts/101/state/") return json({...clock(), results_visible:state.resultsVisible, task_progress:state.taskProgress, submissions:state.submissions.map(({result,source,custom_input,...meta})=>meta)});
    if (/^\/submissions\/\d+\/state\/$/.test(url.pathname)) return json({submission:state.submissions.find(item=>item.id===Number(url.pathname.split("/")[2]))});
    if (url.pathname === "/attempts/101/save/") {
      if (state.offline) return route.abort();
      if (state.hangSave) return;
      if (state.closed) return json({error:"Время истекло."},409);
      const body = route.request().postDataJSON(), key = `${body.type}:${body.problem ?? body.question}`;
      const previous = state.fields.get(key), revision = previous?.revision || 0;
      const unchanged = previous && (body.source ?? body.text) === (previous.source ?? previous.text) && (body.submission || null) === (previous.submission || null);
      if (body.expected_revision !== revision && !unchanged) return json({error:"Текст изменён в другой вкладке.", conflict:previous},409);
      const field = {...body, revision:unchanged ? revision : revision + 1};
      state.fields.set(key, field);
      state.saves.push(body); return json({saved:true,revision:field.revision,...clock()});
    }
    if (url.pathname === "/attempts/101/submit/") {
      const body=route.request().postDataJSON();
      state.requests.push(body);
      const previous = state.submissions.find(item => item.request_id === body.request_id);
      if (previous) return json({submission:previous,...clock()},201);
      const item={id:200+state.submissions.length,problem_id:Number(body.problem),source:body.source,custom_input:body.custom_input,kind:body.kind==="sample"?"Примеры":body.kind==="custom"?"Свой ввод":"На проверку",kind_code:body.kind,status:"queued",status_label:"В очереди",created_at:new Date().toISOString(),result:{}};
      item.request_id = body.request_id;
      state.submissions.unshift(item);
      if (state.hangSubmit) { state.hangSubmit = false; return; }
      return json({submission:item,...clock()},201);
    }
    if (url.pathname === "/attempts/101/finish/") {
      state.finish=route.request().postDataJSON(); state.closed=true;
      return json({finished:true,url:"/attempts/101/results/",...clock()});
    }
    if (url.pathname === "/attempts/101/results/") return route.fulfill({contentType:"text/html",body:"<h1>Результаты работы</h1>"});
    return route.fulfill({status:404,body:""});
  });
  await page.goto(origin+"/attempts/101/");
  if (!fallback) await page.locator('#problem-1[data-editor="monaco"]').waitFor({state:"attached"});
  return {page,context,state,errors};
}
async function appendCode(page, text, problem=1) {
  await page.locator(`#problem-${problem} .monaco-editor textarea.inputarea`).focus();
  await page.keyboard.press("Control+End");
  await page.keyboard.insertText(text);
}
async function saved(page, problem=1) {
  await page.waitForFunction(id => document.querySelector(`#problem-${id} .save-status`).textContent==="Сохранено на сервере", problem);
}

test("Monaco, independent tasks, autosave and keyboard-accessible panes", async () => {
  const {page,context,state,errors}=await workbench();
  try {
    assert.ok(await page.locator("#problem-1 .monaco-editor .line-numbers").count());
    await appendCode(page,"\n// first task draft"); await saved(page);
    assert.ok(state.saves.find(item=>item.problem==="1").source.includes("first task draft"));
    assert.equal(state.submissions.length,0);
    await page.selectOption("#task-select","2");
    await page.locator('#problem-2[data-editor="monaco"]').waitFor();
    assert.equal(await page.locator("#problem-2 textarea.code").inputValue(),fixture.source);
    await appendCode(page,"\n// second task draft",2); await saved(page,2);
    await page.selectOption("#task-select","1");
    assert.ok((await page.locator("#problem-1 textarea.code").inputValue()).includes("first task draft"));
    await page.locator("#pane-resizer").focus(); await page.keyboard.press("ArrowRight");
    assert.equal(await page.locator("#pane-resizer").getAttribute("aria-valuenow"),"45");
    await page.locator("#theory-tab").focus(); await page.keyboard.press("ArrowLeft");
    assert.equal(await page.locator("#statement-tab").getAttribute("aria-selected"),"true");
    await page.screenshot({path:"test-results/workspace-desktop.png",fullPage:true});
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});

test("sample verdict and literal stdout appear under the editor without navigation", async () => {
  const {page,context,state,errors}=await workbench();
  try {
    await page.locator("#problem-1 .sample-run").click();
    await page.waitForFunction(()=>document.querySelector("#run-result").textContent.includes("В очереди"));
    assert.equal(state.submissions.length,1);
    Object.assign(state.submissions[0],{status:"wrong_answer",status_label:"Неверный ответ",result:{samples:[{label:"Example",status:"wrong_answer",stdout:'<img src=x onerror="alert(1)">',stderr:""}]}});
    await page.waitForFunction(()=>document.querySelector("#run-result").textContent.includes("Неверный ответ"),null,{timeout:9000});
    assert.ok((await page.locator("#run-result").textContent()).includes("<img src=x"));
    assert.equal(await page.locator("#run-result img").count(),0);
    assert.equal(new URL(page.url()).pathname,"/attempts/101/");
    await page.locator("#history-tab").click();
    assert.equal(await page.locator(".submission-row").count(),1);
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});

test("offline draft is explicit and can be restored into Monaco after reload", async () => {
  const {page,context,state}=await workbench();
  try {
    state.offline=true;
    await appendCode(page,"\n// keep me offline");
    await page.locator('#problem-1 .save-status[data-state="error"]').waitFor();
    assert.ok(await page.evaluate(()=>localStorage.getItem("department:101:code:1").includes("keep me offline")));
    page.once("dialog",dialog=>dialog.accept());
    await page.reload();
    await page.locator('#problem-1[data-editor="monaco"]').waitFor();
    state.offline=false;
    await page.getByRole("button",{name:"Восстановить в редактор"}).click();
    await saved(page);
    assert.ok((await page.locator("#problem-1 textarea.code").inputValue()).includes("keep me offline"));
  } finally { await context.close(); }
});

test("two tabs compare conflicting code and explicitly keep the chosen version", async () => {
  const {page,context,state,errors}=await workbench();
  const second = await context.newPage();
  try {
    await second.goto(origin+"/attempts/101/");
    await second.locator('#problem-1[data-editor="monaco"]').waitFor();
    await appendCode(page,"\n// first tab"); await saved(page);
    await appendCode(second,"\n// second tab");
    await second.locator("#conflict-dialog[open]").waitFor();
    assert.ok((await second.locator("#conflict-local").inputValue()).includes("second tab"));
    assert.ok((await second.locator("#conflict-server").inputValue()).includes("first tab"));
    assert.ok(state.fields.get("code:1").source.includes("first tab"));
    await second.locator("#conflict-close").click();
    await second.getByRole("button",{name:"Сравнить версии",exact:true}).first().click();
    await second.locator("#conflict-keep-local").click(); await saved(second);
    assert.ok(state.fields.get("code:1").source.includes("second tab"));
    await appendCode(page,"\n// another first tab edit");
    await page.locator("#conflict-dialog[open]").waitFor();
    await page.locator("#conflict-use-server").click();
    assert.ok((await page.locator("#code-1").inputValue()).includes("second tab"));
    assert.equal(await page.evaluate(()=>localStorage.getItem("department:101:code:1")),null);
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});

test("unknown submission outcome survives reload and retries the same operation", async () => {
  const {page,context,state,errors}=await workbench({fastTimeout:true});
  try {
    state.hangSubmit=true;
    await page.locator("#problem-1 .judge-run").click();
    await page.waitForFunction(()=>document.querySelector("#workspace-message").textContent.includes("Результат запроса неизвестен"));
    assert.equal(state.submissions.length,1);
    assert.equal(await page.locator("#code-1").evaluate(node=>node.readOnly),false);
    const requestId = state.requests[0].request_id;
    await page.reload();
    await page.locator("#retry-submission").waitFor();
    assert.equal(await page.locator("#problem-1 .judge-run").isDisabled(),true);
    await page.locator("#preview-pending-submission").click();
    assert.equal(await page.locator("#submitted-source").inputValue(),fixture.source);
    await page.locator("#close-source").click();
    await page.locator("#retry-submission").click();
    await page.waitForFunction(()=>document.querySelector("#pending-submission").hidden);
    assert.equal(state.requests.length,2);
    assert.equal(state.requests[1].request_id,requestId);
    assert.equal(state.submissions.length,1);
    assert.equal(await page.evaluate(()=>Object.keys(localStorage).filter(key=>key.startsWith("department:101:pending-submission:")).length),0);
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});

test("a hanging save times out and a later save can succeed", async () => {
  const {page,context,state}=await workbench({fastTimeout:true});
  try {
    state.hangSave=true;
    await appendCode(page,"\n// delayed save");
    await page.locator('#problem-1 .save-status[data-state="error"]').waitFor();
    assert.ok((await page.locator("#problem-1 .save-status").textContent()).includes("15 секунд"));
    state.hangSave=false;
    await page.locator("#save-all").click(); await saved(page);
    assert.ok(state.fields.get("code:1").source.includes("delayed save"));
  } finally { await context.close(); }
});

test("final confirmation includes code from all tasks and current theory", async () => {
  const {page,context,state}=await workbench();
  try {
    await appendCode(page,"\n// final code");
    await page.locator("#theory-tab").click();
    await page.locator("#answer-1").fill("Выбран long long для суммы двух чисел.");
    await page.locator("#finish-button").click();
    await page.locator("#confirm-finish").click();
    await page.waitForURL("**/results/");
    assert.equal(state.finish.confirm,true);
    assert.equal(state.finish.drafts.length,2);
    assert.ok(state.finish.drafts[0].source.includes("final code"));
    assert.ok(state.finish.answers[0].text.includes("long long"));
  } finally { await context.close(); }
});

test("server deadline locks the editor and an extension reopens the same draft", async () => {
  const {page,context,state}=await workbench();
  try {
    state.closed=true;
    await page.waitForFunction(()=>document.querySelector("#problem-1 .judge-run").disabled,null,{timeout:9000});
    assert.equal(await page.locator("#problem-1 textarea.code").evaluate(node=>node.readOnly),true);
    state.closed=false; state.deadline=new Date(Date.now()+7200000).toISOString();
    await page.waitForFunction(()=>!document.querySelector("#problem-1 .judge-run").disabled,null,{timeout:9000});
    await appendCode(page,"\n// extended"); await saved(page);
    assert.ok(state.saves.at(-1).source.includes("extended"));
  } finally { await context.close(); }
});

test("mobile layout fits the viewport and editor fallback still saves", async () => {
  const {page,context,state,errors}=await workbench({mobile:true,fallback:true});
  try {
    await page.locator('.mobile-workspace-nav [data-mobile-view="code"]').click();
    const input=page.locator("#code-1");
    await input.fill("int main(){return 0;}"); await saved(page);
    assert.equal(state.saves.at(-1).source,"int main(){return 0;}");
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    await page.locator('.mobile-workspace-nav [data-mobile-view="statement"]').click();
    assert.equal(await page.locator("#code-1").isVisible(),false);
    await page.locator('.mobile-workspace-nav [data-mobile-view="code"]').click();
    assert.equal(await input.inputValue(),"int main(){return 0;}");
    await page.locator('.mobile-workspace-nav [data-mobile-view="output"]').click();
    await page.locator("#samples-tab").click();
    assert.equal(await page.locator("#samples-panel").isVisible(),true);
    await page.screenshot({path:"test-results/workspace-mobile.png",fullPage:true});
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});

test("progress and final review distinguish drafts, graded submissions and empty theory", async () => {
  const {page,context,state}=await workbench();
  try {
    assert.match(await page.locator("#code-progress").textContent(),/Отправлено: 0 из 2/);
    await page.locator("#problem-1 .sample-run").click();
    await page.waitForFunction(()=>document.querySelector("#run-result").textContent.includes("В очереди"));
    assert.match(await page.locator("#code-progress").textContent(),/Отправлено: 0 из 2/);
    await page.locator("#problem-1 .judge-run").click();
    await page.waitForFunction(()=>document.querySelector("#code-progress").textContent.includes("Отправлено: 1 из 2"));
    await appendCode(page,"\n// saved but not submitted"); await saved(page);
    await page.locator("#finish-button").click();
    const review=await page.locator("#finish-checklist").textContent();
    assert.ok(review.includes("код изменён после последней отправки"));
    assert.ok(review.includes("Максимум из трёх: решение не отправлено"));
    assert.ok(review.includes("Вопросов по теории без ответа: 1"));
    assert.ok(review.includes("Отправок в очереди или на проверке: 1"));
    await page.locator("#cancel-finish").click();
    await page.locator("#theory-progress").click();
    await page.locator("#answer-1").fill("Использую long long.");
    assert.match(await page.locator("#theory-progress").textContent(),/1 из 1/);
    state.taskProgress={"1":"Принято"};
    await page.waitForFunction(()=>document.querySelector("#code-progress").textContent.includes("Принято: 1"),null,{timeout:9000});
    await page.reload();
    await page.locator('#problem-1[data-editor="monaco"]').waitFor();
    await page.locator("#finish-button").click();
    await page.waitForFunction(()=>document.querySelector("#finish-checklist").textContent.includes("код изменён после последней отправки"));
    await page.locator("#cancel-finish").click();
    state.resultsVisible=false; state.taskProgress={"1":"Отправлено"};
    await page.waitForFunction(()=>document.querySelector("#code-progress").textContent.includes("Результаты после закрытия"),null,{timeout:9000});
  } finally { await context.close(); }
});

test("mobile Monaco keeps drafts across panes, tasks and viewport sizes", async () => {
  const {page,context,errors}=await workbench({mobile:true});
  try {
    await page.locator('.mobile-workspace-nav [data-mobile-view="code"]').click();
    await appendCode(page,"\n// mobile draft"); await saved(page);
    await page.locator('.mobile-workspace-nav [data-mobile-view="statement"]').click();
    await page.screenshot({path:"test-results/workspace-mobile-statement.png"});
    await page.selectOption("#task-select","2");
    await page.locator('.mobile-workspace-nav [data-mobile-view="code"]').click();
    assert.equal(await page.locator("#code-2").inputValue(),fixture.source);
    await page.selectOption("#task-select","1");
    assert.ok((await page.locator("#code-1").inputValue()).includes("mobile draft"));
    await page.screenshot({path:"test-results/workspace-mobile-editor.png"});
    for (const width of [320,768,1024,1366]) {
      await page.setViewportSize({width,height:844});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`horizontal overflow at ${width}px`);
    }
    await page.setViewportSize({width:390,height:844});
    await page.locator("#finish-button").click();
    await page.screenshot({path:"test-results/workspace-finish-review.png"});
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});

test("custom input persists per task and renders input, stdout and stderr", async () => {
  const {page,context,state,errors}=await workbench();
  try {
    await page.locator("#custom-tab").click();
    await page.locator("#custom-input").fill("7 8\n");
    await page.selectOption("#task-select","2");
    assert.equal(await page.locator("#custom-input").inputValue(),"");
    await page.locator("#custom-input").fill("2 9 3");
    await page.selectOption("#task-select","1");
    assert.equal(await page.locator("#custom-input").inputValue(),"7 8\n");
    await page.locator("#custom-run").click();
    await page.waitForFunction(()=>document.querySelector("#run-result").textContent.includes("В очереди"));
    assert.equal(state.submissions[0].kind_code,"custom");
    assert.equal(state.submissions[0].custom_input,"7 8\n");
    Object.assign(state.submissions[0],{status:"accepted",status_label:"Выполнено",result:{samples:[{stdout:"15",stderr:"note"}]}});
    await page.waitForFunction(()=>document.querySelector("#run-result").textContent.includes("Выполнено"),null,{timeout:9000});
    const output=await page.locator("#run-result").textContent();
    assert.ok(output.includes("15") && output.includes("note") && output.includes("7 8"));
    assert.equal(await page.locator(".theory-question select option").count(),1);
    await page.reload();
    await page.locator("#custom-tab").click();
    assert.equal(await page.locator("#custom-input").inputValue(),"7 8\n");
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});

test("compiler location opens submitted code after edits without replacing the draft", async () => {
  const {page,context,state,errors}=await workbench();
  try {
    await page.locator("#problem-1 .sample-run").click();
    await page.waitForFunction(()=>document.querySelector("#run-result").textContent.includes("В очереди"));
    await appendCode(page,"\n// newer draft"); await saved(page);
    Object.assign(state.submissions[0],{status:"compile_error",status_label:"Ошибка компиляции",result:{compiler:"/work/main.cpp:5:3: error: expected expression"}});
    await page.locator(".diagnostic-link").waitFor({timeout:9000});
    await page.locator(".diagnostic-link").click();
    assert.equal(await page.locator("#source-dialog").isVisible(),true);
    assert.equal(await page.locator("#submitted-source").inputValue(),fixture.source);
    assert.ok((await page.locator("#code-1").inputValue()).includes("newer draft"));
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});

test("teacher form sends score, feedback and the answer revision", async () => {
  const {page,context,state,errors}=await workbench();
  try {
    await page.goto(origin+"/teacher/attempts/101/");
  await page.locator("details summary").filter({hasText:"Связанный код"}).click();
    assert.ok((await page.locator("details pre").textContent()).includes("int main"));
    await page.locator("details summary").filter({hasText:"История оценок"}).click();
    await page.locator("details summary").filter({hasText:"Ответ версии 1"}).click();
    await page.getByText("Первый ответ", {exact:true}).waitFor();
    await page.locator('[name="q1-score"]').fill("4");
    await page.locator('[name="q1-feedback"]').fill("Тип выбран верно");
    await page.getByRole("button",{name:"Сохранить оценку"}).click();
    await page.getByText("Оценка сохранена.").waitFor();
    assert.equal(state.grade["q1-score"],"4");
    assert.equal(state.grade["q1-feedback"],"Тип выбран верно");
    assert.ok(state.grade["q1-expected_updated_at"]);
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});

test("teacher queue refreshes safely and marks stale data when disconnected", async () => {
  const {page,context,state,errors}=await workbench();
  try {
    await page.goto(origin+"/teacher/judge/");
    state.monitor.queued=7;
    state.monitor.warnings=["Очередь задерживается: старейшая отправка ожидает 150 с."];
    state.monitor.errors=[{id:200,student:'<img src=x onerror="alert(1)">',kind:"Свой ввод",attempt_id:101}];
    await page.waitForFunction(()=>document.querySelector("#worker-queued").textContent==="7",null,{timeout:11000});
    assert.ok((await page.locator("#queue-warnings").textContent()).includes("150 с"));
    assert.equal(await page.locator("#worker-errors img").count(),0);
    assert.equal(await page.locator('#worker-errors form input[name="csrfmiddlewaretoken"]').count(),1);
    state.offline=true;
    await page.locator("#monitor-stale").waitFor({timeout:11000});
    assert.equal(await page.locator("#worker-queued").textContent(),"7");
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});

test("task progress refreshes and full history follows the selected task", async () => {
  const {page,context,state,errors}=await workbench();
  try {
    await page.goto(origin+"/attempts/101/");
    state.taskProgress={"1":"Принято","2":"Есть черновик"};
    await page.waitForFunction(()=>document.querySelector('#task-select option[value="1"]').textContent.includes("Принято"), null, {timeout:9000});
    await page.selectOption("#task-select", "2");
    assert.ok((await page.locator("#full-history").getAttribute("href")).endsWith("history/?problem=2"));
    assert.ok((await page.locator('#task-select option[value="2"]').textContent()).includes("Есть черновик"));
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});
