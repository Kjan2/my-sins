/* Workbench UI. Textareas remain the canonical values for saves and final submission. */
(() => {
  "use strict";
  const root = document.querySelector("#workspace");
  if (!root) return;
  const attemptId = root.dataset.attempt;
  const base = `/attempts/${attemptId}/`;
  const eligible = root.dataset.canEdit === "true";
  const csrf = root.querySelector("[name=csrfmiddlewaretoken]").value;
  const clock = JSON.parse(document.querySelector("#clock-data").textContent);
  const message = document.querySelector("#workspace-message");
  const taskSelect = document.querySelector("#task-select");
  const finishDialog = document.querySelector("#finish-dialog");
  const runResult = document.querySelector("#run-result");
  let closed = clock.closed, operation = null, leaving = false;
  let serverTime = Date.parse(clock.server_now), deadline = Date.parse(clock.deadline), syncAt = performance.now();
  let submissions = JSON.parse(document.querySelector("#submission-data").textContent);
  const latestJudged = new Map(Object.entries(JSON.parse(document.querySelector("#latest-judge-data").textContent) || {}));
  let selectedProblem = taskSelect.value;
  let fontSize = 14;
  try { fontSize = Math.max(12, Math.min(20, Number(localStorage.getItem("department:editor-font")) || 14)); } catch (_) {}
  const fields = [];
  const pendingKey = `department:${attemptId}:pending-submission`;
  let pendingSubmission = null, conflictField = null;
  const conflictDialog = document.querySelector("#conflict-dialog");
  const selections = new Map(), resultCache = new Map();
  let historySignature = "", shownResultKey = "", pendingResultKey = "", resultRequest = 0, pollBusy = false;

  function notice(text, kind = "info") {
    message.textContent = text; message.dataset.kind = kind; message.hidden = false;
  }
  function setStatus(field, text, state) {
    field.status.textContent = text; field.status.dataset.state = state;
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function canSave() { return eligible && !closed; }
  function updateProgress() {
    // The server's progress covers all submissions, including those outside the recent history.
    for (const item of submissions) {
      if (item.kind_code !== "judge") continue;
      const id = String(item.problem_id), known = latestJudged.get(id);
      if (!known || item.id > known.id) latestJudged.set(id, {id: item.id, source: item.source});
    }
    const options = [...taskSelect.options];
    const sent = options.filter(option => latestJudged.has(option.value) || ["Принято", "Отправлено"].includes(option.dataset.progress)).length;
    const accepted = options.filter(option => option.dataset.progress === "Принято").length;
    const codeProgress = document.querySelector("#code-progress");
    const progressText = `Отправлено: ${sent} из ${options.length} · ${root.dataset.resultsVisible === "true" ? `Принято: ${accepted}` : "Результаты после закрытия"}`;
    if (codeProgress.textContent !== progressText) codeProgress.textContent = progressText;
    const answers = fields.filter(field => !field.code);
    const theoryProgress = document.querySelector("#theory-progress");
    if (theoryProgress) theoryProgress.textContent = `Теория заполнена: ${answers.filter(field => field.input.value.trim()).length} из ${answers.length}`;
    if (finishDialog?.open) renderFinishChecklist();
  }
  function renderFinishChecklist() {
    const checklist = document.querySelector("#finish-checklist");
    if (!checklist) return;
    const items = [];
    for (const field of codeFields) {
      const title = [...taskSelect.options].find(option => option.value === field.id).dataset.title;
      const latest = latestJudged.get(field.id);
      if (!latest) items.push(`${title}: решение не отправлено на оценку.`);
      else if (latest.source === undefined) items.push(`${title}: код последней отправки ещё не сверен. Проверьте историю отправок.`);
      else if (field.input.value !== latest.source) items.push(`${title}: код изменён после последней отправки. Изменения не будут оценены.`);
    }
    const emptyAnswers = fields.filter(field => !field.code && !field.input.value.trim()).length;
    if (emptyAnswers) items.push(`Вопросов по теории без ответа: ${emptyAnswers}.`);
    const checking = submissions.filter(item => item.kind_code === "judge" && ["queued", "running"].includes(item.status)).length;
    if (checking) items.push(`Отправок в очереди или на проверке: ${checking}. Они будут проверены после завершения.`);
    checklist.replaceChildren(...items.map(text => element("li", "", text)));
    checklist.hidden = items.length === 0;
  }
  function showMobileView(view) {
    root.dataset.mobileView = view;
    root.querySelectorAll(".mobile-workspace-nav button").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.mobileView === view)));
  }
  root.querySelectorAll(".mobile-workspace-nav button").forEach(button => button.addEventListener("click", () => showMobileView(button.dataset.mobileView)));
  document.querySelector("#theory-progress")?.addEventListener("click", () => {
    showMobileView("statement"); selectTab(document.querySelector("#theory-tab"));
    document.querySelector("#theory-tab").focus();
  });
  function updateEditable() {
    const writable = canSave() && !operation;
    const closedNotice = document.querySelector("#closed-notice");
    if (closedNotice) closedNotice.hidden = eligible && !closed;
    fields.forEach(field => {
      field.input.readOnly = !writable;
      field.editor?.setReadOnly(!writable);
      if (field.select) field.select.disabled = !writable;
    });
    root.querySelectorAll(".judge-run").forEach(button => { button.disabled = !writable || !!pendingSubmission; });
    root.querySelectorAll(".custom-run").forEach(button => { button.disabled = !writable || !!pendingSubmission; });
    const stdin = document.querySelector("#custom-input");
    if (stdin) stdin.readOnly = !writable;
    root.querySelectorAll(".sample-run").forEach(button => { button.disabled = !writable || !!pendingSubmission || button.dataset.hasSamples !== "true"; });
    document.querySelector("#retry-submission").disabled = !!operation;
    document.querySelector("#save-all").disabled = !writable;
    const finish = document.querySelector("#finish-button");
    if (finish) finish.disabled = !writable || !!pendingSubmission;
    const confirmFinish = document.querySelector("#confirm-finish"), cancelFinish = document.querySelector("#cancel-finish");
    if (confirmFinish) confirmFinish.disabled = !canSave() || !!operation || !!pendingSubmission;
    if (cancelFinish) cancelFinish.disabled = !!operation;
    if (closed && finishDialog?.open && operation !== "finish") {
      const error = document.querySelector("#finish-error");
      error.textContent = "Время работы истекло. Новые изменения сохранить нельзя; текст остался в редакторе.";
      error.hidden = false;
    }
    taskSelect.disabled = !!operation;
    document.querySelector("#previous-task").disabled = !!operation || taskSelect.selectedIndex === 0;
    document.querySelector("#next-task").disabled = !!operation || taskSelect.selectedIndex === taskSelect.options.length - 1;
  }
  function sync(data) {
    if (!data.server_now) return;
    if (typeof data.results_visible === "boolean") root.dataset.resultsVisible = String(data.results_visible);
    if (data.task_progress) for (const option of taskSelect.options) {
      if (data.task_progress[option.value]) {
        option.dataset.progress = data.task_progress[option.value];
        option.textContent = `${option.dataset.title} · ${option.dataset.progress}`;
      }
    }
    serverTime = Date.parse(data.server_now); deadline = Date.parse(data.deadline); syncAt = performance.now();
    if (closed !== data.closed) {
      closed = data.closed;
      if (eligible) notice(closed ? "Редактирование закрыто. Несохранённый текст остался в редакторе — при необходимости скопируйте его." : "Срок продлён. Можно продолжить работу и сохранить изменения.", "clock");
    }
    document.querySelector("#deadline-label").textContent = new Date(deadline).toLocaleString("ru-RU", {timeZone: "Europe/Moscow"}) + " МСК";
    document.querySelector(".workspace-clock").title = "До " + document.querySelector("#deadline-label").textContent;
    updateEditable();
  }
  async function requestJSON(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {...options, redirect: "error", signal: controller.signal});
      let data;
      try { data = await response.json(); }
      catch (_) { throw new Error("Сервер не подтвердил запрос. Проверьте соединение и вход в аккаунт."); }
      if (!response.ok) {
        const error = new Error(data.error || "Запрос не выполнен. Повторите попытку.");
        error.status = response.status; error.conflict = data.conflict;
        throw error;
      }
      return data;
    } catch (error) {
      if (controller.signal.aborted) throw new Error("Сервер не ответил за 15 секунд. Результат запроса неизвестен. Текст остался в редакторе.");
      if (error instanceof TypeError) throw new Error("Нет связи с сервером. Результат запроса неизвестен; текст остался в редакторе.");
      throw error;
    } finally { clearTimeout(timer); }
  }
  async function post(route, body) {
    const data = await requestJSON(base + route, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf}, body: JSON.stringify(body)});
    sync(data); return data;
  }
  function sameText(left, right) {
    return (left.source ?? left.text) === (right.source ?? right.text) && String(left.submission || "") === String(right.submission || "");
  }
  function clearLocal(field, saved = field.payload()) {
    try {
      const stored = JSON.parse(localStorage.getItem(field.key));
      if (stored && sameText(stored, saved)) localStorage.removeItem(field.key);
    } catch (_) {}
  }

  root.querySelectorAll(".problem, .theory-question").forEach(section => {
    const code = section.classList.contains("problem");
    const input = section.querySelector("textarea"), select = section.querySelector("select");
    const itemId = code ? section.dataset.problem : section.dataset.question;
    const field = {section, input, select, code, id: itemId, status: section.querySelector(".save-status"),
      key: `department:${attemptId}:${code ? "code" : "theory"}:${itemId}`, dirty: false, revision: 0,
      serverRevision: Number(section.dataset.revision), conflict: null,
      chain: Promise.resolve(), timer: null, editor: null, assigning: false, localSaved: true};
    field.payload = () => code ? {type: "code", problem: itemId, source: input.value, expected_revision: field.serverRevision} : {type: "theory", question: itemId, text: input.value, submission: select?.value || null, expected_revision: field.serverRevision};
    field.store = () => {
      try { localStorage.setItem(field.key, JSON.stringify(field.payload())); field.localSaved = true; }
      catch (_) { field.localSaved = false; }
    };
    field.change = () => {
      if (field.assigning) return;
      field.revision++; field.dirty = true; field.store();
      if (!code) updateProgress();
      setStatus(field, field.localSaved ? "Есть несохранённые изменения" : "Не сохранено. Локальная копия недоступна — дождитесь связи с сервером.", "dirty");
      clearTimeout(field.timer);
      if (canSave() && !operation && !field.conflict) field.timer = setTimeout(() => saveField(field).catch(() => {}), 900);
    };
    field.assign = value => {
      field.assigning = true;
      try { if (field.editor) field.editor.setValue(value); else input.value = value; }
      finally { field.assigning = false; }
    };
    input.addEventListener("input", field.change);
    select?.addEventListener("change", field.change);
    input.addEventListener("keydown", event => {
      if (code && event.key === "Tab" && canSave() && !operation) {
        event.preventDefault(); input.setRangeText("    ", input.selectionStart, input.selectionEnd, "end"); field.change();
      }
    });
    fields.push(field);
  });
  const codeFields = fields.filter(field => field.code);
  const customInput = document.querySelector("#custom-input");
  const customValues = new Map();
  const customKey = id => `department:${attemptId}:stdin:${id}`;
  codeFields.forEach(field => {
    try { customValues.set(field.id, localStorage.getItem(customKey(field.id)) || ""); }
    catch (_) { customValues.set(field.id, ""); }
  });
  customInput.addEventListener("input", () => {
    customValues.set(selectedProblem, customInput.value);
    const status = document.querySelector("#custom-storage-status");
    try { localStorage.setItem(customKey(selectedProblem), customInput.value); status.textContent = "Ввод сохранён в этом браузере."; }
    catch (_) { status.textContent = "Локальное сохранение недоступно. Скопируйте ввод перед уходом."; }
  });

  function mountEditor(field) {
    if (field.editor) { requestAnimationFrame(() => field.editor.layout()); return; }
    try {
      field.editor = window.ContestEditor.mount(field.input, {
        readOnly: !canSave() || !!operation, fontSize, onChange: field.change,
        onCursor: position => { field.section.querySelector(".cursor-position").textContent = `Стр. ${position.lineNumber}, стлб. ${position.column}`; },
      });
      field.section.dataset.editor = "monaco";
      document.querySelector("#editor-engine").textContent = "Подсветка C++";
    } catch (_) {
      document.querySelector("#editor-engine").textContent = "Текстовый режим";
      field.input.hidden = false;
    }
  }
  window.addEventListener("contest:editor-ready", () => mountEditor(codeFields.find(field => field.id === selectedProblem)));
  async function saveField(field) {
    clearTimeout(field.timer);
    const save = async () => {
      if (!field.dirty) return;
      if (field.conflict) { showConflict(field); throw new Error("Сначала разрешите конфликт текстов."); }
      if (!canSave()) { setStatus(field, "Редактирование закрыто; текст можно скопировать.", "error"); throw new Error("Время работы истекло или работа завершена."); }
      const revision = field.revision, body = field.payload();
      setStatus(field, "Сохраняем…", "saving");
      try {
        const data = await post("save/", body);
        field.serverRevision = data.revision;
        if (revision === field.revision) {
          field.dirty = false;
          setStatus(field, "Сохранено на сервере", "saved");
          clearLocal(field, body);
        } else field.store();
      } catch (error) {
        if (error.conflict) { field.conflict = error.conflict; field.store(); showConflict(field); }
        setStatus(field, error.message + (field.localSaved ? "" : " Локальная копия недоступна."), "error");
        throw error;
      }
    };
    // Serialize writes per field, including retries, to prevent an old request overwriting new text.
    field.chain = field.chain.catch(() => {}).then(save);
    return field.chain;
  }
  async function saveAll() { await Promise.all(fields.map(saveField)); }

  function showConflict(field) {
    field.compare.hidden = false;
    conflictField = field;
    document.querySelector("#conflict-local").value = field.input.value;
    document.querySelector("#conflict-server").value = field.conflict.source ?? field.conflict.text;
    const linkLabel = value => value ? `Связанная отправка: #${value}` : "Связанная отправка: без связи";
    document.querySelector("#conflict-local-link").textContent = field.code ? "" : linkLabel(field.select?.value);
    document.querySelector("#conflict-server-link").textContent = field.code ? "" : linkLabel(field.conflict.submission);
    if (!conflictDialog.open && !finishDialog?.open) conflictDialog.showModal();
    notice("Есть конфликт текстов. Нажмите «Сравнить версии» у поля перед сохранением.", "error");
  }
  fields.forEach(field => {
    const compare = element("button", "compare-conflict", "Сравнить версии"); compare.type = "button"; compare.hidden = true;
    field.status.after(compare); field.compare = compare;
    compare.onclick = () => { if (field.conflict) showConflict(field); };
  });
  // Buttons remain available after closing the dialog or a failed finish request.
  function resolveConflict(useServer) {
    const field = conflictField;
    if (!field?.conflict) return;
    const server = field.conflict;
    if (!useServer && !canSave()) { notice("Срок истёк. Локальный текст доступен для копирования.", "error"); return; }
    field.serverRevision = server.revision;
    field.conflict = null; field.compare.hidden = true;
    if (useServer) {
      clearLocal(field);
      field.assign(server.source ?? server.text);
      if (field.select) {
        if (server.submission && ![...field.select.options].some(option => option.value === String(server.submission))) field.select.add(new Option(`Отправка #${server.submission}`, server.submission));
        field.select.value = server.submission || "";
      }
      field.revision++; field.dirty = false;
      setStatus(field, "Загружено с сервера", "saved");
    } else { field.change(); saveField(field).catch(() => {}); }
    conflictDialog.close(); updateProgress();
  }
  document.querySelector("#conflict-use-server").onclick = () => resolveConflict(true);
  document.querySelector("#conflict-keep-local").onclick = () => resolveConflict(false);
  document.querySelector("#conflict-close").onclick = () => conflictDialog.close();

  const recovered = [];
  fields.forEach(field => {
    try {
      const saved = JSON.parse(localStorage.getItem(field.key));
      if (saved && typeof (saved.source ?? saved.text) === "string" && !sameText(saved, field.payload())) recovered.push({field, saved});
    } catch (_) {}
  });
  if (recovered.length) {
    const panel = element("div", "workspace-notice", "В этом браузере есть несохранённые изменения предыдущего сеанса. ");
    const restore = element("button", "", "Восстановить в редактор"); restore.type = "button";
    restore.onclick = () => {
      recovered.forEach(({field, saved}) => {
        if (field.select) {
          if (saved.submission && ![...field.select.options].some(option => option.value === String(saved.submission))) field.select.add(new Option(`Отправка #${saved.submission}`, saved.submission));
          field.select.value = saved.submission ? String(saved.submission) : "";
        }
        field.assign(saved.source ?? saved.text);
        if (Number.isInteger(saved.expected_revision)) field.serverRevision = saved.expected_revision;
        field.change();
        if (!canSave()) setStatus(field, "Локальный текст восстановлен для просмотра. Сохранение закрыто.", "error");
      });
      panel.remove();
    };
    panel.append(restore); message.before(panel);
  }

  function selectTab(button) {
    button.closest('[role="tablist"]').querySelectorAll('[role="tab"]').forEach(tab => {
      const selected = tab === button;
      tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1;
      document.getElementById(tab.dataset.tab).hidden = !selected;
    });
  }
  root.querySelectorAll('[role="tablist"]').forEach(tablist => {
    const tabs = [...tablist.querySelectorAll('[role="tab"]')];
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => selectTab(tab));
      tab.addEventListener("keydown", event => {
        let target;
        if (event.key === "ArrowRight") target = tabs[(index + 1) % tabs.length];
        if (event.key === "ArrowLeft") target = tabs[(index + tabs.length - 1) % tabs.length];
        if (event.key === "Home") target = tabs[0];
        if (event.key === "End") target = tabs[tabs.length - 1];
        if (target) { event.preventDefault(); selectTab(target); target.focus(); }
      });
    });
  });

  function renderHistory() {
    const items = submissions.filter(item => String(item.problem_id) === selectedProblem);
    const signature = JSON.stringify([selectedProblem, selections.get(selectedProblem), items]);
    if (signature === historySignature) return;
    historySignature = signature;
    document.querySelector("#submission-count").textContent = items.length;
    const list = document.querySelector("#submission-list"); list.replaceChildren();
    if (!items.length) { list.append(element("p", "output-placeholder", "Пока нет отправок. Здесь появится история запусков этой задачи.")); return; }
    items.forEach(item => {
      const link = element("a", "submission-row"); link.href = `/submissions/${item.id}/`; link.dataset.submission = item.id;
      if (selections.get(selectedProblem) === item.id) link.classList.add("is-selected");
      const title = element("span", "", `#${item.id}`); title.append(element("small", "", item.kind));
      const time = element("time", "", new Date(item.created_at).toLocaleTimeString("ru-RU", {timeZone: "Europe/Moscow"})); time.dateTime = item.created_at;
      link.append(title, time, element("span", `verdict-${item.status}`, item.status_label));
      link.addEventListener("click", event => {
        if (event.ctrlKey || event.metaKey || event.shiftKey) return;
        event.preventDefault(); selections.set(selectedProblem, item.id); renderHistory(); showSelectedResult(); selectTab(document.querySelector("#result-tab"));
      });
      list.append(link);
    });
  }
  function showEmptyResult() {
    const empty = element("div", "output-empty");
    empty.append(element("span", "empty-play", "▷"), element("h3", "", "Проверьте своё решение"), element("p", "", "Запустите примеры, чтобы увидеть вывод программы. Для оценки отправьте решение на проверку."));
    runResult.replaceChildren(empty);
  }
  function renderResult(item) {
    const heading = element("div", "result-heading");
    const title = element("h3", `verdict-${item.status}`, item.status_label);
    if (["queued", "running"].includes(item.status)) title.prepend(element("span", "result-spinner"));
    const link = element("a", "", `Код отправки #${item.id} ↗`); link.href = `/submissions/${item.id}/`;
    heading.append(title, link);
    runResult.replaceChildren(heading, element("p", "result-caption", item.kind_code === "sample" ? "Запуск открытых примеров · баллы не начисляются" : item.kind_code === "custom" ? "Свой ввод · баллы не начисляются" : "Отправка на оценку · C++17"));
    const notes = {
      queued: "Решение стоит в очереди. Результат появится здесь автоматически.",
      running: "Компилируем и проверяем программу. Можно продолжать редактирование.",
      received: "Отправка принята. Результаты откроются после общего закрытия работы.",
      accepted: item.kind_code === "custom" ? "Программа выполнена. Вывод не сравнивался с эталоном." : item.kind_code === "sample" ? "Все открытые примеры пройдены." : "Все тесты пройдены. Баллы доступны на странице итогов.",
      wrong_answer: "Ответ программы не совпал с ожидаемым.",
      compile_error: "Не удалось скомпилировать программу. Проверьте сообщения компилятора.",
      runtime_error: "Программа завершилась с ошибкой.",
      limit_exceeded: "Превышено ограничение времени, памяти или размера вывода.",
      infra_error: "Проверка временно недоступна. Преподаватель может повторить её.",
    };
    runResult.append(element("p", "result-note", item.result?.message || notes[item.status] || "Результат получен."));
    if (item.result?.compiler) {
      const output = element("pre", "compiler-output");
      for (const line of item.result.compiler.split("\n")) {
        const match = line.match(/(?:^|\/)(main\.cpp:(\d+)(?::(\d+))?)/);
        if (match) {
          const offset = match.index + match[0].indexOf("main.cpp");
          output.append(document.createTextNode(line.slice(0, offset)));
          const button = element("button", "diagnostic-link", match[1]); button.type = "button";
          button.addEventListener("click", () => revealDiagnostic(item, Number(match[2]), Number(match[3]) || 1));
          output.append(button, document.createTextNode(line.slice(offset + match[1].length) + "\n"));
        } else output.append(document.createTextNode(line + "\n"));
      }
      runResult.append(output);
    }
    if (item.kind_code === "custom" && item.result?.samples?.length) {
      const run = item.result.samples[0];
      for (const [label, text] of [["Ввод", item.custom_input], ["Вывод", run.stdout], ["stderr", run.stderr]]) {
        runResult.append(element("h3", "console-label", label), element("pre", "compiler-output", text || "(пусто)"));
      }
    }
    if (item.kind_code === "sample") {
      const examples = [...root.querySelector(`[data-statement="${item.problem_id}"]`).querySelectorAll(".example-case")];
      (item.result?.samples || []).forEach((sample, index) => {
        const row = element("section", "console-example");
        const label = element("h3", `verdict-${sample.status}`, `${sample.label} · ${sample.status === "accepted" ? "Пройден" : "Не пройден"}`);
        const columns = element("div", "console-columns");
        const actual = element("div"), expected = element("div"), input = element("div");
        input.append(element("span", "console-label", "Ввод"), element("pre", "", examples[index]?.querySelector("[data-sample-input]").textContent || "(пустой ввод)"));
        actual.append(element("span", "console-label", "Полученный вывод"), element("pre", "", sample.stdout || "(пустой вывод)"));
        expected.append(element("span", "console-label", "Ожидаемый вывод"), element("pre", "", examples[index]?.querySelector("[data-sample-expected]").textContent || "(пустой вывод)"));
        columns.append(input, expected, actual); row.append(label, columns);
        if (sample.stderr) row.append(element("pre", "console-stderr", sample.stderr));
        runResult.append(row);
      });
    }
  }
  function selectSourceLine(input, line, column) {
    const lines = input.value.split("\n");
    line = Math.max(1, Math.min(lines.length, line));
    const offset = lines.slice(0, line - 1).reduce((total, text) => total + text.length + 1, 0);
    const start = offset + Math.min(lines[line - 1].length, Math.max(0, column - 1));
    input.focus(); input.setSelectionRange(start, offset + lines[line - 1].length);
    input.scrollTop = Math.max(0, (line - 3) * 23);
  }
  function revealDiagnostic(item, line, column) {
    const field = codeFields.find(field => field.id === String(item.problem_id));
    if (field.input.value === item.source) {
      showMobileView("code");
      selectProblem(field.id);
      if (field.editor) field.editor.reveal(line, column);
      else selectSourceLine(field.input, line, column);
    } else {
      const dialog = document.querySelector("#source-dialog"), input = document.querySelector("#submitted-source");
      document.querySelector("#source-title").textContent = `Код отправки #${item.id} · строка ${line}`;
      input.value = item.source || ""; dialog.showModal(); selectSourceLine(input, line, column);
    }
  }
  document.querySelector("#close-source").addEventListener("click", () => document.querySelector("#source-dialog").close());
  async function showSelectedResult() {
    const items = submissions.filter(item => String(item.problem_id) === selectedProblem);
    const selected = items.find(item => item.id === selections.get(selectedProblem)) || items[0];
    const key = selected ? `${selected.id}:${selected.status}` : `empty:${selectedProblem}`;
    if (shownResultKey === key || pendingResultKey === key) return;
    const request = ++resultRequest; pendingResultKey = "";
    if (!selected) { showEmptyResult(); shownResultKey = key; return; }
    if (["queued", "running", "received"].includes(selected.status)) { renderResult(selected); shownResultKey = key; return; }
    if (resultCache.has(key)) { renderResult(resultCache.get(key)); shownResultKey = key; return; }
    pendingResultKey = key;
    runResult.replaceChildren(element("p", "output-placeholder", "Загружаем результат…"));
    try {
      const data = await requestJSON(`/submissions/${selected.id}/state/`);
      const item = data.submission;
      resultCache.set(`${item.id}:${item.status}`, item);
      if (resultCache.size > 8) resultCache.delete(resultCache.keys().next().value);
      if (request !== resultRequest) return;
      renderResult(item); shownResultKey = `${item.id}:${item.status}`;
    } catch (error) { if (request === resultRequest) runResult.replaceChildren(element("p", "output-placeholder", "Не удалось загрузить результат. Обновление повторится автоматически.")); }
    finally { if (request === resultRequest) pendingResultKey = ""; }
  }
  function selectProblem(id) {
    const field = codeFields.find(item => item.id === String(id)) || codeFields[0];
    selectedProblem = field.id; taskSelect.value = field.id;
    const historyLink = document.querySelector("#full-history");
    if (historyLink) historyLink.href = `${base}history/?problem=${selectedProblem}`;
    customInput.value = customValues.get(selectedProblem) || "";
    codeFields.forEach(item => { item.section.hidden = item !== field; });
    root.querySelectorAll("[data-statement]").forEach(node => { node.hidden = node.dataset.statement !== selectedProblem; });
    root.querySelectorAll("[data-case-list]").forEach(node => { node.hidden = node.dataset.caseList !== selectedProblem; });
    document.querySelector("#output-task-label").textContent = taskSelect.selectedOptions[0].textContent;
    mountEditor(field); updateEditable(); renderHistory(); showSelectedResult();
  }
  taskSelect.addEventListener("change", () => { history.replaceState(null, "", `#problem-${taskSelect.value}`); selectProblem(taskSelect.value); });
  for (const [name, direction] of [["previous-task", -1], ["next-task", 1]]) document.getElementById(name).addEventListener("click", () => {
    const option = taskSelect.options[taskSelect.selectedIndex + direction];
    if (option) { taskSelect.value = option.value; taskSelect.dispatchEvent(new Event("change")); }
  });
  window.addEventListener("hashchange", () => selectProblem(location.hash.replace(/^#problem-/, "")));

  async function poll() {
    if (pollBusy || document.hidden) return;
    pollBusy = true;
    try {
      const data = await requestJSON(base + "state/");
      sync(data); submissions = data.submissions; updateProgress(); renderHistory(); showSelectedResult();
      if (message.dataset.kind === "poll") message.hidden = true;
    } catch (_) { notice("Не удалось обновить результаты. Проверяем соединение и повторяем запрос.", "poll"); }
    finally { pollBusy = false; }
  }
  async function run(field, kind) {
    if (!canSave() || operation || pendingSubmission) return;
    operation = "submit"; updateEditable();
    showMobileView("output");
    selectTab(document.querySelector("#result-tab"));
    const source = field.input.value;
    try {
      // Also persist an untouched starter template so its revision is known before sending.
      field.dirty = true; field.store();
      await saveAll();
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
      const hex = [...bytes].map(value => value.toString(16).padStart(2, "0")).join("");
      const requestId = `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
      const payload = {problem: field.id, source, kind, expected_revision: field.serverRevision, request_id: requestId};
      if (kind === "custom") payload.custom_input = customValues.get(field.id) || "";
      pendingSubmission = payload;
      try { localStorage.setItem(`${pendingKey}:${payload.request_id}`, JSON.stringify(payload)); }
      catch (_) { notice("Локальное хранилище недоступно. При потере связи повторите запрос до перезагрузки страницы.", "error"); }
      renderPending();
      await sendPending();
    } catch (error) { notice(error.message, "error"); }
    finally { operation = null; updateEditable(); }
  }
  function renderPending() {
    document.querySelector("#pending-submission").hidden = !pendingSubmission;
    if (pendingSubmission) {
      const title = [...taskSelect.options].find(option => option.value === String(pendingSubmission.problem))?.textContent || "Задача";
      const kind = {judge: "на проверку", sample: "примеры", custom: "свой ввод"}[pendingSubmission.kind];
      document.querySelector("#pending-submission-label").textContent = `${title} · ${kind}: получение отправки ещё не подтверждено. Повтор запроса сохранит одну отправку. Отправленный код доступен для просмотра.`;
    }
  }
  function restorePending() {
    try {
      for (let index = 0; index < localStorage.length; index++) {
        const key = localStorage.key(index);
        if (!key.startsWith(pendingKey + ":")) continue;
        const pending = JSON.parse(localStorage.getItem(key));
        if (pending && typeof pending.request_id === "string" && typeof pending.source === "string" &&
            codeFields.some(field => field.id === String(pending.problem)) && ["judge", "sample", "custom"].includes(pending.kind)) return pending;
      }
    } catch (_) {}
    return null;
  }
  function clearPending(payload) {
    try { localStorage.removeItem(`${pendingKey}:${payload.request_id}`); } catch (_) {}
    pendingSubmission = restorePending();
  }
  function receiveConflict(error) {
    if (!error.conflict) return;
    const field = fields.find(item => item.code === (error.conflict.type === "code") && item.id === String(error.conflict.problem ?? error.conflict.question));
    if (field) { field.conflict = error.conflict; field.store(); showConflict(field); }
  }
  async function sendPending() {
    const payload = pendingSubmission;
    try {
      const data = await post("submit/", payload);
      const item = data.submission;
      submissions = [item, ...submissions.filter(row => row.id !== item.id)].slice(0, 50);
      if (payload.kind === "judge") latestJudged.set(String(item.problem_id), {id: item.id, source: payload.source});
      updateProgress();
      selections.set(String(item.problem_id), item.id);
      if (payload.kind === "judge") root.querySelectorAll(`.theory-question select[data-problem="${item.problem_id}"]`).forEach(select => {
        if (![...select.options].some(option => option.value === String(item.id))) select.add(new Option(`Отправка #${item.id}`, item.id));
      });
      renderHistory(); showSelectedResult();
      clearPending(payload);
      notice(`Отправка #${item.id} принята. Результат обновится автоматически.`, "submission");
    } catch (error) {
      if ([400, 403, 404, 409].includes(error.status)) {
        clearPending(payload);
      }
      receiveConflict(error);
      throw error;
    } finally { renderPending(); }
  }
  pendingSubmission = restorePending();
  renderPending();
  window.addEventListener("storage", event => {
    if (!operation && event.key?.startsWith(pendingKey + ":")) {
      pendingSubmission = restorePending(); renderPending(); updateEditable();
    }
  });
  document.querySelector("#retry-submission").onclick = async () => {
    if (operation || !pendingSubmission) return;
    operation = "submit"; updateEditable();
    try { await sendPending(); }
    catch (error) { notice(error.message, "error"); }
    finally { operation = null; updateEditable(); }
  };
  document.querySelector("#preview-pending-submission").onclick = () => {
    document.querySelector("#submitted-source").value = pendingSubmission.source;
    document.querySelector("#source-dialog").showModal();
  };
  codeFields.forEach(field => {
    field.section.querySelector(".sample-run")?.addEventListener("click", () => run(field, "sample"));
    field.section.querySelector(".judge-run")?.addEventListener("click", () => run(field, "judge"));
  });
  document.querySelector("#custom-run")?.addEventListener("click", () => run(codeFields.find(field => field.id === selectedProblem), "custom"));
  document.querySelector("#save-all").addEventListener("click", async () => {
    if (!canSave() || operation) return;
    try { await saveAll(); notice(fields.some(field => field.dirty) ? "Новые изменения ещё сохраняются." : "Код и ответы сохранены на сервере.", "saved"); }
    catch (error) { notice(error.message, "error"); }
  });

  document.querySelector("#finish-button")?.addEventListener("click", () => {
    renderFinishChecklist(); finishDialog.showModal();
    // Load code only when comparing it; the page and periodic polling use metadata.
    for (const [id, latest] of latestJudged) {
      if (latest.source !== undefined) continue;
      requestJSON(`/submissions/${latest.id}/state/`).then(data => {
        if (latestJudged.get(id)?.id === latest.id) {
          latest.source = data.submission.source;
          if (finishDialog.open) renderFinishChecklist();
        }
      }).catch(() => {});
    }
  });
  document.querySelector("#cancel-finish")?.addEventListener("click", () => { if (!operation) finishDialog.close(); });
  finishDialog?.addEventListener("cancel", event => { if (operation) event.preventDefault(); });
  document.querySelector("#confirm-finish")?.addEventListener("click", async () => {
    if (!canSave() || operation || pendingSubmission) return;
    operation = "finish"; updateEditable();
    const confirmButton = document.querySelector("#confirm-finish"), cancelButton = document.querySelector("#cancel-finish"), errorNode = document.querySelector("#finish-error");
    confirmButton.disabled = cancelButton.disabled = true; errorNode.hidden = true;
    try {
      await saveAll();
      const data = await post("finish/", {confirm: true, drafts: codeFields.map(field => field.payload()), answers: fields.filter(field => !field.code).map(field => field.payload())});
      fields.forEach(field => { field.dirty = false; clearLocal(field); });
      leaving = true; location.href = data.url;
    } catch (error) { receiveConflict(error); errorNode.textContent = error.message + " Досрочное завершение не подтверждено. Текст остался в редакторе."; errorNode.hidden = false; }
    finally { operation = null; confirmButton.disabled = cancelButton.disabled = false; updateEditable(); }
  });
  for (const [name, delta] of [["font-smaller", -1], ["font-larger", 1]]) document.getElementById(name).addEventListener("click", () => {
    fontSize = Math.max(12, Math.min(20, fontSize + delta));
    codeFields.forEach(field => { field.editor?.setFontSize(fontSize); field.input.style.fontSize = `${fontSize}px`; });
    try { localStorage.setItem("department:editor-font", fontSize); } catch (_) {}
  });
  document.addEventListener("keydown", event => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
    if (event.key.toLowerCase() === "s") { event.preventDefault(); event.stopPropagation(); document.querySelector("#save-all").click(); }
    if (event.key === "Enter" && document.querySelector(".editor-panel").contains(document.activeElement)) {
      event.preventDefault(); event.stopPropagation();
      const field = codeFields.find(item => item.id === selectedProblem);
      const button = field.section.querySelector(event.shiftKey ? ".judge-run" : ".sample-run");
      if (button && !button.disabled) button.click();
    }
  }, true);
  window.addEventListener("beforeunload", event => { if (!leaving && fields.some(field => field.dirty)) { event.preventDefault(); event.returnValue = ""; } });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) fields.filter(field => field.dirty).forEach(field => { field.store(); if (!operation) saveField(field).catch(() => {}); });
    else poll();
  });

  const split = document.querySelector("#workspace-split"), resizer = document.querySelector("#pane-resizer");
  function setPaneWidth(value) {
    value = Math.max(28, Math.min(65, value));
    split.style.setProperty("--statement-width", `${value}%`); resizer.setAttribute("aria-valuenow", Math.round(value));
    try { localStorage.setItem("department:statement-width", value); } catch (_) {}
  }
  try { const value = Number(localStorage.getItem("department:statement-width")); if (value) setPaneWidth(value); } catch (_) {}
  resizer.addEventListener("pointerdown", event => { if (event.button === 0) { resizer.setPointerCapture(event.pointerId); document.body.classList.add("is-resizing"); } });
  resizer.addEventListener("pointermove", event => { if (resizer.hasPointerCapture(event.pointerId)) { const rect = split.getBoundingClientRect(); setPaneWidth((event.clientX - rect.left) / rect.width * 100); } });
  for (const eventName of ["pointerup", "pointercancel", "lostpointercapture"]) resizer.addEventListener(eventName, event => { document.body.classList.remove("is-resizing"); if (resizer.hasPointerCapture(event.pointerId)) resizer.releasePointerCapture(event.pointerId); });
  resizer.addEventListener("keydown", event => {
    const current = Number(resizer.getAttribute("aria-valuenow"));
    const values = {ArrowLeft: current - 2, ArrowRight: current + 2, Home: 28, End: 65};
    if (event.key in values) { event.preventDefault(); setPaneWidth(values[event.key]); }
  });

  sync(clock);
  updateProgress();
  selectProblem(location.hash.replace(/^#problem-/, "") || selectedProblem);
  let deadlineFlush = false;
  function tick() {
    const remaining = Math.max(0, deadline - serverTime - (performance.now() - syncAt));
    const seconds = Math.ceil(remaining / 1000);
    document.querySelector("#timer").textContent = `${String(Math.floor(seconds / 3600)).padStart(2, "0")}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
    document.querySelector(".workspace-clock").classList.toggle("is-urgent", seconds <= 300 && eligible);
    if (remaining > 2000) deadlineFlush = false;
    if (remaining > 0 && remaining <= 2000 && !deadlineFlush && canSave() && !operation) { deadlineFlush = true; saveAll().catch(() => {}); }
    if (!remaining && !closed) { closed = true; updateEditable(); if (eligible) notice("Время работы истекло. Несохранённый текст остался в редакторе и доступен для копирования.", "clock"); }
  }
  tick();
  setInterval(tick, 250);
  setInterval(poll, 4000);
  setInterval(() => { if (canSave() && !operation) fields.filter(field => field.dirty && !field.conflict).forEach(field => saveField(field).catch(() => {})); }, 8000);
})();
