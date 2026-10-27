(() => {
  "use strict";
  const root = document.querySelector("#judge-monitor");
  if (!root) return;
  const csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;
  let busy = false, signature = "";
  function render(state) {
    const values = {label: state.worker_label, message: state.message, current: state.current_submission || "—",
      heartbeat: state.heartbeat_at ? new Date(state.heartbeat_at).toLocaleString("ru-RU", {timeZone:"Europe/Moscow"}) + " МСК" : "—",
      queued: state.queued, running: state.running, oldest: state.oldest_seconds};
    for (const [key, value] of Object.entries(values)) document.querySelector(`#worker-${key}`).textContent = value;
    const warnings = document.querySelector("#queue-warnings");
    warnings.replaceChildren();
    for (const warning of state.warnings || []) {
      const text = document.createElement("p"); text.textContent = warning; warnings.append(text);
    }
    warnings.hidden = !(state.warnings || []).length;
    const nextSignature = JSON.stringify(state.errors);
    if (nextSignature === signature) return;
    signature = nextSignature;
    const errors = document.querySelector("#worker-errors"); errors.replaceChildren();
    for (const error of state.errors) {
      const row = document.createElement("div"); row.className = "monitor-error";
      const link = document.createElement("a"); link.href = `/submissions/${error.id}/`;
      link.textContent = `#${error.id} · ${error.student} · ${error.kind}`;
      const form = document.createElement("form"); form.method = "post"; form.action = `/teacher/submissions/${error.id}/requeue/`;
      const token = document.createElement("input"); token.type = "hidden"; token.name = "csrfmiddlewaretoken"; token.value = csrf;
      const button = document.createElement("button"); button.type = "submit"; button.textContent = "Повторить проверку";
      form.append(token, button); row.append(link, form); errors.append(row);
    }
    if (!state.errors.length) errors.textContent = "Ошибок нет.";
  }
  async function poll() {
    if (busy || document.hidden) return;
    busy = true;
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(root.dataset.stateUrl, {redirect:"error", cache:"no-store", signal:controller.signal});
      if (!response.ok) throw new Error();
      render(await response.json()); document.querySelector("#monitor-stale").hidden = true;
    } catch (_) { document.querySelector("#monitor-stale").hidden = false; }
    finally { clearTimeout(timer); busy = false; }
  }
  render(JSON.parse(document.querySelector("#judge-state").textContent));
  setInterval(poll, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
})();
