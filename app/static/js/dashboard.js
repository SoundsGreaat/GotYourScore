/* Dashboard: hash-synced view + tracker-category state for the sidebar
   view engine below (#view-container swaps). Loaded once per full page
   load (deferred) — runs before htmx initializes so a restored hash
   retargets the eager load. */

/* Hash-synced view + category + reporting-period state.

   TWO orthogonal dimensions share the URL hash:

   - VIEW: where you are (sidebar) — qa-tracker, my-reviews, all-reviews,
     to-review, team-quotas.
   - CATEGORY: the global QA Score / Bad Feedback selector — a FILTER,
     not navigation. Clicking a tab re-fetches the CURRENT view in that
     category (To-review lists only that category's handoffs, My
     Reviews flips between QA reviews and Bad Feedback about you, the
     tracker flips between the matrix and the BF list). Refund Check
     will slot in as a third category once it exists.

   The hash mirrors all of it — "#view/category" plus "@YYYY-MM" when a
   non-current reporting period is selected — and is restored BEFORE
   htmx fires its initial load: reloads and shared links reopen the
   same view AND category AND period. Legacy hashes ("#qa-matrix",
   "#bad-feedback@2026-07") map onto the new scheme. Category-aware
   endpoints read ?cat=; view switches get it injected per-request, and
   category-flavored partials BAKE it into their self-refetch URLs
   (injection only covers view switches). Hashes matching no visible
   link (RBAC-hidden views) fall back to the server-chosen default. */
