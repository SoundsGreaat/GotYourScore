/* Reusable rich-comment editor: a Quill instance plus the shared
   action set (AI Refactor / Copy All / Copy HTML), extracted from the
   review drawer's notes machinery so the Bad Feedback editor (and the
   future Refund Check editor) mount the exact same experience per
   agent card without duplicating the clipboard/AI plumbing.

   Markup contract: vendor Quill + DOMPurify are loaded in base.html.

   window.gysCommentEditor({
     mount:       element receiving the toolbar + editor,
     html:        initial sanitized-on-insert HTML (may be ''),
     placeholder: Quill placeholder text,
     refactorUrl: POST endpoint returning {html} — omit to hide the
                  Refactor button,
     refactorLabel: button caption (default "Refactor with AI"),
     emptyMessage:  toast text for actions on an empty comment,
     toast:       notifier (defaults to window.gysToast),
     onChange:    called on every text change.
   }) -> {
     getHtml(),      // sanitized HTML ('' when the sanitizer is gone)
     isEmpty(),      // whitespace-only AND without embedded images
     setHtml(html),  // replace content through the Quill API
     focus(),
     destroy(),      // unhook listeners (the caller removes the DOM)
   }

   Copy semantics mirror the review drawer: Copy All puts rich text on
   the clipboard (selection + execCommand, innerText fallback); Copy
   HTML exports CRM-safe plain-text markup (Quill chrome stripped,
   bullet-only <ol> swapped to <ul>, DOMPurify allowlist, data:-URIs
   on <img> preserved). */
