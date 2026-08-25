/* Keyboard-friendly dropdowns — project-wide, zero per-template wiring,
 * zero injected UI (native <select>-style typeahead: you just type and
 * the matching option gets highlighted).
 *
 * Works on every popover-API dropdown menu (`[popover].dropdown.menu`)
 * via pure event delegation on `document`, so HTMX-swapped partials and
 * cloned batch rows are covered automatically, including menus that do
 * not exist yet when this script runs. The page's CSS/DOM is never
 * touched — nothing is mutated during the daisyUI entry animation.
 *
 * Behavior (with a menu open):
 *  - Typing printable characters highlights the first option whose
 *    label starts with the accumulated buffer (case-insensitive).
 *    Repeating one character cycles through its matches; Backspace
 *    edits the buffer; the buffer resets after a short idle pause.
 *  - ArrowDown/ArrowUp move the active option, Enter picks it (the
 *    option's existing onclick runs — the shared setDropdownValue
 *    setter closes the popover itself).
 *  - ArrowDown/ArrowUp on a closed dropdown's trigger opens it.
 */
(function () {
    'use strict';

    var TYPEAHEAD_RESET_MS = 800;

    function optionButtons(menu) {
        return Array.prototype.slice.call(
            menu.querySelectorAll(':scope > li > button'));
    }

    function setActive(menu, btn) {
        optionButtons(menu).forEach(function (b) {
            b.classList.remove('active');
            b.classList.remove('menu-active');
        });
        if (btn) {
            btn.classList.add('active');
            btn.classList.add('menu-active');
            // Keep the highlighted option in view inside max-h-48 lists.
            if (btn.scrollIntoView) btn.scrollIntoView({ block: 'nearest' });
        }
    }

    function activeButton(menu) {
        var current = null;
        optionButtons(menu).forEach(function (b) {
            if (b.classList.contains('menu-active')) current = b;
        });
        return current;
    }

    // First option whose label starts with `query`, searched cyclically
    // from the option right after `startAfter` (-1 = from the top).
    function findMatch(buttons, query, startAfter) {
        var q = query.toLowerCase();
        for (var i = 0; i < buttons.length; i++) {
            var idx = (startAfter + 1 + i) % buttons.length;
            var label = buttons[idx].textContent.trim().toLowerCase();
            if (label.indexOf(q) === 0) return buttons[idx];
        }
        return null;
    }

    function typeahead(menu, key) {
        var now = Date.now();
        var state = menu.__ddTypeahead || { q: '', t: 0 };
        var fresh = now - state.t > TYPEAHEAD_RESET_MS;
        var buttons = optionButtons(menu);
        if (!buttons.length) return;

        var currentIndex = buttons.indexOf(activeButton(menu));
        var query;
        var match;
        if (!fresh && state.q.length === 1 && state.q === key) {
            // Same character again: cycle through its matches.
            match = findMatch(buttons, key, currentIndex);
            query = key;
        } else {
            query = (fresh ? '' : state.q) + key;
            match = findMatch(buttons, query, -1);
            if (!match && query.length > 1) {
                // No prefix hit for the whole buffer — fall back to the
                // newest character alone, continuing from the current
                // highlight (native <select> behaves the same way).
                query = key;
                match = findMatch(buttons, query, currentIndex);
            }
        }
        menu.__ddTypeahead = { q: query, t: now };
        if (match) setActive(menu, match);
    }

    // The open menu a keystroke belongs to: either the target sits inside
    // an open menu, or on the trigger button that owns one.
    function openMenuFor(event) {
        var t = event.target;
        if (!t || !t.closest) return null;
        var menu = t.closest('[popover].dropdown.menu');
        if (menu && menu.matches(':popover-open')) return menu;
        var trigger = t.closest('button[popovertarget]');
        if (!trigger) return null;
        var owned = document.getElementById(trigger.getAttribute('popovertarget'));
        return owned && owned.matches(':popover-open') ? owned : null;
    }

    document.addEventListener('keydown', function (event) {
        if (event.ctrlKey || event.metaKey || event.altKey) return;

        var menu = openMenuFor(event);
        if (!menu) {
            // ArrowUp/Down on a CLOSED dropdown's trigger opens it.
            if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
            var t = event.target;
            var trigger = t && t.closest
                ? t.closest('button[popovertarget]')
                : null;
            if (!trigger) return;
            var closed = document.getElementById(trigger.getAttribute('popovertarget'));
            if (!closed || !closed.showPopover || closed.matches(':popover-open')) return;
            event.preventDefault();
            closed.showPopover();
            return;
        }

        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            var buttons = optionButtons(menu);
            if (!buttons.length) return;
            var idx = buttons.indexOf(activeButton(menu));
            var step = event.key === 'ArrowDown' ? 1 : -1;
            var next = buttons[
                ((idx === -1 ? (step === 1 ? -1 : 0) : idx) + step + buttons.length)
                % buttons.length
            ];
            setActive(menu, next);
        } else if (event.key === 'Enter') {
            event.preventDefault();
            var pick = activeButton(menu) || optionButtons(menu)[0];
            // Runs the option's existing onclick handler; the shared
            // setDropdownValue setter hides the popover after applying.
            if (pick) pick.click();
        } else if (event.key === 'Backspace') {
            var st = menu.__ddTypeahead;
            if (st && st.q) {
                st.q = st.q.slice(0, -1);
                st.t = Date.now();
                if (st.q) {
                    var m = findMatch(optionButtons(menu), st.q, -1);
                    if (m) setActive(menu, m);
                }
                event.preventDefault();
            }
        } else if (event.key.length === 1) {
            event.preventDefault(); // e.g. stop Space from scrolling
            typeahead(menu, event.key);
        }
        // Escape: native light dismiss closes the popover.
    });
})();

/* Shared pick handler for every popover-API dropdown rendered by
 * app/templates/macros/dropdowns.html (the macro's default js_fn).
 * Per-template wrappers may call this and then add their own side
 * effects (see review_drawer / qa_matrix).
 */
window.setDropdownValue = function (hiddenId, displayRef, value, text, btnEl) {
    var hidden = document.getElementById(hiddenId);
    if (hidden) {
        hidden.value = value;
        // Programmatic picks never fire native change events; HTMX filter
        // forms (my_reviews) and the qa-matrix batch rows rely on one to
        // refetch / grow rows after every pick.
        hidden.dispatchEvent(new Event('change', { bubbles: true }));
    }
    // displayRef may be an id ("x-display"), an id selector
    // ("#x-display") or a class selector (".js-x-display" — id-less
    // spans survive htmx's settle step, see review_drawer).
    var display = document.getElementById(displayRef)
        || (displayRef.indexOf('#') === 0 || displayRef.indexOf('.') === 0
            ? document.querySelector(displayRef) : null);
    if (display) {
        display.textContent = text;
        display.classList.remove('text-base-content/70');
    }
    if (btnEl) {
        // btnEl is normally the clicked menu OPTION (inside the popover
        // <ul>), but programmatic callers may pass any trigger — the
        // option-deselect step only applies when a list is present.
        var list = btnEl.closest('ul');
        if (list) {
            list.querySelectorAll('button').forEach(function (btn) {
                btn.classList.remove('active');
                btn.classList.remove('menu-active');
            });
            btnEl.classList.add('active');
            btnEl.classList.add('menu-active');
        }
    }
    // Popover-API menus stay open after an inner click — close on pick.
    var pop = btnEl && btnEl.closest('[popover]');
    if (pop && pop.matches(':popover-open')) pop.hidePopover();
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
};
