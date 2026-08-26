/* Shared front-end helpers for the HTMX partials.
 *
 * Loaded once from base.html; every partial script binds these to local
 * names instead of re-declaring its own copy.
 */

/* Floating notifications: every transient note surfaces bottom-center
 * as translucent gradient glass (.gys-float-note in input.css). kind:
 * 'alert-success' for the mint look, anything else renders as
 * error-tinted. options.html replaces the text content (trusted,
 * caller-built markup); options.timeout overrides 3.5s. */
window.gysFloatNote = function (content, kind, timeout) {
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
    if (typeof content === 'string' && /<[a-z][\s\S]*>/i.test(content)) {
        note.innerHTML = content; // trusted: built by our own code only
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

/* Human-readable message from an API error payload. */
window.extractDetail = function (payload, statusCode) {
    if (payload && payload.detail) {
        if (typeof payload.detail === 'string') return payload.detail;
        if (payload.detail.message) return payload.detail.message;
    }
    return 'Request failed (' + statusCode + ').';
};

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
            return htmx.ajax('GET', '/partials/review-drawer', { target: '#drawer-container', swap: 'innerHTML' })
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
