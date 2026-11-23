/* Shared single-choice popovers. Delegation also covers HTMX swaps,
 * cloned batch rows, and dynamically populated options. */
(function () {
    'use strict';

    var TYPEAHEAD_RESET_MS = 800;
    var SEARCH_THRESHOLD = 10;
    var activePicker = null;
    var anchorSupport = CSS.supports('position-area', 'block-end span-inline-end')
        && CSS.supports('position-try-fallbacks', 'flip-block, flip-inline');
    var visibility = new IntersectionObserver(function (entries) {
        if (!activePicker || !activePicker.matches(':popover-open')) return;
        var trigger = triggerFor(activePicker);
        if (entries.some(function (entry) { return entry.target === trigger && !entry.isIntersecting; })) {
            activePicker.hidePopover();
        }
    });

    function optionButtons(picker) {
        return Array.from(picker.querySelectorAll('.gys-picker-options > li > button[data-opt-value]'));
    }

    function visibleOptions(picker) {
        return optionButtons(picker).filter(function (button) {
            return !button.parentElement.hidden && !button.disabled;
        });
    }

    function pickerFor(trigger) {
        var picker = trigger.nextElementSibling;
        return picker && picker.matches('.gys-picker[popover]')
            && picker.id === trigger.getAttribute('popovertarget') ? picker : null;
    }

    function triggerFor(picker) {
        var trigger = picker.previousElementSibling;
        return trigger && trigger.getAttribute('popovertarget') === picker.id ? trigger : null;
    }

    function searchFor(picker) {
        return picker.querySelector('.gys-picker-search:not([hidden]) input');
    }

    function bestOption(options) {
        return options.find(function (btn) { return btn.getAttribute('aria-checked') === 'true'; }) || options[0];
    }

    function syncPicker(picker, trigger) {
        var options = optionButtons(picker);
        var value = picker.nextElementSibling.value;
        var selected = options.find(function (button) { return button.dataset.optValue === value; });
        options.forEach(function (button) {
            button.setAttribute('aria-checked', String(button === selected));
        });
        var searchable = options.length >= SEARCH_THRESHOLD;
        picker.querySelector('.gys-picker-search').hidden = !searchable;
        picker.setAttribute('role', searchable ? 'dialog' : 'menu');
        picker.querySelector('.gys-picker-options').setAttribute('role', searchable ? 'menu' : 'group');
        trigger.setAttribute('aria-haspopup', searchable ? 'dialog' : 'menu');
    }

    // Dynamic consumers can sync after replacing options, before the trigger receives focus.
    window.gysSyncDropdown = function (idOrTrigger) {
        var trigger = typeof idOrTrigger === 'string'
            ? document.getElementById(idOrTrigger + '-button')
            : idOrTrigger;
        var picker = trigger && pickerFor(trigger);
        if (picker) syncPicker(picker, trigger);
    };

    // Keep the popup at least as wide as the trigger, unless the viewport is narrower.
    // CSS handles the normal anchored placement; measure only to correct overflow
    // or to position it on browsers without anchor positioning.
    function positionPicker(picker, trigger) {
        var gap = 4;
        var edge = 8;
        var viewport = window.visualViewport;
        var layout = document.documentElement.getBoundingClientRect();
        var viewportLeft = viewport ? viewport.offsetLeft : 0;
        var x = Math.max(layout.left, viewportLeft);
        var y = viewport ? viewport.offsetTop : 0;
        var width = Math.min(layout.right, viewportLeft + (viewport ? viewport.width : window.innerWidth)) - x;
        var height = viewport ? viewport.height : window.innerHeight;
        var anchor = trigger.getBoundingClientRect();
        if (anchor.right <= x || anchor.left >= x + width || anchor.bottom <= y || anchor.top >= y + height) {
            picker.hidePopover();
            return;
        }
        picker.style.setProperty('--gys-picker-trigger-width', anchor.width + 'px');
        picker.style.setProperty('--gys-picker-viewport-width', width + 'px');
        picker.style.setProperty('--gys-picker-viewport-height', height + 'px');
        picker.style.removeProperty('--gys-picker-available');
        picker.classList.remove('gys-picker-positioned');
        var box = picker.getBoundingClientRect();
        if (anchorSupport && box.left >= x + edge && box.right <= x + width - edge
                && box.top >= y + edge && box.bottom <= y + height - edge
                && (box.top >= anchor.bottom || box.bottom <= anchor.top)) return;

        var below = y + height - edge - anchor.bottom - gap;
        var above = anchor.top - y - edge - gap;
        var openBelow = box.height <= below || below >= above;
        var available = openBelow ? below : above;
        // On very short viewports a centered popup is more usable than a clipped search input.
        var usable = available >= Math.min(96, box.height);
        var maxHeight = usable ? available : height - 2 * edge;
        var renderedHeight = Math.min(box.height, maxHeight);
        var top = usable
            ? (openBelow ? anchor.bottom + gap : anchor.top - gap - renderedHeight)
            : y + (height - renderedHeight) / 2;
        var start = getComputedStyle(trigger).direction === 'rtl' ? anchor.right - box.width : anchor.left;
        picker.style.setProperty('--gys-picker-left', Math.max(x + edge, Math.min(start, x + width - box.width - edge)) + 'px');
        picker.style.setProperty('--gys-picker-top', Math.max(y + edge, Math.min(top, y + height - edge - renderedHeight)) + 'px');
        picker.style.setProperty('--gys-picker-available', maxHeight + 'px');
        picker.classList.add('gys-picker-positioned');
    }

    function updatePosition() {
        if (!activePicker) return;
        if (!activePicker.matches(':popover-open')) {
            activePicker = null;
            return;
        }
        var trigger = triggerFor(activePicker);
        if (trigger) positionPicker(activePicker, trigger);
    }

    function filterOptions(picker, search) {
        var query = search ? search.value.trim().toLocaleLowerCase() : '';
        var count = 0;
        optionButtons(picker).forEach(function (button) {
            var matches = button.dataset.optLabel.toLocaleLowerCase().includes(query);
            button.parentElement.hidden = !matches;
            if (matches) count++;
        });
        picker.querySelector('.gys-picker-status').textContent = query && !count ? 'No matching options.' : '';
        picker.querySelector('.gys-picker-options').scrollTop = 0;
        if (picker === activePicker) updatePosition();
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
        var buttons = visibleOptions(menu);
        if (!buttons.length) return;

        var currentIndex = buttons.indexOf(document.activeElement);
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
        if (match) match.focus();
    }

    // The open menu a keystroke belongs to: either the target sits inside
    // an open menu, or on the trigger button that owns one.
    function openMenuFor(event) {
        var t = event.target;
        if (!t || !t.closest) return null;
        var menu = t.closest('.gys-picker[popover]');
        if (menu && menu.matches(':popover-open')) return menu;
        var trigger = t.closest('button[popovertarget]');
        if (!trigger) return null;
        var owned = pickerFor(trigger);
        return owned && owned.matches(':popover-open') ? owned : null;
    }

    document.addEventListener('keydown', function (event) {
        if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;

        var menu = openMenuFor(event);
        if (!menu) {
            // ArrowUp/Down on a CLOSED dropdown's trigger opens it.
            if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
            var t = event.target;
            var trigger = t && t.closest
                ? t.closest('button[popovertarget]')
                : null;
            if (!trigger) return;
            var closed = pickerFor(trigger);
            if (!closed || !closed.showPopover) return;
            event.preventDefault();
            closed.showPopover();
            var opts = visibleOptions(closed);
            var selected = event.key === 'ArrowUp' ? opts[opts.length - 1] : (searchFor(closed) || bestOption(opts));
            if (selected) selected.focus();
            return;
        }

        var search = searchFor(menu);
        var fromSearch = event.target === search;
        if (fromSearch && !['ArrowDown', 'ArrowUp', 'Enter', 'Escape', 'Tab'].includes(event.key)) return;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            var buttons = visibleOptions(menu);
            if (!buttons.length) return;
            var idx = buttons.indexOf(document.activeElement);
            var step = event.key === 'ArrowDown' ? 1 : -1;
            if (search && idx === 0 && step === -1) {
                search.focus();
                return;
            }
            var next = buttons[
                ((idx === -1 ? (step === 1 ? -1 : 0) : idx) + step + buttons.length)
                % buttons.length
            ];
            next.focus();
        } else if (event.key === 'Home' || event.key === 'End') {
            event.preventDefault();
            var edgeOptions = visibleOptions(menu);
            var edge = event.key === 'Home' ? edgeOptions[0] : edgeOptions[edgeOptions.length - 1];
            if (edge) edge.focus();
        } else if (event.key === 'Escape') {
            // Explicitly restore focus as popover light-dismiss does not
            // consistently do so across browsers.
            event.preventDefault();
            var owner = triggerFor(menu);
            if (menu.hidePopover) menu.hidePopover();
            if (owner) owner.focus();
        } else if (event.key === 'Tab') {
            if (fromSearch && !event.shiftKey) {
                var result = bestOption(visibleOptions(menu));
                if (result) {
                    event.preventDefault();
                    result.focus();
                    return;
                }
            } else if (search && event.shiftKey && event.target.matches('button[data-opt-value]')) {
                event.preventDefault();
                search.focus();
                return;
            }
            // Continue the browser's tab sequence from the invoker.
            var trigger = triggerFor(menu);
            menu.hidePopover();
            if (trigger) trigger.focus();
        } else if (fromSearch && event.key === 'Enter') {
            event.preventDefault(); // Never submit the enclosing form from a picker search.
            var match = bestOption(visibleOptions(menu));
            if (match) match.click();
        } else if (event.key === 'Backspace') {
            var st = menu.__ddTypeahead;
            if (st && st.q) {
                st.q = st.q.slice(0, -1);
                st.t = Date.now();
                if (st.q) {
                    var m = findMatch(optionButtons(menu), st.q, -1);
                    if (m) m.focus();
                }
                event.preventDefault();
            }
        } else if (event.key.length === 1 && event.key !== ' ') {
            event.preventDefault();
            if (search) {
                search.focus();
                search.value += event.key;
                filterOptions(menu, search);
            } else {
                typeahead(menu, event.key);
            }
        }
    });

    function onSearch(event) {
        if (!event.target.matches('.gys-picker-search input')) return;
        // Don't let HTMX filter forms refetch as the user types or blurs the search.
        event.stopPropagation();
        if (event.type !== 'change') filterOptions(event.target.closest('.gys-picker'), event.target);
    }
    ['input', 'change', 'search'].forEach(function (type) {
        document.addEventListener(type, onSearch, true);
    });
    document.addEventListener('click', function (event) {
        var trigger = event.target.closest('button[popovertarget]');
        var picker = trigger && pickerFor(trigger);
        if (picker) picker.__ddKeyboardOpen = event.detail === 0;
    }, true);
    document.addEventListener('beforetoggle', function (event) {
        var picker = event.target;
        if (event.newState !== 'open' || !picker.matches('.gys-picker[popover]')) return;
        var trigger = triggerFor(picker);
        if (!trigger) return;
        picker.style.setProperty('--gys-picker-font-size', getComputedStyle(trigger).fontSize);
        picker.style.removeProperty('--gys-picker-open-width');
        syncPicker(picker, trigger);
    }, true);

    // Keep the menu button's expanded state accurate for assistive tech,
    // including native activation and light-dismiss/outside clicks.
    document.addEventListener('toggle', function (event) {
        var menu = event.target;
        if (!menu.matches || !menu.matches('.gys-picker[popover]')) return;
        var trigger = triggerFor(menu);
        if (!trigger) return;
        var open = menu.matches(':popover-open');
        trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
        menu.__ddTypeahead = null;
        if (open) {
            activePicker = menu;
            positionPicker(menu, trigger);
            if (!menu.matches(':popover-open')) return;
            visibility.disconnect();
            visibility.observe(trigger);
            menu.style.setProperty('--gys-picker-open-width', menu.getBoundingClientRect().width + 'px');
            if (!menu.contains(document.activeElement)) {
                var entry = (menu.__ddKeyboardOpen && searchFor(menu)) || bestOption(visibleOptions(menu));
                if (entry) entry.focus();
            }
        } else {
            if (activePicker === menu) {
                activePicker = null;
                visibility.disconnect();
            }
            var search = searchFor(menu);
            if (search) search.value = '';
            filterOptions(menu, search);
        }
        menu.__ddKeyboardOpen = false;
    }, true);
    window.addEventListener('resize', updatePosition);
    window.addEventListener('scroll', function (event) {
        if (activePicker && (!(event.target instanceof Node) || !activePicker.contains(event.target))) updatePosition();
    }, {capture: true, passive: true});
    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', updatePosition);
        window.visualViewport.addEventListener('scroll', updatePosition);
    }
})();

