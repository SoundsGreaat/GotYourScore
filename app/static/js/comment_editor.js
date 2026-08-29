/* Reusable rich-comment editor: a Quill instance that behaves 1:1 like
   the review drawer's notes editor — same glow-shell chrome, same
   toolbar (plus a link button), and the same RIGHT-CLICK context menu
   for the actions (AI Refactor / Copy All / Copy HTML). Shared by the
   Bad Feedback record editor and reusable for future record kinds
   (Refund Check). Vendor Quill + DOMPurify load in base.html.

   window.gysCommentEditor({
     mount:       element receiving the editor shell,
     html:        initial sanitized-on-insert HTML (may be ''),
     placeholder: Quill placeholder text,
      refactorUrl: POST endpoint for the Refactor menu item — its NDJSON
                   streaming twin ("<refactorUrl>/stream") drives a
                   token-by-token reveal into the editor; omit to hide
                   the menu item,
     emptyMessage:  toast text for actions on an empty comment,
     toast:       notifier (defaults to window.gysToast),
     onChange:    called on every text change.
   }) -> {
     getHtml(),      // sanitized HTML ('' when the sanitizer is gone)
     isEmpty(),      // whitespace-only AND without embedded images
     setHtml(html),  // replace content through the Quill API
     focus(),
     destroy(),      // abort listeners, remove the menu node
   }

   Menu mechanics mirror review_drawer.html: ul.js-gys-comment-menu
   (same visual classes as the drawer's .js-ai-context-menu — DIFFERENT
   hook class on purpose, the drawer queries that one document-wide),
   absolute position clamped to the viewport, hidden toggled via the
   `hidden` class, 250 ms re-open grace on hide, 200 ms opening-gesture
   guard on the document click, Esc + scroll dismissal. */
