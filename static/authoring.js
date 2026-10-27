(() => {
  "use strict";
  const form = document.querySelector("[data-authoring-form]");
  if (!form) return;
  let dirty = form.dataset.hasErrors === "true";
  const status = form.querySelector("[data-authoring-status]");
  function changed() {
    dirty = true;
    if (status) status.textContent = "Есть несохранённые изменения.";
  }
  function deletion(row) {
    row.dataset.deleted = String(!!row.querySelector('[name$="-DELETE"]')?.checked);
  }
  form.addEventListener("input", changed);
  form.addEventListener("change", event => {
    changed();
    const row = event.target.closest("[data-formset-row]");
    if (row) deletion(row);
  });
  form.querySelectorAll("[data-formset]").forEach(section => {
    const total = section.querySelector('[name$="-TOTAL_FORMS"]');
    const rows = section.querySelector("[data-formset-rows]");
    section.querySelectorAll("[data-formset-row]").forEach(deletion);
    section.querySelector("[data-add-form]").addEventListener("click", () => {
      const index = Number(total.value);
      if (index >= Number(section.dataset.formLimit)) {
        if (status) status.textContent = "Достигнут предел строк. Сохраните изменения, прежде чем добавлять новые.";
        return;
      }
      const template = document.createElement("template");
      template.innerHTML = section.querySelector("[data-empty-form]").innerHTML.replaceAll("__prefix__", String(index));
      const row = template.content.firstElementChild;
      rows.append(row); total.value = index + 1;
      deletion(row); changed();
      row.querySelector('select, textarea, input:not([type="hidden"]):not([type="checkbox"])')?.focus();
    });
  });
  form.addEventListener("submit", event => {
    if (!event.submitter?.hasAttribute("data-preview")) dirty = false;
  });
  window.addEventListener("beforeunload", event => {
    if (dirty) { event.preventDefault(); event.returnValue = ""; }
  });
})();
