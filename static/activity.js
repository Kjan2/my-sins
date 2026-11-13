(() => {
  const root = document.querySelector('[data-activity-attempt]');
  if (!root) return;
  const csrf = root.querySelector('[name=csrfmiddlewaretoken]').value;
  function record(kind) {
    fetch(`/attempts/${root.dataset.activityAttempt}/activity/`, {method:'POST',keepalive:true,
      headers:{'Content-Type':'application/json','X-CSRFToken':csrf}, body:JSON.stringify({kind})}).catch(() => {});
  }
  window.addEventListener('blur', () => record('blur'));
  document.addEventListener('visibilitychange', () => { if(document.hidden) record('hidden'); });
  document.addEventListener('paste', () => record('paste'));
})();
