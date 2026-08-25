/* "View notes" modal, shared by every review table (My/All Reviews,
   QA Matrix). Wired ONCE here via document-level delegation — the same
   pattern as dropdowns.js — so partials ship no per-view wiring and
   htmx swaps need no re-init.

   Markup contract (see macros/modals.html):
     <dialog class="modal js-notes-modal">          — one per section
       .js-notes-title / .js-notes-body

   Trigger contract (row markup):
     <button data-case-notes="{id}"
             data-case-label="{case number | fallback}"
             data-case-type="{case type}">…</button>
     <template data-notes-for="{id}">{raw notes html}</template>

   The title reads "Case {label} · {case type}"; the body is sanitized
   with DOMPurify (vendored, loaded in base.html). */
(function () {
    document.addEventListener('click', function (event) {
        var button = event.target.closest ? event.target.closest('[data-case-notes]') : null;
        if (!button) return;
        var section = button.closest('section');
        if (!section || !section.contains(button)) return;
        var modal = section.querySelector('.js-notes-modal');
        if (!modal) return;
        var titleElement = modal.querySelector('.js-notes-title');
        var bodyElement = modal.querySelector('.js-notes-body');
        if (!titleElement || !bodyElement) return;

        var id = button.getAttribute('data-case-notes');
        var template = section.querySelector('template[data-notes-for="' + id + '"]');
        // content.textContent keeps the ORIGINAL html string (innerHTML
        // would double-escape it into visible "&lt;strong&gt;" text).
        var raw = template ? template.content.textContent : '';
        var label = button.getAttribute('data-case-label') || id;
        var caseType = button.getAttribute('data-case-type');
        titleElement.textContent = 'Case ' + label + (caseType ? ' \u00B7 ' + caseType : '');
        if (!raw.trim()) {
            bodyElement.innerHTML = '<p class="text-base-content/60 italic">No notes for this case.</p>';
        } else if (window.DOMPurify) {
            bodyElement.innerHTML = window.DOMPurify.sanitize(raw);
        } else {
            bodyElement.textContent = raw;
        }
        modal.showModal();
    });
})();
