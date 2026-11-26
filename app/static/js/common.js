/* Shared front-end helpers for the HTMX partials.
 *
 * Loaded once from base.html; every partial script binds these to local
 * names instead of re-declaring its own copy.
 */

/* Resize from the current rendered height or a height captured immediately
 * before a DOM swap. Return to natural sizing when the motion ends, and
 * return the animation to callers that need to finalize a collapse. */
var gysResizeAnimations = new WeakMap();
window.gysResizePanel = function (panel, update, fromHeight) {
    var previous = gysResizeAnimations.get(panel);
    var canAnimate = panel.animate && matchMedia('(prefers-reduced-motion: no-preference)').matches;
    var start = canAnimate ? (fromHeight ?? panel.getBoundingClientRect().height) : 0;
    previous?.cancel();
    update?.();
    if (!canAnimate || !panel.isConnected) return;

    var end = panel.getBoundingClientRect().height;
    if (Math.abs(start - end) < 1) return;
    var css = getComputedStyle(panel);
    var speed = css.getPropertyValue('--gys-speed').trim();
    var animation = panel.animate([
        {height: start + 'px', overflow: 'clip'},
        {height: end + 'px', overflow: 'clip'},
    ], {
        duration: parseFloat(speed) * (speed.endsWith('ms') ? 1 : 1000),
        easing: css.getPropertyValue('--gys-ease').trim(),
    });
    gysResizeAnimations.set(panel, animation);
    animation.finished.then(function () {
        if (gysResizeAnimations.get(panel) === animation) gysResizeAnimations.delete(panel);
    }, function () { /* A newer update can interrupt this resize. */ });
    return animation;
};

/* Floating notifications: every transient note surfaces bottom-center
 * as translucent gradient glass (.gys-float-note in input.css). kind:
 * 'alert-success' for the mint look, anything else renders as
 * error-tinted. Pass trustedHtml only for caller-built markup.
 * timeout overrides 3.5s. */
window.gysFloatNote = function (content, kind, timeout, trustedHtml) {
    // Host choice matters: an open <dialog> lives in the TOP LAYER and
    // paints OVER anything appended to <body> (z-index is powerless
    // against it), so the stack must be created inside the open dialog
    // whenever one exists — that keeps notes visible above its glass.
    var host = document.querySelector('dialog[open]') || document.body;
    var stack = host.querySelector(':scope > .gys-float-stack');
    if (!stack) {
        stack = document.createElement('div');
        stack.className = 'gys-float-stack';
        host.appendChild(stack);
    }
    var note = document.createElement('div');
    note.setAttribute('role', 'alert');
    note.className = 'gys-float-note' + (kind === 'alert-success' ? '' : ' gys-error');
    if (trustedHtml) {
        note.innerHTML = content;
    } else {
        note.textContent = content;
    }
    stack.appendChild(note);
    window.setTimeout(function () {
        note.classList.add('gys-leaving');
        window.setTimeout(function () { note.remove(); }, 250);
    }, timeout || 3500);
};

/* Thin wrapper keeping the old call signature: message text plus a
 * daisyUI alert modifier ('alert-error' default, 'alert-success'). */
window.gysToast = function (message, kind) {
    window.gysFloatNote(message, kind);
};

function requestErrorText(status) {
    return status >= 500 ? 'Server error' : 'Request error';
}

// Only construct these with client-owned copy, never response or exception text.
class GysRequestError extends Error {
    constructor(status, message) {
        super(message || requestErrorText(status));
    }
}
window.gysRequestErrorMessage = function (error, fallback) {
    return error instanceof GysRequestError ? error.message : (fallback || 'Request error');
};
window.gysAiResponseError = function (response) {
    if (response.status !== 429) return new GysRequestError(response.status);
    var retryAfter = Number(response.headers.get('Retry-After'));
    return new GysRequestError(429, Number.isSafeInteger(retryAfter) && retryAfter > 0
        ? 'AI request limit reached. Try again in ' + retryAfter + ' seconds.'
        : 'AI request limit reached. Please try again shortly.');
};

// Compatibility with the existing admin 400 contract, limited to these
// endpoints and exact known validation messages. Never return arbitrary detail.
const gysCaseFieldErrors = new Set([
    'Field label cannot be blank.',
    'Field label must be at most 200 characters.',
    'Field type must be one of: date, text, textarea.',
    'Field position must be a whole number.',
    'Field position must be an integer from 0 to 100000.',
    'Tracker field was not found.',
    'The tracker has reached its active field limit.',
    'The built-in Date field must remain a date.',
    'Restore the archived field before changing it.',
    'This field cannot be changed to a date because it has non-text values.',
    'This field cannot be changed to a date while it contains values that are not valid YYYY-MM-DD dates. Clear or correct those values first.',
    'The field list is out of date. Refresh the tracker fields and try again.',
]);
const gysScorecardErrors = new Set([
    'Autofail is a separate review outcome, not a scorecard error.',
    'The Autofail rule key is reserved; remove the old rule.',
]);

