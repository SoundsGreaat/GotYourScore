/* "View notes" modal, shared by every review table (My/All Reviews,
   QA Matrix). Wired ONCE here via document-level delegation — the same
   pattern as dropdowns.js — so partials ship no per-view wiring and
   htmx swaps need no re-init.

   Markup contract (see macros/modals.html):
     <dialog class="modal js-notes-modal">          — one per section
       .js-notes-title / .js-notes-body
       .js-notes-actions > .js-notes-edit           — "Edit review",
         shown only when the clicked row is editable

   Trigger contract (row markup):
     <button data-case-notes="{id}"
             data-case-label="{case number | fallback}"
             data-case-type="{case type}"
             data-notes-open="{'edit'|'complete'|'' = hidden}">…</button>
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

        var editButton = modal.querySelector('.js-notes-edit');
        if (editButton) {
            // Availability rides on the TRIGGER (data-notes-open), not
            // on the row's action buttons: some tables render notes
            // triggers without an Actions column at all (QA-matrix
            // quota chips), which used to hide the button for reviews
            // that are perfectly editable elsewhere.
            var mode = button.getAttribute('data-notes-open') || '';
            if (mode === 'edit' || mode === 'complete') {
                editButton.dataset.reviewId = id;
                editButton.dataset.reviewMode = mode;
                editButton.classList.remove('hidden');
            } else {
                editButton.classList.add('hidden');
            }
        }
        modal.showModal();
    });

    document.addEventListener('click', function (event) {
        var button = event.target.closest ? event.target.closest('.js-notes-modal .js-notes-edit') : null;
        if (!button || !button.dataset.reviewId) return;
        var openModal = button.closest('.js-notes-modal[open]');
        if (openModal) openModal.close();
        if (typeof window.openReviewDrawer === 'function') {
            window.openReviewDrawer(button.dataset.reviewId, button.dataset.reviewMode || 'edit');
        }
    });
})();
