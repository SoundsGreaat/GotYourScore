(function () {
    var panel = document.getElementById('admin-view');
    if (!panel) return;

    function showInitialError(event) {
        var loading = panel.querySelector('[data-admin-loading]');
        if (loading && event.detail.ctx?.sourceElement === panel) {
            loading.textContent = 'Could not load this section. Select a section or reload the page.';
        }
    }
    document.addEventListener('htmx:response:error', showInitialError);
    document.addEventListener('htmx:error', showInitialError);

    function filterUsers(input) {
        var list = document.getElementById('users-list');
        var query = input.value.trim().toLowerCase();
        var visible = 0;
        var total = list.children.length;
        Array.prototype.forEach.call(list.children, function (row) {
            row.hidden = !row.dataset.userMatch.toLowerCase().includes(query);
            if (!row.hidden) visible += 1;
        });
        document.getElementById('users-empty').hidden = visible > 0;
        var count = document.getElementById('users-count');
        var message = visible === total
            ? total + (total === 1 ? ' account' : ' accounts')
            : visible + ' of ' + total + ' accounts';
        if (count.textContent !== message) count.textContent = message;
    }

    panel.addEventListener('input', function (event) {
        if (event.target.id === 'users-search') filterUsers(event.target);
    });

    function moveNeighbor(item, direction) {
        var sibling = direction === 'up' ? item.previousElementSibling : item.nextElementSibling;
        var kind = item.hasAttribute('data-row') ? '[data-row]' : '[data-group]';
        return sibling && sibling.matches(kind) ? sibling : null;
    }

    function syncMoveButtons() {
        panel.querySelectorAll('[data-move]').forEach(function (button) {
            var item = button.closest('[data-row], [data-group]');
            var canMove = !!moveNeighbor(item, button.dataset.move);
            button.setAttribute('aria-disabled', canMove ? 'false' : 'true');
            button.tabIndex = canMove ? 0 : -1;
        });
    }

    // Rules can be added, deleted, moved by the drag handle or swapped.
    new MutationObserver(syncMoveButtons).observe(panel, {childList: true, subtree: true});

    var reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    var moveAnimations = new WeakMap();

    function motionOptions(element) {
        var css = getComputedStyle(element);
        var speed = css.getPropertyValue('--gys-speed').trim();
        return {
            duration: parseFloat(speed) * (speed.endsWith('ms') ? 1 : 1000),
            easing: css.getPropertyValue('--gys-ease').trim(),
        };
    }

    // Let details open/close natively before animating the measured box.
    // Holding content-visibility open would leave closed controls tabbable.
    var disclosureStarts = new WeakMap();
    var disclosureAnimations = new WeakMap();
    panel.addEventListener('click', function (event) {
        var details = event.target.closest('.gys-admin-disclosure > summary')?.parentElement;
        if (details && !reducedMotion.matches && typeof details.animate === 'function') {
            disclosureStarts.set(details, details.getBoundingClientRect().height);
        }
    });
    panel.addEventListener('toggle', function (event) {
        var details = event.target;
        if (!disclosureStarts.has(details)) return;
        var start = disclosureStarts.get(details);
        disclosureStarts.delete(details);
        disclosureAnimations.get(details)?.cancel();
        if (reducedMotion.matches) return;
        var end = details.getBoundingClientRect().height;
        if (start === end) return;
        disclosureAnimations.set(details, details.animate([
            {height: start + 'px', overflow: 'clip'},
            {height: end + 'px', overflow: 'clip'}
        ], motionOptions(details)));
    }, true);

    function animateReorder(startPositions) {
        startPositions.forEach(function (start) {
            var item = start.item;
            var delta = start.top - (item.getBoundingClientRect().top + window.scrollY);
            if (!delta) return;
            moveAnimations.set(item, item.animate([
                {translate: '0 ' + delta + 'px'},
                {translate: '0 0'}
            ], motionOptions(item)));
        });
    }

    var moveAnnouncement;
    panel.addEventListener('click', function (event) {
        var button = event.target.closest('[data-move]');
        if (!button) return;
        var direction = button.dataset.move;
        var item = button.closest('[data-row], [data-group]');
        var neighbor = moveNeighbor(item, direction);
        if (!neighbor) return;
        var list = item.parentElement;
        var reference = direction === 'up' ? neighbor : neighbor.nextSibling;
        var startPositions = [];
        if (!reducedMotion.matches && typeof item.animate === 'function') {
            startPositions = [item, neighbor].map(function (entry) {
                var top = entry.getBoundingClientRect().top + window.scrollY;
                moveAnimations.get(entry)?.cancel();
                return {item: entry, top: top};
            });
        }
        if (typeof list.moveBefore === 'function') list.moveBefore(item, reference);
        else list.insertBefore(item, reference);
        item.scrollIntoView({block: 'nearest', inline: 'nearest'});
        button.focus({preventScroll: true});
        animateReorder(startPositions);

        var isRule = item.hasAttribute('data-row');
        var label = isRule ? item.querySelector('[data-display]').value.trim() : item.querySelector('h4').textContent;
        var position = Array.prototype.indexOf.call(list.children, item) + 1;
        var status = document.getElementById('scorecard-move-status');
        clearTimeout(moveAnnouncement);
        status.textContent = '';
        // Re-inserting a node can re-announce focus, so delay the update.
        moveAnnouncement = setTimeout(function () {
            if (status.isConnected) status.textContent = (label || 'Rule') + ' moved ' + direction + ' to position ' + position + '.';
        }, 500);
    });

    // A successful account mutation replaces the directory. Retain the
    // filter and restore keyboard focus only if it was in the old panel.
    var userSwaps = new WeakMap();
    var editorSwaps = new WeakMap();
    document.addEventListener('htmx:before:swap', function (event) {
        var detail = event.detail;
        var source = detail.ctx.sourceElement;
        if (!source) return;

        function swaps(target) {
            return detail.tasks.some(function (task) {
                return task.type === 'main' && task.target === target && task.swapSpec.style !== 'none';
            });
        }
        if (source.matches('[data-template-open]') && swaps(document.getElementById('scorecard-editor'))) {
            editorSwaps.set(detail.ctx, source);
        }
        if (!source.closest('#users-root') || !swaps(panel)) return;
        var input = document.getElementById('users-search');
        var details = document.activeElement.closest('details');
        userSwaps.set(detail.ctx, {
            query: input ? input.value : '',
            focus: panel.contains(document.activeElement),
            summary: details ? details.querySelector('summary').id : '',
        });
    });
    document.addEventListener('htmx:after:swap', function (event) {
        var ctx = event.detail.ctx;
        var selected = editorSwaps.get(ctx);
        if (selected && selected.isConnected) {
            editorSwaps.delete(ctx);
            panel.querySelectorAll('[data-template-open]').forEach(function (button) {
                var current = button === selected;
                button.closest('[data-template-card]').classList.toggle('card-selected', current);
                if (current) button.setAttribute('aria-current', 'true');
                else button.removeAttribute('aria-current');
            });
        }
        var state = userSwaps.get(ctx);
        if (!state) return;
        userSwaps.delete(ctx);
        var input = document.getElementById('users-search');
        if (input && state.query) {
            input.value = state.query;
            filterUsers(input);
        }
        if (state.focus) {
            var summary = document.getElementById(state.summary);
            var target = summary && !summary.closest('[hidden]') ? summary : (input || panel);
            target.focus({preventScroll: true});
        }
    });
})();