/* Shared pick handler for every popover-API dropdown rendered by
 * app/templates/macros/dropdowns.html (the macro's default js_fn).
 * Per-template wrappers may call this and then add their own side
 * effects (see review_drawer / qa_matrix).
 */
window.setDropdownValue = function (hiddenId, displayRef, value, text, btnEl) {
    // New-style macro calls pass only (hiddenId, displayRef, btnEl):
    // the picked value/label live in the option button's dataset (see
    // the macro's js_fn doc). Programmatic callers may still pass all
    // five args explicitly. Vararg-forwarding WRAPPERS declare the old
    // 5-param signature, so a shifted call lands the ELEMENT in the
    // value slot — detect it there before trusting `value`.
    if (value instanceof Element) {
        btnEl = value;
        value = undefined;
    }
    if (text instanceof Element) text = undefined;
    if (btnEl && value === undefined) {
        value = btnEl.dataset.optValue || '';
        text = btnEl.dataset.optLabel || '';
    }
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
    // spans survive htmx's settle step, see review_drawer). Null means
    // "no visible label" (programmatic/hidden-only callers): skip the
    // display update instead of crashing on indexOf.
    var display = displayRef
        ? document.getElementById(displayRef)
          || (displayRef.indexOf('#') === 0 || displayRef.indexOf('.') === 0
              ? document.querySelector(displayRef) : null)
        : null;
    if (display) {
        display.textContent = text;
        display.classList.remove('text-base-content/70');
    }
    window.gysSyncDropdown(hiddenId);
    // Popover-API menus stay open after an inner click — close on pick.
    var pop = btnEl && btnEl.closest('[popover]');
    if (pop && pop.matches(':popover-open')) {
        pop.hidePopover();
        // Programmatic picks in a closed menu must not steal focus.
        var trigger = document.querySelector('[popovertarget="' + pop.id + '"]');
        if (trigger) trigger.focus();
    }
};
