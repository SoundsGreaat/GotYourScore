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

    var activityRevision = 0;
    var activityStates = new Map();
    var scorecardLoads = new WeakMap();
    document.addEventListener('htmx:config:request', function (event) {
        var ctx = event.detail.ctx;
        var toggle = ctx.sourceElement;
        if (toggle.matches('[data-template-toggle]')) {
            // Capture the desired state before queuing; a sibling may
            // already have changed the persisted state by the time it runs.
            ctx.request.body.set('active', String(toggle.checked));
            toggle.classList.toggle('gys-scorecard-toggle-on', toggle.checked);
        } else if (ctx.request.method === 'GET') {
            var path = new URL(ctx.request.action, document.baseURI).pathname;
            if (path === '/admin/partials/scorecards' || path.startsWith('/admin/partials/scorecard-editor/')) {
                scorecardLoads.set(ctx, activityRevision);
            }
        }
    });

    function syncScorecardActivity(states) {
        panel.querySelectorAll('[data-template-status]').forEach(function (badge) {
            var active = states[badge.dataset.templateStatus];
            if (typeof active !== 'boolean') return;
            var label = active ? 'Active' : 'Inactive';
            if (badge.textContent !== label) badge.textContent = label;
            badge.classList.toggle('badge-success', active);
            badge.classList.toggle('badge-soft', active);
            badge.classList.toggle('badge-ghost', !active);
        });
        panel.querySelectorAll('[data-template-toggle]').forEach(function (toggle) {
            var active = states[toggle.closest('[data-template-card]').dataset.templateId];
            if (typeof active !== 'boolean') return;
            toggle.defaultChecked = active;
            toggle.checked = active;
            toggle.classList.toggle('gys-scorecard-toggle-on', active);
            toggle.closest('label').title = active ? 'Deactivate template'
                : 'Activate template (demotes other active templates of this case type)';
        });
    }

    function recordScorecardActivity(states) {
        var revision = ++activityRevision;
        Object.keys(states).forEach(function (id) {
            activityStates.set(id, {active: states[id], revision: revision});
        });
        syncScorecardActivity(states);
    }

    panel.addEventListener('scorecard-activity', function (event) {
        recordScorecardActivity(event.detail.states);
    });

    var scorecardCreates = new WeakMap();
    panel.addEventListener('htmx:before:swap', function (event) {
        var ctx = event.detail.ctx;
        var source = ctx.sourceElement;
        if (!source.matches('[data-template-create]')) return;
        var task = event.detail.tasks.find(function (entry) {
            return entry.type === 'main' && entry.target?.id === 'template-list' && entry.swapSpec.style === 'outerMorph';
        });
        var created = task?.fragment.querySelector('[data-template-open][aria-current="true"]');
        if (!created) return;

        var editor = panel.querySelector('#scorecard-editor');
        // Do not override an open editor or a template the user is already opening.
        var openCreated = editor.querySelector('[data-scorecard-editor-empty]') && !editor.classList.contains('htmx-request');
        var selected = openCreated ? created : task.target.querySelector('[data-template-open][aria-current="true"]');
        var selection = selected?.closest('[data-template-card]').dataset.templateId;
        var states = {};
        task.fragment.querySelectorAll('[data-template-card]').forEach(function (card) {
            var id = card.dataset.templateId;
            var button = card.querySelector('[data-template-open]');
            card.classList.toggle('card-selected', id === selection);
            if (id === selection) button.setAttribute('aria-current', 'true');
            else button.removeAttribute('aria-current');
            states[id] = card.querySelector('[data-template-toggle]').hasAttribute('checked');
        });
        scorecardCreates.set(ctx, {
            states: states,
            openCard: openCreated ? created.closest('[data-template-card]').id : null,
        });
    });

    panel.addEventListener('htmx:after:swap', function (event) {
        var ctx = event.detail.ctx;
        var state = scorecardCreates.get(ctx);
        if (!state) return;
        scorecardCreates.delete(ctx);
        recordScorecardActivity(state.states);

        var source = ctx.sourceElement;
        var submitted = ctx.request.body;
        if (source.elements.name.value === submitted.get('name')
            && source.elements.case_type.value === submitted.get('case_type')) {
            source.reset();
            var initial = source.querySelector('#new-case-picker [data-opt-value]');
            window.setDropdownValue('new-case', '#new-case-display', initial.dataset.optValue, initial.dataset.optLabel);
        }
        if (state.openCard) document.getElementById(state.openCard)?.querySelector('[data-template-open]').click();
        window.gysToast('Template created.', 'alert-success');
    });

    panel.addEventListener('htmx:finally:request', function (event) {
        var ctx = event.detail.ctx;
        var toggle = ctx.sourceElement;
        if (!toggle?.matches('[data-template-toggle]') || ctx.response?.status === 204) return;
        toggle.checked = toggle.defaultChecked;
        toggle.classList.toggle('gys-scorecard-toggle-on', toggle.checked);
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

    var sortables = new WeakMap();
    window.gysAdminSortable = function (list, kind, items, onEnd) {
        if (!window.Sortable || !list) return;
        sortables.get(list)?.destroy();
        var motion = motionOptions(list);
        var sortable = new Sortable(list, {
            handle: '.scorecard-grip-' + kind,
            draggable: items,
            animation: reducedMotion.matches ? 0 : motion.duration,
            easing: motion.easing,
            ghostClass: 'scorecard-ghost',
            chosenClass: 'scorecard-chosen',
            dragClass: 'scorecard-drag',
            onChoose: function () {
                sortable.option('animation', reducedMotion.matches ? 0 : motionOptions(list).duration);
            },
            onEnd: onEnd,
        });
        sortables.set(list, sortable);
        return sortable;
    };
    // A parent swap can remove a Sortable host that has no HTMX attributes.
    // Don't destroy hosts that are still connected after a DOM move.
    new MutationObserver(function (changes) {
        var hosts = '#editor-groups, [data-rows], [data-case-field-list]';
        changes.forEach(function (change) {
            change.removedNodes.forEach(function (node) {
                if (!(node instanceof Element) || node.isConnected) return;
                var removed = node.matches(hosts) ? [node] : [];
                removed.push(...node.querySelectorAll(hosts));
                removed.forEach(function (list) {
                    if (list.isConnected) return;
                    sortables.get(list)?.destroy();
                    sortables.delete(list);
                });
            });
        });
        syncMoveButtons();
    }).observe(panel, {childList: true, subtree: true});

    // Let details open/close natively before animating the measured box.
    // Holding content-visibility open would leave closed controls tabbable.
    var disclosureStarts = new WeakMap();
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
        window.gysResizePanel(details, null, start);
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

    function capturePositions(items) {
        if (reducedMotion.matches || typeof Element.prototype.animate !== 'function') return [];
        return Array.from(items).filter(function (item) { return item.getClientRects().length; }).map(function (item) {
            var top = item.getBoundingClientRect().top + window.scrollY;
            moveAnimations.get(item)?.cancel();
            return {item: item, top: top};
        });
    }

    var moveAnnouncements = new WeakMap();
    function announceMove(item, direction) {
        var list = item.parentElement;
        var status = item.closest('[data-case-field-tracker], #editor-root')
            ?.querySelector('[data-admin-move-status]');
        if (!status) return;
        var label = item.querySelector('[data-case-field-name]')?.textContent
            || item.querySelector('[data-display]')?.value.trim()
            || item.querySelector('h4')?.textContent || 'Rule';
        var position = Array.prototype.indexOf.call(list.children, item) + 1;
        clearTimeout(moveAnnouncements.get(status));
        status.textContent = '';
        // Reinserting a focused node can trigger a separate focus announcement.
        moveAnnouncements.set(status, setTimeout(function () {
            if (status.isConnected) status.textContent = label + ' moved ' + (direction ? direction + ' ' : '')
                + 'to position ' + position + '.';
        }, 500));
    }

    function moveItem(list, item, reference) {
        if (typeof list.moveBefore === 'function') list.moveBefore(item, reference);
        else list.insertBefore(item, reference);
    }

    panel.addEventListener('click', function (event) {
        var button = event.target.closest('[data-move]');
        if (!button) return;
        var direction = button.dataset.move;
        var item = button.closest('[data-row], [data-group]');
        var neighbor = moveNeighbor(item, direction);
        if (!neighbor) return;
        var list = item.parentElement;
        if (caseListBusy(list)) return;
        var originalOrder = [...list.children];
        var reference = direction === 'up' ? neighbor : neighbor.nextSibling;
        var startPositions = capturePositions([item, neighbor]);
        moveItem(list, item, reference);
        item.scrollIntoView({block: 'nearest', inline: 'nearest'});
        button.focus({preventScroll: true});
        animateReorder(startPositions);
        if (list.matches('[data-case-field-list]')) persistFieldOrder(list, item, originalOrder, direction);
        else announceMove(item, direction);
    });

    var fieldRequests = new WeakSet();
    var ordering = new WeakSet();
    var dragOrders = new WeakMap();
    var pendingFieldWrites = new Set();
    function beginFieldWrite() {
        var finish;
        var pending = new Promise(function (resolve) { finish = resolve; });
        pendingFieldWrites.add(pending);
        return function () {
            pendingFieldWrites.delete(pending);
            finish();
        };
    }

    document.addEventListener('htmx:before:request', function (event) {
        var ctx = event.detail.ctx;
        if (!pendingFieldWrites.size || ctx.request.method !== 'GET'
            || new URL(ctx.request.action, document.baseURI).pathname !== '/admin/partials/case-fields') return;
        var send = ctx.fetch;
        ctx.fetch = async function (url, options) {
            // Returning to the tab while a local write is in flight must
            // not fetch a snapshot from before that write.
            while (pendingFieldWrites.size) {
                await Promise.all(pendingFieldWrites);
                options.signal?.throwIfAborted?.();
            }
            return send(url, options);
        };
    });
    function caseListBusy(list) {
        return ordering.has(list) || fieldRequests.has(list.closest('[data-case-field-tracker]'));
    }

    function setFieldBusy(list, busy) {
        if (!list) return;
        var sortable = sortables.get(list);
        if (sortable) sortable.option('disabled', busy);
        if (busy) list.setAttribute('aria-busy', 'true');
        else list.removeAttribute('aria-busy');
    }

    async function persistFieldOrder(list, item, originalOrder, direction) {
        ordering.add(list);
        var finish = beginFieldWrite();
        setFieldBusy(list, true);
        var status;
        function readStatus(event) { status = event.detail.ctx.response?.status; }
        list.addEventListener('htmx:finally:request', readStatus, {once: true});
        function rollback() {
            if (!list.isConnected) return;
            var focus = document.activeElement;
            var startPositions = capturePositions(list.children);
            originalOrder.forEach(function (row) { moveItem(list, row, null); });
            animateReorder(startPositions);
            if (list.contains(focus)) focus.focus({preventScroll: true});
        }
        try {
            await htmx.ajax('POST', list.dataset.reorderUrl, {
                source: list,
                target: list,
                swap: 'none',
                values: {
                    field_ids: JSON.stringify(Array.from(list.children, function (row) {
                        return Number(row.dataset.caseFieldId);
                    })),
                },
            });
            if (status !== 204) {
                // The global HTMX error handler already shows the server's message.
                rollback();
            } else if (list.isConnected) {
                announceMove(item, direction);
                window.gysToast('Field order saved.', 'alert-success');
            }
        } catch {
            rollback();
            if (list.isConnected) window.gysToast('Could not save the field order.');
        } finally {
            list.removeEventListener('htmx:finally:request', readStatus);
            ordering.delete(list);
            setFieldBusy(list, false);
            finish();
        }
    }

    function initCaseFieldLists(scope) {
        scope.querySelectorAll('[data-case-field-list]').forEach(function (list) {
            if (sortables.has(list)) return;
            var sortable = window.gysAdminSortable(list, 'rule', '[data-case-field-id]', function (event) {
                var originalOrder = dragOrders.get(list);
                dragOrders.delete(list);
                if (originalOrder && event.oldIndex !== event.newIndex) {
                    persistFieldOrder(list, event.item, originalOrder);
                }
            });
            sortable?.option('onStart', function () {
                dragOrders.set(list, [...list.children].filter(function (row) {
                    return row !== Sortable.ghost && row !== Sortable.clone;
                }));
            });
        });
    }
    document.addEventListener('htmx:after:process', function (event) {
        if (event.target === panel || panel.contains(event.target)) initCaseFieldLists(event.target);
    });

    var fieldSwaps = new WeakMap();
    var fieldRequestScopes = new WeakMap();
    function fieldFormValues(form) {
        return new URLSearchParams(new FormData(form)).toString();
    }

    panel.addEventListener('htmx:before:request', function (event) {
        var ctx = event.detail.ctx;
        var source = ctx.sourceElement;
        var scope = source.closest('[data-case-field-tracker]');
        if (!source.matches('[data-case-field-action]') || !scope) return;
        var list = scope.querySelector('[data-case-field-list]');
        if (caseListBusy(list) || dragOrders.has(list)) {
            event.preventDefault();
            return;
        }
        fieldRequestScopes.set(ctx, {
            scope: scope, finish: beginFieldWrite(),
            submitted: source.dataset.caseFieldAction === 'create' ? fieldFormValues(source) : null,
        });
        fieldRequests.add(scope);
        setFieldBusy(list, true);
    });
    // HTMX dispatches directly on document if archive/restore removed the request form.
    document.addEventListener('htmx:finally:request', function (event) {
        var ctx = event.detail.ctx;
        var request = fieldRequestScopes.get(ctx);
        if (!request) return;
        fieldRequestScopes.delete(ctx);
        request.finish();
        var scope = request.scope;
        fieldRequests.delete(scope);
        setFieldBusy(scope.querySelector('[data-case-field-list]'), false);
        fieldSwaps.get(ctx)?.untouched.forEach(function (form) { form.removeAttribute('hx-morph-skip'); });
    });

    panel.addEventListener('htmx:before:swap', function (event) {
        var ctx = event.detail.ctx;
        var source = ctx.sourceElement;
        var action = source.dataset.caseFieldAction;
        var scope = source.closest('[data-case-field-tracker]');
        if (!action || !scope) return;
        var task = event.detail.tasks.find(function (entry) {
            return entry.type === 'main' && entry.target === scope && entry.swapSpec.style === 'outerMorph';
        });
        if (!task) return;

        // Keep disclosures and untouched forms as they are, including unsaved inputs and pickers.
        keepDisclosures(scope, task.fragment);
        var resetCreate = action === 'create' && fieldRequestScopes.get(ctx)?.submitted === fieldFormValues(source);
        var untouched = [...scope.querySelectorAll('form[data-case-field-action="update"], form[data-case-field-action="create"]')]
            .filter(function (form) { return form !== source || (action === 'create' && !resetCreate); });
        untouched.forEach(function (form) { form.setAttribute('hx-morph-skip', ''); });
        var active = document.activeElement;
        var row = active.closest('[data-case-field-row]');
        var addSummary = scope.querySelector('[data-case-default-focus]');
        var nextFocus = row?.nextElementSibling?.querySelector('[data-case-row-focus]')
            || row?.previousElementSibling?.querySelector('[data-case-row-focus]')
            || (action === 'restore' ? scope.querySelector('[id$="-archive-summary"]') : addSummary);
        var content = scope.querySelector('[data-case-field-content]');
        fieldSwaps.set(ctx, {
            scope: scope, source: source, action: action, untouched: untouched, resetCreate: resetCreate,
            positions: capturePositions(scope.querySelectorAll('[data-case-field-row]')),
            height: action !== 'update' && !reducedMotion.matches ? content.getBoundingClientRect().height : null,
            active: active, nextFocus: nextFocus,
        });
    });

    panel.addEventListener('htmx:after:swap', function (event) {
        var state = fieldSwaps.get(event.detail.ctx);
        if (!state) return;
        fieldSwaps.delete(event.detail.ctx);
        state.untouched.forEach(function (form) { form.removeAttribute('hx-morph-skip'); });
        var scope = state.scope;
        if (!scope.isConnected) return;
        if (state.resetCreate) {
            state.source.reset();
            var initial = state.source.querySelector('[data-opt-value="text"]');
            var pickerId = initial.closest('[popover]').id.replace(/-picker$/, '');
            window.setDropdownValue(pickerId, '#' + pickerId + '-display', initial);
        }
        if (!state.active.isConnected) state.nextFocus?.focus({preventScroll: true});
        if (state.height !== null) window.gysResizePanel(scope.querySelector('[data-case-field-content]'), null, state.height);
        animateReorder(state.positions.filter(function (entry) { return entry.item.isConnected; }));
        var rowId = scope.dataset.caseFieldChanged;
        var row = rowId ? document.getElementById(scope.id + '-' + rowId
            + (state.action === 'archive' ? '-archived' : '-row')) : null;
        if (state.action !== 'update' && row?.getClientRects().length && !reducedMotion.matches && row.animate) {
            row.animate([{opacity: 0, translate: '0 -0.25rem'}, {opacity: 1, translate: '0 0'}], motionOptions(row));
        }
        if (scope.dataset.caseFieldSaved) window.gysToast(scope.dataset.caseFieldSaved, 'alert-success');
    });

    function keepDisclosures(scope, fragment) {
        scope.querySelectorAll('details[id]').forEach(function (details) {
            fragment.getElementById(details.id)?.toggleAttribute('open', details.open);
        });
    }

    // A morph keeps stable nodes, but the incoming fragment still needs
    // their local disclosure/resize state and any unsaved form values.
    var adminDraftRequests = new WeakMap();
    var adminStateSwaps = new WeakMap();
    function formValues(form) {
        return new URLSearchParams(new FormData(form)).toString();
    }
    panel.addEventListener('htmx:before:request', function (event) {
        var ctx = event.detail.ctx;
        var form = ctx.sourceElement;
        if (!form?.matches('form') || !form.closest('[data-admin-state-scope]')) return;
        var submitted = ctx.request.body;
        adminDraftRequests.set(ctx, {
            form: new URLSearchParams(submitted).toString(),
            fields: new Map(Array.from(form.querySelectorAll('[data-admin-keep-draft][id]'), function (field) {
                return [field.id, submitted.get(field.name)];
            })),
        });
    });
    panel.addEventListener('htmx:before:swap', function (event) {
        var ctx = event.detail.ctx;
        var source = ctx.sourceElement;
        var scope = source?.closest('[data-admin-state-scope]');
        if (!scope) return;
        var task = event.detail.tasks.find(function (entry) {
            return entry.type === 'main' && entry.target === scope && entry.swapSpec.style === 'outerMorph';
        });
        if (!task) return;
        var request = adminDraftRequests.get(ctx);
        keepDisclosures(scope, task.fragment);

        var draftFields = [];
        scope.querySelectorAll('textarea[id], input[data-admin-keep-draft][id]').forEach(function (field) {
            var next = task.fragment.getElementById(field.id);
            if (!next) return;
            // A manual browser resize writes an inline size on the field.
            if (field.matches('textarea')) {
                next.style.height = field.style.height;
                if (field.style.width) next.style.width = field.style.width;
                if (!next.style.length) next.removeAttribute('style');
            }
            var baseline = request?.fields.get(field.id) ?? field.defaultValue;
            var value = field.hasAttribute('data-admin-keep-draft') &&
                !source.hasAttribute('data-admin-discard-draft') && field.value !== baseline
                ? field.value : next.defaultValue;
            draftFields.push([field.id, value]);
        });

        var formDrafts = [];
        scope.querySelectorAll('form[data-admin-keep-form][id]').forEach(function (form) {
            var next = task.fragment.getElementById(form.id);
            if (!next) return;
            var submitted = form === source && request ? new URLSearchParams(request.form) : null;
            var values = Array.from(form.elements).flatMap(function (field) {
                if (!(field instanceof HTMLInputElement) || !field.name || field.disabled) return [];
                if (field.type === 'checkbox') {
                    var baseline = submitted ? submitted.getAll(field.name).includes(field.value) : field.defaultChecked;
                    return field.checked === baseline ? [] : [{name: field.name, value: field.value, checked: field.checked}];
                }
                var original = submitted ? submitted.get(field.name) : field.defaultValue;
                return field.value === original ? [] : [{name: field.name, value: field.value}];
            });
            // Apply only unsaved local edits on top of fresh server defaults.
            // Keeping a whole stale role form could re-grant a revoked role.
            formDrafts.push({id: form.id, values: values});
        });
        var nextFocus = source.closest('details')?.querySelector('summary')
            || scope.querySelector('[data-admin-state-focus]');
        var resetForm = source.hasAttribute('data-admin-discard-draft') ? source.closest('form') : null;
        adminStateSwaps.set(ctx, {
            scope: scope, formDrafts: formDrafts, draftFields: draftFields, active: document.activeElement,
            nextFocus: nextFocus, resetForm: resetForm,
        });
    });
    panel.addEventListener('htmx:after:swap', function (event) {
        var ctx = event.detail.ctx;
        var state = adminStateSwaps.get(ctx);
        if (!state) return;
        adminStateSwaps.delete(ctx);
        if (!state.scope.isConnected) return;
        if (state.resetForm?.isConnected) state.resetForm.reset();
        state.formDrafts.forEach(function (draft) {
            var form = document.getElementById(draft.id);
            if (!form) return;
            form.reset();
            draft.values.forEach(function (entry) {
                var field = Array.from(form.elements).find(function (input) {
                    return !input.disabled && input.name === entry.name &&
                        (entry.checked === undefined || input.value === entry.value);
                });
                if (field && entry.checked !== undefined) field.checked = entry.checked;
                else if (field) field.value = entry.value;
            });
        });
        state.draftFields.forEach(function (entry) {
            var field = document.getElementById(entry[0]);
            if (!field) return;
            if (field.matches('[data-admin-draft-picker]')) {
                var picker = document.getElementById(field.id + '-picker');
                var option = picker?.querySelector('[data-opt-value="' + CSS.escape(entry[1]) + '"]');
                if (option) window.setDropdownValue(field.id, '#' + field.id + '-display', entry[1], option.dataset.optLabel);
            } else if (field.value !== entry[1]) {
                field.value = entry[1];
            }
        });
        if (!state.active?.isConnected && state.nextFocus?.isConnected) {
            state.nextFocus.focus({preventScroll: true});
        }
        var saved = state.scope.dataset.adminStateSaved;
        if (saved) {
            delete state.scope.dataset.adminStateSaved;
            window.gysToast(saved, 'alert-success');
        }
    });

    // Retain the directory filter and restore keyboard focus after edits.
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
        var users = source.closest('#users-root');
        if (!users || !swaps(users)) return;
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
        var started = scorecardLoads.get(ctx);
        if (started !== undefined && started < activityRevision) {
            // An editor/list response can have been prepared before a
            // toggle completed. Keep newer status on the replacement.
            var states = {};
            activityStates.forEach(function (state, id) {
                if (state.revision > started) states[id] = state.active;
            });
            syncScorecardActivity(states);
        }
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
