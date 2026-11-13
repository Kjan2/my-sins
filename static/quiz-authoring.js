(() => {
  const source = document.querySelector('#id_quiz_questions');
  if (!source) return;
  let questions;
  try { questions = JSON.parse(source.value || '[]'); if (!Array.isArray(questions)) return; } catch (_) { return; }
  const host = document.createElement('section'); host.className = 'quiz-authoring';
  source.parentNode.insertBefore(host, source);
  const raw = document.createElement('details'), summary = document.createElement('summary');
  summary.textContent = 'Исходные данные вопросов (JSON)'; raw.append(summary); source.replaceWith(raw); raw.append(source);
  const add = document.createElement('button'); add.type = 'button'; add.textContent = '+ Добавить вопрос теста'; host.after(add);
  function node(tag, text) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; return e; }
  function serialize() {
    const result = [...host.children].map(row => ({
      text:row.querySelector('[data-question-text]').value,
      options:[...row.querySelectorAll('[data-option-text]')].map(i => i.value),
      correct:[...row.querySelectorAll('[data-correct]')].flatMap((i, index) => i.checked ? [index] : []),
      ...(row.querySelector('[data-question-seconds]').value ? {seconds:Number(row.querySelector('[data-question-seconds]').value)} : {}),
    }));
    source.value = JSON.stringify(result); source.dispatchEvent(new Event('input', {bubbles:true}));
  }
  function appendQuestion(question = {text:'',options:['',''],correct:[0]}) {
    const row = node('fieldset'); row.append(node('legend', `Вопрос ${host.children.length + 1}`));
    const label = node('label','Условие (Markdown и формулы)');
    const text = node('textarea'); text.dataset.questionText = ''; text.rows = 3; text.value = question.text; label.append(text); row.append(label);
    const timerLabel = node('label','Время вопроса, секунд (при запрете возврата; необязательно)');
    const timer = node('input'); timer.type = 'number'; timer.min = '1'; timer.dataset.questionSeconds = ''; timer.value = question.seconds || ''; timerLabel.append(timer); row.append(timerLabel);
    const options = node('div'); row.append(options);
    function appendOption(value = '', correct = false) {
      const option = node('div'); option.className = 'form-columns';
      const title = node('input'); title.type = 'text'; title.value = value; title.dataset.optionText = ''; title.setAttribute('aria-label','Вариант ответа');
      const correctLabel = node('label','Правильный'); const check = node('input'); check.type = 'checkbox'; check.checked = correct; check.dataset.correct = ''; correctLabel.prepend(check);
      const remove = node('button','Удалить ответ'); remove.type = 'button'; remove.addEventListener('click', () => { option.remove(); serialize(); });
      option.append(title, correctLabel, remove); options.append(option);
    }
    question.options.forEach((value, index) => appendOption(value, question.correct.includes(index)));
    const addOption = node('button','+ Ответ'); addOption.type = 'button'; addOption.addEventListener('click', () => { appendOption(); serialize(); });
    const removeQuestion = node('button','Удалить вопрос'); removeQuestion.type = 'button'; removeQuestion.addEventListener('click', () => { row.remove(); serialize(); });
    row.append(addOption, removeQuestion); host.append(row);
    row.addEventListener('input', serialize); row.addEventListener('change', serialize);
  }
  questions.forEach(appendQuestion);
  add.addEventListener('click', () => { appendQuestion(); serialize(); });
  raw.addEventListener('toggle', () => { if (!raw.open) { try { const q = JSON.parse(source.value || '[]'); if (Array.isArray(q)) { host.replaceChildren(); q.forEach(appendQuestion); } } catch (_) {} } });
})();
