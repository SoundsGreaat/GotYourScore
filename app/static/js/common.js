/* Shared front-end helpers for the HTMX partials.
 *
 * Loaded once from base.html; every partial script binds these to local
 * names instead of re-declaring its own copy.
 */

/* Transient toast in the bottom-right corner. kind is a daisyUI alert
 * modifier, e.g. 'alert-error' (default) or 'alert-success'. */
window.gysToast = function (message, kind) {
    var toast = document.createElement('div');
    toast.setAttribute('role', 'alert');
    toast.className = 'alert ' + (kind || 'alert-error') + ' fixed bottom-4 right-4 z-50 w-auto max-w-sm shadow-lg';
    toast.textContent = message;
    document.body.appendChild(toast);
    window.setTimeout(function () { toast.remove(); }, 3500);
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