(function () {
    var container = document.getElementById('view-container');
    if (!container) return;

    var newReviewBtn = document.getElementById('new-review-btn');

    var CATEGORIES = ['qa-score', 'bad-feedback'];
    var links = Array.prototype.slice.call(
        document.querySelectorAll('aside a[data-view][hx-target="#view-container"]')
    );
    var tabs = Array.prototype.slice.call(
        document.querySelectorAll('.gys-tabs button[data-cat]')
    );
    if (!links.length && !tabs.length) return;

    // Old-format hashes (before the category dimension existed).
    var LEGACY_VIEWS = {
        'qa-matrix': 'qa-tracker',
        'bad-feedback': 'qa-tracker',
        'my-reviews': 'my-reviews',
        'all-reviews': 'all-reviews',
        'to-review': 'to-review',
        'team-quotas': 'team-quotas'
    };
    var LEGACY_BAD_FEEDBACK = { 'bad-feedback': true };

    var state = { view: null, category: 'qa-score', period: null };

    function baseHx(url) {
        return (url || '').split('?')[0];
    }
    function endpointFor(view, category) {
        var bf = category === 'bad-feedback';
        switch (view) {
            case 'qa-tracker':
                return bf ? '/partials/bad-feedback' : '/partials/qa-matrix';
            case 'my-reviews':
                return bf ? '/partials/my-reviews?cat=bad-feedback' : '/partials/my-reviews';
            case 'all-reviews':
                return bf ? '/partials/all-reviews?cat=bad-feedback' : '/partials/all-reviews';
            case 'to-review':
                return '/partials/to-review?cat=' + (bf ? 'bad-feedback' : 'qa-score');
            case 'team-quotas':
                return '/partials/team-quotas';
        }
        return null;
    }
    function withPeriod(url, period) {
        if (!period) return url;
        return url + (url.indexOf('?') === -1 ? '?' : '&')
            + 'period=' + encodeURIComponent(period);
    }

    /* "#view/category@period" — legacy names accepted. Invalid or
       RBAC-foreign categories collapse to qa-score (my-reviews is the
       one view every role can open in both categories). */
    function parseHash() {
        var raw = window.location.hash.replace('#', '');
        var period = null;
        var at = raw.indexOf('@');
        if (at !== -1) {
            period = raw.slice(at + 1) || null;
            raw = raw.slice(0, at);
        }
        var parts = raw.split('/');
        var view = parts[0];
        var category = parts[1];
        if (view && LEGACY_VIEWS[view]) {
            if (!category && LEGACY_BAD_FEEDBACK[view]) category = 'bad-feedback';
            view = LEGACY_VIEWS[view];
        }
        if (CATEGORIES.indexOf(category) === -1) category = 'qa-score';
        return { view: view, category: category, period: period };
    }

    function setActive(view, category) {
        links.forEach(function (link) {
            var linkView = link.getAttribute('data-view');
            var linkCat = link.getAttribute('data-cat');
            link.classList.toggle('menu-active',
                linkView === view && (!linkCat || linkCat === category));
        });
        tabs.forEach(function (btn) {
            var isTarget = btn.getAttribute('data-cat') === category;
            btn.classList.toggle('active', isTarget);
            btn.setAttribute('aria-selected', isTarget ? 'true' : 'false');
        });
        // The sliding underline is owned by js/tabs.js — nudge it, since
        // programmatic .active toggles never pass through its click
        // handler (reload-restore, sidebar-driven switches).
        Array.prototype.forEach.call(
            document.querySelectorAll('.gys-tabs[data-tabs]'),
            function (track) {
                track.dispatchEvent(new CustomEvent('gys-tabs-sync'));
            }
        );
    }

    // Which views does THIS user see? Sidebar links are the RBAC truth:
    // a hash naming a hidden view falls back to the server default.
    var knownViews = {};
    links.forEach(function (link) {
        knownViews[link.getAttribute('data-view')] = true;
    });
    function defaultView() {
        return baseHx(container.getAttribute('hx-get')) === '/partials/qa-matrix'
            ? 'qa-tracker' : 'my-reviews';
    }

    function loadView(view, period) {
        var url = endpointFor(view, state.category);
        if (!url) return;
        state.view = view;
        container.dataset.view = view;
        container.setAttribute('hx-get', endpointFor(view, state.category));
        htmx.ajax('GET', withPeriod(url, period), { source: container, target: container });
    }

    function setCategory(category) {
        if (CATEGORIES.indexOf(category) === -1) return;
        if (!state.view) return;
        if (category === state.category) { setActive(state.view, category); return; }
        state.category = category;
        loadView(state.view, state.period);
    }

    // Restore BEFORE htmx initializes so the load-trigger fetches the
    // hashed view/category/period instead of the server-side default.
    var wanted = parseHash();
    if (wanted.view && knownViews[wanted.view]) {
        state.view = wanted.view;
        state.category = wanted.category;
        state.period = wanted.period;
    } else {
        state.view = defaultView();
    }
    container.dataset.view = state.view;
    container.setAttribute(
        'hx-get',
        withPeriod(endpointFor(state.view, state.category), state.period));
    setActive(state.view, state.category);

    // The navbar "New Review" button is CATEGORY-AWARE: QA Score mounts
    // the review drawer (which self-opens on swap), Bad Feedback opens
    // the Bad Feedback creator modal (window.openBadFeedbackCreator in
    // common.js) — so the htmx wiring moved here from the markup.
    if (newReviewBtn) {
        newReviewBtn.addEventListener('click', function () {
            if (state.category === 'bad-feedback') {
                window.openBadFeedbackCreator();
                return;
            }
            htmx.ajax('GET', '/partials/review-drawer', {
                target: '#drawer-container',
                swap: 'innerHTML',
            });
        });
    }

    tabs.forEach(function (btn) {
        if (btn.disabled) return;
        btn.addEventListener('click', function () {
            setCategory(btn.getAttribute('data-cat'));
        });
    });

    // Record real view swaps while their original targets are still connected.
    // HTMX 4 emits after:swap even for no-swap responses, and an outerHTML
    // swap can replace the original section (and the event source).
    var viewSwaps = new WeakMap();
    document.addEventListener('htmx:before:swap', function (event) {
        var detail = event.detail;
        if (detail.tasks.some(function (task) {
            return task.type === 'main' && task.swapSpec.style !== 'none'
                && container.contains(task.target);
        })) {
            viewSwaps.set(detail.ctx, detail.ctx.sourceElement);
        }
    });
    document.addEventListener('htmx:after:swap', function (event) {
        var elt = viewSwaps.get(event.detail.ctx);
        if (!elt) return;

        var view = state.view;
        var category = state.category;
        if (elt === container) {
            view = container.dataset.view || state.view;
        } else if (elt.matches('aside a[data-view][hx-target="#view-container"]')) {
            view = elt.getAttribute('data-view');
            if (elt.getAttribute('data-cat')) category = elt.getAttribute('data-cat');
        }
        state.view = view;
        state.category = category;
        container.dataset.view = view;
        // Sticky selection: views WITHOUT a period picker (To-review,
        // the BF lists) don't reset it — switching there and back must
        // keep the chosen month.
        var periodNode = container.querySelector('[data-period]');
        if (periodNode) state.period = periodNode.getAttribute('data-period');
        history.replaceState(null, '', '#' + view + '/' + category
            + (state.period ? '@' + state.period : ''));
        setActive(view, category);
    });

    // Carry category and period into view switches unless the request
    // already provides them. Section self-refetches set their own filters.
    document.body.addEventListener('htmx:config:request', function (event) {
        var ctx = event.detail.ctx;
        var elt = ctx.sourceElement;
        var isViewSwitch =
            elt === container ||
            elt.matches('aside a[data-view][hx-target="#view-container"]');
        if (!isViewSwitch) return;
        var request = ctx.request;
        var query = new URL(request.action, document.baseURI).searchParams;
        if (!request.body.has('cat') && !query.has('cat')) {
            request.body.set('cat', elt.getAttribute('data-cat') || state.category);
        }
        if (state.period && !request.body.has('period') && !query.has('period')) {
            request.body.set('period', state.period);
        }
    });
})();
