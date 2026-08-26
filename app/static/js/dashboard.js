/* Dashboard: hash-synced view + reporting-period state for the sidebar
   view engine below (#view-container swaps). Loaded once per full page
   load (deferred) — runs before htmx initializes so a restored hash
   retargets the eager load. */

/* Hash-synced view + reporting-period state. Every sidebar link swaps
   a partial into #view-container while the URL stays "/", so a reload
   always landed on the default view. The active partial is mirrored
   into the location hash — as "#view", or "#view@YYYY-MM" when a
   non-current reporting period is selected — and restored BEFORE
   htmx fires its initial load: reloads (and direct /#my-reviews@2026-07-style
   links) reopen the same view AND period. View switches carry the
   selected period via a configRequest parameter injection, so
   navigating never drops it. Hashes that match no visible link
   (RBAC-hidden views) fall back to the server-chosen default. */
(function () {
    var container = document.getElementById('view-container');
    if (!container) return;
    var links = Array.prototype.slice.call(
        document.querySelectorAll('aside a[hx-target="#view-container"]')
    );
    if (!links.length) return;

    var state = { period: null };

    function baseHx(url) {
        return (url || '').split('?')[0];
    }
    function partialOf(source) {
        var url = typeof source === 'string'
            ? source
            : ((source && (source.getAttribute('hx-get') || source.getAttribute('hx-post'))) || '');
        return baseHx(url).split('/').pop();
    }
    function withPeriod(url, period) {
        return period ? baseHx(url) + '?period=' + encodeURIComponent(period) : baseHx(url);
    }
    function parseHash() {
        var raw = window.location.hash.replace('#', '');
        var at = raw.indexOf('@');
        return at === -1
            ? { view: raw, period: null }
            : { view: raw.slice(0, at), period: raw.slice(at + 1) || null };
    }
    function setActive(partial) {
        links.forEach(function (link) {
            link.classList.toggle('menu-active', partialOf(link) === partial);
        });
    }
    function linkFor(partial) {
        for (var i = 0; i < links.length; i++) {
            if (partialOf(links[i]) === partial) return links[i];
        }
        return null;
    }

    // Restore BEFORE htmx initializes so the load-trigger fetches the
    // hashed view (and period) instead of the server-side default.
    var wanted = parseHash();
    var target = wanted.view ? linkFor(wanted.view) : null;
    if (target) {
        state.period = wanted.period;
        container.setAttribute(
            'hx-get', withPeriod(target.getAttribute('hx-get'), wanted.period));
    }
    var current = target ? wanted.view : partialOf(container);
    setActive(current);

    // Mirror every successful swap into the hash + sidebar highlight.
    // afterSwap fires ONLY on real swaps, so 403s (forbidden views)
    // never touch the hash. Gotcha: for outerHTML swaps detail.target
    // is the DETACHED old node (closest('#view-container') is null),
    // so relevance is judged by the requesting element (which may be
    // the fresh, attached section), its hx-target, or an explicit
    // container target. The view name comes from the request path and
    // the period from the fresh partial's data-period attribute.
    document.body.addEventListener('htmx:afterSwap', function (event) {
        var detail = event.detail || {};
        var elt = detail.elt;
        var swapped = detail.target;
        var touchesView =
            swapped === container ||
            !!(swapped && swapped.closest && swapped.closest('#view-container')) ||
            !!elt && (
                elt === container ||
                (elt.getAttribute && elt.getAttribute('hx-target') === '#view-container') ||
                !!(elt.closest && elt.closest('#view-container'))
            );
        if (!touchesView) return;
        var source = (detail.requestConfig && detail.requestConfig.path) || elt;
        var partial = partialOf(source);
        if (!partial) return;
        // Sticky selection: views WITHOUT a period picker (e.g.
        // To-review) don't reset it — switching there and back must
        // keep the chosen month.
        var periodNode = container.querySelector('[data-period]');
        if (periodNode) state.period = periodNode.getAttribute('data-period');
        history.replaceState(
            null, '', '#' + partial + (state.period ? '@' + state.period : ''));
        setActive(partial);
    });

    // Carry the selected period into view switches. Gotcha: htmx strips
    // a query string from hx-get when the requester is an <a> (anchor
    // path resolution), so the period is injected as a REQUEST PARAMETER
    // in configRequest (serialized into the query string for GETs).
    // Section refetches keep their own period via the baked ?period=
    // URL and are skipped here.
    document.body.addEventListener('htmx:configRequest', function (event) {
        if (!state.period) return;
        var detail = event.detail;
        var elt = detail.elt;
        var isViewSwitch =
            elt === container ||
            !!(elt && elt.getAttribute &&
               elt.getAttribute('hx-target') === '#view-container');
        if (!isViewSwitch) return;
        if (detail.parameters && detail.parameters.period === undefined &&
            (detail.path || '').indexOf('period=') === -1) {
            detail.parameters.period = state.period;
        }
    });

    // Tracker tabs are declarative now (hx-get + hx-target on each
    // button, visuals in js/tabs.js); their hx-target makes the
    // configRequest listener above carry the selected period.
})();
