(() => {
  'use strict';
  const root = document.querySelector('#task-workspace');
  if (!root) return;
  const id = root.dataset.task, base = `/tasks/${id}/`, csrf = root.querySelector('[name=csrfmiddlewaretoken]').value;
  let clock = JSON.parse(document.querySelector('#task-clock').textContent), syncAt = performance.now();
  let revision = clock.revision, closed = clock.closed, generation = 0, savedGeneration = 0, saving = null, debounce;
  let busy = false, polling = false, editorMounted = false, hideTimer = false, serverQuestion = clock.current_question;
  const editable = root.dataset.editable === 'true', key = `contest:task:${id}:draft`;
  const status = document.querySelector('#save-state'), error = document.querySelector('#task-error');
  const editors = new Map();
  const textareas = [...root.querySelectorAll('[data-filename] textarea')];
  const labels = {received:'Результаты после дедлайна', active:'Выполняется', queued:'В очереди', running:'Проверяется', accepted:'Успешно выполнено', wrong_answer:'Не зачтено: неверный ответ', compile_error:'Ошибка компиляции', runtime_error:'Ошибка выполнения', limit_exceeded:'Превышение лимита', infra_error:'Техническая ошибка — попытка не расходуется'};
  const payload = () => root.dataset.kind === 'quiz' ? {answers:Object.fromEntries([...root.querySelectorAll('[data-question]')].map(q => [q.dataset.question, [...q.querySelectorAll('input:checked')].map(i => Number(i.value))]))} : {files:Object.fromEntries(textareas.map(t => [t.closest('[data-filename]').dataset.filename, t.value]))};
  function showError(message) { error.textContent = message; error.hidden = false; }
  function setEditable() {
    root.querySelectorAll('textarea.task-code, .quiz-question input').forEach(e => { e.disabled = !editable || closed; });
    editors.forEach(e => e.setReadOnly(!editable || closed));
    root.querySelectorAll('#task-save, #task-next, #task-send, #task-run, #terminal-form button').forEach(b => { b.disabled = !editable || closed || busy; });
  }
  function updateClock(data) {
    clock = data; syncAt = performance.now();
    closed = data.closed;
    document.querySelector('#task-status').textContent = labels[data.status] || data.status;
    setEditable();
  }
  async function request(url, options = {}) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {...options, signal:controller.signal, redirect:'error'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Сервер отклонил запрос.');
      return data;
    } catch (e) { throw new Error(e.name === 'AbortError' ? 'Сервер не ответил. Проверьте соединение; текст сохранён в браузере.' : e.message); }
    finally { clearTimeout(timeout); }
  }
  const post = data => request(base + 'action/', {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':csrf}, body:JSON.stringify(data)});
  function remember() {
    try { localStorage.setItem(key, JSON.stringify({revision, question:serverQuestion, ...payload()})); }
    catch (_) { status.textContent = 'Локальная копия недоступна. Проверьте сохранение на сервере.'; }
  }
  function changed() {
    if (!editable || closed) return;
    generation++; remember(); status.textContent = 'Есть несохранённые изменения';
    clearTimeout(debounce); debounce = setTimeout(() => save().catch(e => showError(e.message)), 600);
  }
  async function save() {
    if (!editable || closed) return;
    if (saving) { await saving; if (generation !== savedGeneration) return save(); return; }
    const sentGeneration = generation, body = payload();
    status.textContent = 'Сохраняется…';
    saving = (async () => {
      const data = await post({action:'save', revision, ...body});
      revision = data.revision; savedGeneration = sentGeneration; updateClock(data);
      status.textContent = generation === savedGeneration ? 'Сохранено на сервере' : 'Есть несохранённые изменения';
      if (generation === savedGeneration) { try { localStorage.removeItem(key); } catch (_) {} }
      else remember();
    })();
    try { await saving; } catch (e) { status.textContent = 'Не сохранено на сервере'; remember(); throw e; }
    finally { saving = null; }
  }
  textareas.forEach(t => t.addEventListener('input', changed));
  root.querySelectorAll('.quiz-question input').forEach(t => t.addEventListener('change', changed));
  function mountEditors() {
    if (!window.ContestEditor || editorMounted) return;
    editorMounted = true;
    textareas.forEach(t => { try { editors.set(t, window.ContestEditor.mount(t, {readOnly:!editable || closed, onChange:changed, onCursor:() => {}})); } catch (_) {} });
  }
  window.addEventListener('contest:editor-ready', mountEditors); mountEditors();
  root.querySelectorAll('[data-file-button]').forEach(b => b.addEventListener('click', () => {
    root.querySelectorAll('[data-filename]').forEach(f => { f.hidden = f.dataset.filename !== b.dataset.fileButton; });
    root.querySelectorAll('[data-file-button]').forEach(other => other.setAttribute('aria-pressed', String(other === b)));
    editors.forEach(editor => editor.layout());
  }));
  try {
    const local = JSON.parse(localStorage.getItem(key));
    if (local) {
      const recovery = document.querySelector('#local-recovery'); recovery.hidden = false;
      document.querySelector('#local-recovery-text').textContent = JSON.stringify(local.files || local.answers, null, 2);
      document.querySelector('#restore-local').disabled = !editable || closed || local.question !== serverQuestion;
      document.querySelector('#restore-local').addEventListener('click', () => {
        if (local.files) textareas.forEach(t => { const value = local.files[t.closest('[data-filename]').dataset.filename]; if (typeof value === 'string') { t.value = value; editors.get(t)?.setValue(value); } });
        if (local.answers) root.querySelectorAll('[data-question]').forEach(q => q.querySelectorAll('input').forEach(i => { i.checked = (local.answers[q.dataset.question] || []).includes(Number(i.value)); }));
        recovery.hidden = true; changed();
      });
      document.querySelector('#discard-local').addEventListener('click', () => { localStorage.removeItem(key); recovery.hidden = true; });
    }
  } catch (_) {}
  async function operation(callback) {
    if (busy || closed) return;
    busy = true; setEditable(); error.hidden = true;
    try { await save(); await callback(); }
    catch (e) { showError(e.message); }
    finally { busy = false; setEditable(); }
  }
  document.querySelector('#task-save')?.addEventListener('click', () => operation(async () => {}));
  for (const action of ['send', 'next']) document.querySelector(`#task-${action}`)?.addEventListener('click', () => operation(async () => {
    const data = await post({action}); updateClock(data); location.reload();
  }));
  async function displaySubmission(id) {
    const data = await request(`/submissions/${id}/state/`), s = data.submission;
    const historyStatus = root.querySelector(`[data-submission-status="${id}"]`);
    if (historyStatus) historyStatus.textContent = s.status_label;
    const output = document.querySelector('#task-output');
    output.textContent = `${s.status_label}\n${JSON.stringify(s.result, null, 2)}`;
    const download = document.querySelector('#task-log'); download.href = `/submissions/${id}/log/`; download.hidden = false;
    if (['queued','running'].includes(s.status)) setTimeout(() => displaySubmission(id).catch(e => showError(e.message)), 1200);
  }
  const run = command => operation(async () => {
    const data = await post({action:'run', command, stdin:document.querySelector('#task-stdin').value});
    updateClock(data); await displaySubmission(data.submission.id);
  });
  document.querySelector('#task-run')?.addEventListener('click', () => run('./main'));
  document.querySelector('#terminal-form')?.addEventListener('submit', e => { e.preventDefault(); run(document.querySelector('#terminal-command').value.trim()); });
  document.querySelector('#toggle-timer').addEventListener('click', () => {
    hideTimer = !hideTimer; document.querySelector('#task-timer').hidden = hideTimer;
    document.querySelector('#toggle-timer').textContent = hideTimer ? 'Показать таймер' : 'Скрыть таймер';
  });
  let timerExpired = false;
  setInterval(() => {
    const timer = document.querySelector('#task-timer');
    if (clock.closed) { timer.textContent = 'Попытка завершена'; return; }
    if (!clock.deadline) { timer.textContent = 'Без таймера'; return; }
    const remaining = Math.max(0, Math.ceil((Date.parse(clock.deadline) - (Date.parse(clock.server_now) + performance.now() - syncAt)) / 1000));
    timer.textContent = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`;
    if (!remaining && !clock.closed && !timerExpired) {
      timerExpired = true; closed = true; setEditable();
      status.textContent = 'Время истекло. Сервер отправляет последнюю сохранённую версию.';
      poll();
    }
  }, 250);
  async function poll() {
    if (polling || saving || busy) return;
    polling = true;
    try {
      const data = await request(base + 'state/');
      if (data.current_question !== serverQuestion || (data.closed && !clock.closed)) { location.reload(); return; }
      updateClock(data);
      if (!['active', 'queued', 'running'].includes(data.status)) clearInterval(pollInterval);
    } catch (e) { showError(e.message); }
    finally { polling = false; }
  }
  const pollInterval = setInterval(poll, 3000);
  function event(kind, detail = '') { if (editable && !closed) post({action:'event', kind, detail}).catch(() => {}); }
  window.addEventListener('blur', () => event('blur'));
  document.addEventListener('visibilitychange', () => { if (document.hidden) event('hidden'); });
  document.addEventListener('paste', e => event('paste', `Символов: ${(e.clipboardData?.getData('text') || '').length}`));
  window.addEventListener('beforeunload', e => { if (generation !== savedGeneration && !closed) { e.preventDefault(); e.returnValue = ''; } });
  document.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); operation(async () => {}); } });
  if (root.dataset.latestSubmission && root.dataset.kind !== "quiz") displaySubmission(root.dataset.latestSubmission).catch(e => showError(e.message));
  setEditable(); updateClock(clock);
})();