(function () {
    var AI_TIMEOUT_MS = 61000;
    var EXPORT_ALLOWED_TAGS = ['p', 'br', 'strong', 'b', 'em', 'i', 'u', 's',
        'ul', 'ol', 'li', 'img', 'a', 'span', 'h1', 'h2', 'h3', 'h4', 'h5',
        'h6', 'blockquote', 'pre', 'code'];

    function sanitizeHtml(html) {
        return window.DOMPurify ? window.DOMPurify.sanitize(html) : '';
    }

    function copyPlainText(text, done) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
            return;
        }
        // Non-secure-context fallback: hidden textarea + execCommand.
        var scratch = document.createElement('textarea');
        scratch.value = text;
        scratch.setAttribute('readonly', '');
        scratch.style.position = 'fixed';
        scratch.style.opacity = '0';
        document.body.appendChild(scratch);
        scratch.select();
        var copied = false;
        try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
        scratch.remove();
        done(copied);
    }

    window.gysCommentEditor = function (options) {
        var mount = options.mount;
        var toast = options.toast || window.gysToast || function () {};
        var quill = null;
        var destroyed = false;

        var toolbar = document.createElement('div');
        toolbar.className = 'mb-1 flex flex-wrap items-center gap-1';

        function button(label, title) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-ghost btn-xs';
            btn.textContent = label;
            btn.title = title;
            btn.dataset.labelHtml = btn.innerHTML;
            return btn;
        }

        var refactorBtn = options.refactorUrl ? button(options.refactorLabel || 'Refactor with AI', 'Rewrite this comment with AI') : null;
        var copyAllBtn = button('Copy All', 'Copy the formatted comment');
        var copyHtmlBtn = button('Copy HTML', 'Copy CRM-ready HTML source');
        if (refactorBtn) toolbar.appendChild(refactorBtn);
        toolbar.appendChild(copyAllBtn);
        toolbar.appendChild(copyHtmlBtn);
        mount.appendChild(toolbar);

        var editorHost = document.createElement('div');
        editorHost.className = 'min-h-24 rounded-box border border-base-300 bg-base-100';
        mount.appendChild(editorHost);

        function setLoading(btn, label, loading) {
            btn.disabled = loading;
            if (loading) {
                btn.textContent = label;
                btn.insertAdjacentHTML('beforeend', ' <span class="loading loading-spinner loading-xs"></span>');
            } else {
                btn.innerHTML = btn.dataset.labelHtml;
            }
        }

        function aiFetch(url, payload) {
            var ctrl = new AbortController();
            var timer = window.setTimeout(function () { ctrl.abort(); }, AI_TIMEOUT_MS);
            return fetch(url, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                signal: ctrl.signal
            }).finally(function () { window.clearTimeout(timer); });
        }

        quill = new window.Quill(editorHost, {
            theme: 'snow',
            placeholder: options.placeholder || '',
            modules: { toolbar: [
                ['bold', 'italic', 'underline'],
                [{ list: 'ordered' }, { list: 'bullet' }],
                ['link', 'code-block']
            ] }
        });
        if (options.html) {
            quill.clipboard.dangerouslyPasteHTML(sanitizeHtml(options.html), 'silent');
        }
        if (options.onChange) quill.on('text-change', options.onChange);

        // CRM paste target is its Source Code editor, which receives the
        // markup literally — so the export ships as PLAIN TEXT on the
        // clipboard (a text/html flavor would hit the CRM's rich-paste
        // sanitizer). Mirrors the review drawer's export byte-for-byte.
        function buildExportHtml() {
            var clone = quill.root.cloneNode(true);
            clone.querySelectorAll('.ql-ui').forEach(function (el) { el.remove(); });
            clone.querySelectorAll('[contenteditable]').forEach(function (el) { el.removeAttribute('contenteditable'); });
            clone.querySelectorAll('[class]').forEach(function (el) { el.removeAttribute('class'); });
            Array.prototype.forEach.call(clone.querySelectorAll('ol'), function (list) {
                var items = Array.prototype.filter.call(list.children, function (el) { return el.tagName === 'LI'; });
                var bulletOnly = items.length > 0 && items.every(function (li) {
                    return li.getAttribute('data-list') === 'bullet';
                });
                if (bulletOnly) {
                    var ul = document.createElement('ul');
                    while (list.firstChild) ul.appendChild(list.firstChild);
                    list.replaceWith(ul);
                }
            });
            clone.querySelectorAll('li[data-list]').forEach(function (li) { li.removeAttribute('data-list'); });
            // DOMPurify keeps data: URIs on <img> by default — embedded
            // screenshots survive the export. If it never loaded,
            // exporting nothing beats exporting unsanitized markup.
            return window.DOMPurify
                ? window.DOMPurify.sanitize(clone.innerHTML, { ALLOWED_TAGS: EXPORT_ALLOWED_TAGS, KEEP_CONTENT: true }).trim()
                : '';
        }

        if (refactorBtn) {
            refactorBtn.addEventListener('click', async function () {
                if (destroyed) return;
                if (isEmpty()) { toast(options.emptyMessage || 'Comment is empty.'); return; }
                setLoading(refactorBtn, 'Refactoring…', true);
                try {
                    var response = await aiFetch(options.refactorUrl, { html: quill.root.innerHTML });
                    if (!response.ok) {
                        var err = null;
                        try { err = (await response.json()).detail; } catch (e) { /* ignore */ }
                        throw new Error(err || 'AI refactoring failed.');
                    }
                    var clean = sanitizeHtml((await response.json()).html);
                    // Replace content through the Quill API: a direct
                    // innerHTML write desyncs Quill's internal delta.
                    quill.deleteText(0, quill.getLength());
                    quill.clipboard.dangerouslyPasteHTML(0, clean);
                    toast('Comment refactored.', 'alert-success');
                } catch (error) {
                    toast(error.name === 'AbortError'
                        ? 'AI request timed out.'
                        : (error.message || 'AI request failed.'));
                } finally {
                    setLoading(refactorBtn, 'Refactoring…', false);
                }
            });
        }

        copyAllBtn.addEventListener('click', function () {
            if (destroyed) return;
            if (isEmpty()) { toast(options.emptyMessage || 'Comment is empty.'); return; }
            var source = quill.root;
            var selection = window.getSelection();
            var copied = false;
            if (selection) {
                selection.removeAllRanges();
                var range = document.createRange();
                range.selectNodeContents(source);
                selection.addRange(range);
                try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
                selection.removeAllRanges();
            }
            if (!copied && navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(source.innerText).then(function () {
                    toast('Copied to clipboard.', 'alert-success');
                }, function () { toast('Unable to copy.'); });
            } else {
                toast(copied ? 'Copied to clipboard.' : 'Unable to copy.',
                    copied ? 'alert-success' : undefined);
            }
        });

        copyHtmlBtn.addEventListener('click', function () {
            if (destroyed) return;
            var html = buildExportHtml();
            if (!html || isEmpty()) { toast(options.emptyMessage || 'Comment is empty.'); return; }
            copyPlainText(html, function (ok) {
                toast(ok ? 'HTML copied to clipboard.' : 'Unable to copy.',
                    ok ? 'alert-success' : undefined);
            });
        });

        function isEmpty() {
            return !quill.getText().trim() && !quill.root.querySelector('img');
        }

        return {
            getHtml: function () { return sanitizeHtml(quill.root.innerHTML); },
            isEmpty: isEmpty,
            setHtml: function (html) {
                quill.deleteText(0, quill.getLength());
                quill.clipboard.dangerouslyPasteHTML(0, sanitizeHtml(html));
            },
            focus: function () { quill.focus(); },
            destroy: function () {
                destroyed = true;
                quill.off('text-change', options.onChange || function () {});
            }
        };
    };
})();