(function () {
    var EXPORT_ALLOWED_TAGS = ['p', 'br', 'strong', 'b', 'em', 'i', 'u', 's',
        'ul', 'ol', 'li', 'img', 'a', 'span', 'h1', 'h2', 'h3', 'h4', 'h5',
        'h6', 'blockquote', 'pre', 'code'];
    var SPARKLES_ICON = '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 text-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 0 0-2.456 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z" /></svg>';
    var CLIPBOARD_ICON = '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 text-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.666 3.888A2.25 2.25 0 0 0 13.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 0 1-.75.75H9a.75.75 0 0 1-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 0 1-2.25 2.25H6.75A2.25 2.25 0 0 1 4.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 0 1 1.927-.184" /></svg>';
    var CODE_ICON = '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 text-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.25 6.75 22.5 12l-5.25 5.25M6.75 17.25 1.5 12l5.25-5.25m7.5-3-4.5 16.5" /></svg>';

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
        var controller = new AbortController();
        var signal = { signal: controller.signal };

        /* Editor shell — same chrome as the drawer's notes field
           (glow-shell wrapper draws the :focus-within halo; the
           container ships ql-container/ql-snow and the min-height). */
        var shell = document.createElement('div');
        shell.className = 'glow-shell overflow-hidden rounded-box border border-base-300 bg-base-100';
        var editorHost = document.createElement('div');
        editorHost.className = 'js-gys-quill min-h-24 ql-container ql-snow';
        shell.appendChild(editorHost);
        mount.appendChild(shell);

        /* Context menu — the drawer's .js-ai-context-menu twin. Lives
           INSIDE the nearest <dialog> when the editor is mounted in a
           modal (a body-appended element paints BELOW a top-layer
           dialog no matter its z-index); fixed positioning keeps the
           same clientX/Y math in both hosts. */
        var menu = document.createElement('ul');
        menu.className = 'js-gys-comment-menu menu menu-sm fixed z-[100] hidden w-52 origin-top-left animate-menu-pop rounded-box border border-base-300 bg-base-100 p-1 shadow-md';
        function menuItem(icon, label) {
            var li = document.createElement('li');
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.innerHTML = icon + label;
            btn.dataset.labelHtml = btn.innerHTML;
            li.appendChild(btn);
            menu.appendChild(li);
            return btn;
        }
        var refactorBtn = options.refactorUrl ? menuItem(SPARKLES_ICON, ' Refactor Notes') : null;
        var copyAllBtn = menuItem(CLIPBOARD_ICON, ' Copy All');
        var copyHtmlBtn = menuItem(CODE_ICON, ' Copy HTML');
        // Attached lazily on first open: agent cards are built while
        // DETACHED (renderEdit builds then appends), so closest('dialog')
        // only resolves once the card is in the document.

        var menuOpenedAt = 0;
        function hideMenu(force) {
            if (!force && Date.now() - menuOpenedAt < 250) return;
            menu.classList.add('hidden');
        }

        function setLoading(btn, label, loading) {
            btn.disabled = loading;
            if (loading) {
                btn.textContent = label;
                btn.insertAdjacentHTML('beforeend', ' <span class="loading loading-spinner loading-xs"></span>');
            } else {
                btn.innerHTML = btn.dataset.labelHtml;
            }
        }

        quill = new window.Quill(editorHost, {
            theme: 'snow',
            placeholder: options.placeholder || '',
            modules: { toolbar: [
                ['bold', 'italic', 'underline'],
                [{ list: 'ordered' }, { list: 'bullet' }],
                [{ color: [] }],
                ['link', 'image', 'code-block']
            ] }
        });
        if (options.html) {
            quill.clipboard.dangerouslyPasteHTML(sanitizeHtml(options.html), 'silent');
        }
        if (options.onChange) quill.on('text-change', options.onChange);

        /* Right-click opens the menu, exactly like the drawer. Fixed
           positioning: clientX/Y are viewport coords — no scroll
           offset needed (the dialog host is viewport-fixed too). */
        editorHost.addEventListener('contextmenu', function (event) {
            if (!event.target.closest('.ql-editor')) return;
            event.preventDefault();
            // With several editors mounted (agent cards), opening one
            // menu dismisses every other one first.
            document.querySelectorAll('.js-gys-comment-menu').forEach(function (other) {
                if (other !== menu) other.classList.add('hidden');
            });
            menuOpenedAt = Date.now();
            // Top-layer rule: a body-appended menu paints BELOW an open
            // <dialog> no matter its z-index — live INSIDE the dialog.
            var host = editorHost.closest('dialog') || document.body;
            if (menu.parentNode !== host) host.appendChild(menu);
            menu.classList.remove('hidden');
            menu.style.left = Math.max(8, Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 8)) + 'px';
            menu.style.top = Math.max(8, Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 8)) + 'px';
        }, signal);

        menu.addEventListener('click', function (event) { event.stopPropagation(); });
        document.addEventListener('click', function (event) {
            if (Date.now() - menuOpenedAt < 200) return;
            hideMenu();
        }, signal);
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') hideMenu();
        }, signal);
        window.addEventListener('scroll', function (event) {
            var target = event.target;
            // Scrolling INSIDE the editor keeps the menu (its content
            // may overflow). So does scrolling inside the host dialog:
            // right-clicking an unfocused editor reveals the caret and
            // scrolls the modal box AFTER the menu opened — dismissing
            // here would close it instantly (the drawer ignores every
            // scroll inside .drawer-content for the same reason). Any
            // other scroll dismisses it.
            if (target instanceof Element &&
                (target.closest('.ql-editor') || target.closest('dialog'))) return;
            hideMenu();
        }, Object.assign({ capture: true }, signal));

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

        function isEmpty() {
            return !quill.getText().trim() && !quill.root.querySelector('img');
        }

        if (refactorBtn) {
            refactorBtn.addEventListener('click', async function () {
                if (destroyed) return;
                if (isEmpty()) {
                    toast(options.emptyMessage || 'Comment is empty.');
                    hideMenu(true);
                    return;
                }
                // The menu STAYS OPEN while streaming (the item shows the
                // spinner) and closes only on success — same feedback
                // contract as the drawer's AI menu.
                setLoading(refactorBtn, 'Refactoring…', true);
                try {
                    // NDJSON stream twin of the refactor endpoint (the
                    // buffered URL gets a "/stream" sibling): tokens are
                    // revealed as the model writes; the helper restores
                    // the original comment and throws on any failure.
                    await window.gysAiStream.streamInto({
                        url: options.refactorUrl + '/stream',
                        payload: { html: quill.root.innerHTML },
                        quill: quill,
                        sanitize: sanitizeHtml,
                        emptyMessage: 'The AI returned an empty comment.',
                        signal: controller.signal
                    });
                    toast('Comment refactored.', 'alert-success');
                    hideMenu(true);
                } catch (error) {
                    // Failure keeps the menu open (item label restored)
                    // so the action can be retried — mirrors the drawer.
                    toast(error.message || 'AI request failed.');
                } finally {
                    setLoading(refactorBtn, 'Refactoring…', false);
                }
            });
        }

        copyAllBtn.addEventListener('click', function () {
            if (destroyed) return;
            hideMenu(true);
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
            hideMenu(true);
            var html = buildExportHtml();
            if (!html || isEmpty()) { toast(options.emptyMessage || 'Comment is empty.'); return; }
            copyPlainText(html, function (ok) {
                toast(ok ? 'HTML copied to clipboard.' : 'Unable to copy.',
                    ok ? 'alert-success' : undefined);
            });
        });

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
                controller.abort();
                quill.off('text-change', options.onChange || function () {});
                menu.remove();
            }
        };
    };
})();