function htmxErrorMessage(ctx) {
    var status = ctx.response?.status;
    if (status === 400 && ctx.text) {
        try {
            var url = new URL(ctx.request.action, document.baseURI);
            var detail = JSON.parse(ctx.text)?.detail;
            if (url.origin === window.location.origin && (
                (/^\/admin\/case-fields\/(?:bad-feedback|refund-check)\//.test(url.pathname)
                    && gysCaseFieldErrors.has(detail))
                || (/^\/admin\/scorecards\/\d+\/save$/.test(url.pathname) && gysScorecardErrors.has(detail))
            )) return detail;
        } catch { /* use the safe fallback */ }
    }
    return requestErrorText(status);
}

/* Error responses do not swap (see the HTMX config in base.html).
 * Show their messages as text; standalone fetch flows handle their own errors. */
document.addEventListener('htmx:response:error', function (evt) {
    window.gysToast(htmxErrorMessage(evt.detail.ctx), 'alert-error');
});
document.addEventListener('htmx:error', function (evt) {
    var ctx = evt.detail.ctx;
    if (!ctx) return;
    if (ctx.request.signal.reason === 'timeout') {
        window.gysToast('The request timed out.', 'alert-error');
    } else if (!ctx.response) {
        window.gysToast('Network error — the server could not be reached.', 'alert-error');
    } else {
        window.gysToast('Unable to update the page.', 'alert-error');
    }
});

window.escapeHtml = function (text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (ch) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
};

/* Fetches the review, mounts a fresh drawer partial into the dashboard's
 * drawer container, then flips it into edit/complete mode once the
 * drawer's script has registered its entry hook.
 *
 * Handshake: __reviewDrawerHookPending tells the fresh drawer to skip its
 * default rules load (the entry hook drives exactly ONE load for the
 * review's own type); the drawer epoch is claimed BEFORE the swap so
 * waitForHook ignores any previous mount's stale closure. */
window.openReviewDrawer = function (reviewId, mode) {
    fetch('/api/reviews/' + reviewId, { credentials: 'same-origin' })
        .then(function (response) {
            if (!response.ok) throw new Error('Review request failed');
            return response.json();
        })
        .then(function (review) {
            if (!window.htmx) return Promise.reject(new Error('htmx unavailable'));
            window.__reviewDrawerHookPending = true;
            window.__reviewDrawerEpoch = (window.__reviewDrawerEpoch || 0) + 1;
            var expectedEpoch = window.__reviewDrawerEpoch;
            return htmx.ajax('GET', '/partials/review-drawer', {
                target: '#drawer-container',
                swap: 'innerHTML settle:0',
            })
                .then(function () { return { review: review, expectedEpoch: expectedEpoch }; });
        })
        .then(function (payload) {
            var attempts = 0;
            (function waitForHook() {
                if (payload.expectedEpoch === window.__reviewDrawerReadyEpoch
                    && typeof window.openReviewDrawerForReview === 'function') {
                    window.openReviewDrawerForReview(payload.review, mode);
                    return;
                }
                // A newer open superseded this one — stop quietly instead
                // of toasting a failure for a drawer that IS open.
                if ((window.__reviewDrawerReadyEpoch || 0) > payload.expectedEpoch) return;
                if (++attempts > 40) { window.gysToast('Unable to open the review.'); return; }
                window.setTimeout(waitForHook, 50);
            })();
        })
        .catch(function () {
            // A dead hook must not leak into the next drawer mount.
            window.__reviewDrawerHookPending = false;
            window.gysToast('Unable to open the review.');
        });
};

/* Mounts the Bad Feedback editor partial and opens it. Works from any
 * view (the partial mounts into #bf-editor-container, which lives next
 * to the drawer container on the dashboard). Closing the dialog
 * unmounts the partial entirely. */
function mountTrackerEditor(tracker, query) {
    if (tracker !== 'bad-feedback' && tracker !== 'refund-check') return;
    var target = document.getElementById('bf-editor-container');
    if (!target || !window.htmx) {
        window.gysToast('Unable to open the editor.');
        return;
    }
    window.__trackerEditorEpoch = (window.__trackerEditorEpoch || 0) + 1;
    var epoch = window.__trackerEditorEpoch;
    htmx.ajax('GET', '/partials/' + tracker + '-editor' + query, {
        source: target,
        target: '#bf-editor-container',
        swap: 'innerHTML',
    }).then(function () {
        // A newer open superseded this one — let its mount win.
        if (epoch !== window.__trackerEditorEpoch) return;
        var modal = target.querySelector('.js-bf-edit-modal');
        if (modal && !modal.open) modal.showModal();
    }).catch(function () {
        if (epoch === window.__trackerEditorEpoch) window.gysToast('Unable to open the editor.');
    });
}

window.openTrackerEditor = function (tracker, recordId) {
    mountTrackerEditor(tracker, '?id=' + encodeURIComponent(recordId));
};
window.openBadFeedbackEditor = function (recordId) {
    window.openTrackerEditor('bad-feedback', recordId);
};

/* CREATE mode: same modal, no id — the partial renders the empty form
 * with a "Save For Later" (Pending) action. Used by the navbar New
 * Review button while the Bad Feedback category is active. */
window.openBadFeedbackCreator = function () {
    mountTrackerEditor('bad-feedback', '');
};
window.openTrackerCreator = function (tracker) {
    mountTrackerEditor(tracker, '');
};
