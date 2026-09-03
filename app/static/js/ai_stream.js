/* Streaming AI insertion for Quill editors — the "AI chat" feel.
   Used by the review drawer (Refactor Notes, Score -> Notes) and the
   shared comment editor (Bad Feedback Refactor).

   window.gysAiStream.streamInto({
     url:         POST endpoint speaking the NDJSON /stream contract of
                  the AI endpoints ({"d": delta} / {"final": html} / {"error"}),
     payload:     JSON body,
     quill:       the Quill instance to type into,
     sanitize:    per-surface sanitizer applied to EVERY fragment before
                  it reaches the editor (default: DOMPurify),
     signal:      optional external AbortSignal (drawer dispose / editor
                  destroy) that cancels the request,
     focusEnd:    keep the caret at the end of the revealed text (Score
                  -> Notes sets the selection; Refactor does not),
     preserveImages: replace editor images with lightweight markers in a
                  refactor request, then put the original images back in
                  the terminal HTML. This keeps data-URI image bytes out
                  of the model context and makes the final swap lossless,
     emptyMessage: error thrown when the final fragment sanitizes to
                  nothing (per-surface wording),
     timeoutMs:   overall request cap (default 61000, mirrors aiFetch).
   }) -> Promise; resolves after the terminal {"final"} fragment has
   replaced the revealed content, rejects with Error(message) on any
   failure. The editor content is NEVER left mid-revealed: the original
   content is captured up front and restored (silently) before the
   promise rejects, and nothing is touched at all until the first delta
   arrives — early failures (503, dead connection) leave the editor
   exactly as it was.

   Reveal mechanics: raw model text accumulates in `accumulated`; a
   requestAnimationFrame loop re-renders a PREFIX of it into Quill each
   frame. The prefix is cut at a text-node boundary (Range.cloneContents
   auto-closes open tags — no broken partial markup), the drain is
   exponential (Math.ceil(remaining / 24)) so bursts catch up quickly
   while token-pace arrival stays gentle, and `prefers-reduced-motion`
   reveals instantly. Every rendered prefix AND the final fragment pass
   through the surface's sanitizer — the raw stream is never trusted. */
(function () {
    var REDUCED_MOTION = window.matchMedia
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function defaultSanitize(html) {
        return window.DOMPurify ? window.DOMPurify.sanitize(html) : '';
    }

    /* Quill commonly stores pasted screenshots as large data: URIs. They
       are not useful to a writing refactor, but sending them consumes the
       context window and asking the model to reproduce them makes the
       terminal replacement lossy. Keep their location as a tiny marker;
       their original markup never leaves the browser. */
    function protectImages(html) {
        var tpl = document.createElement('template');
        tpl.innerHTML = html;
        var images = [];
        Array.prototype.forEach.call(tpl.content.querySelectorAll('img'), function (image, index) {
            images.push(image.outerHTML);
            var marker = document.createElement('span');
            marker.setAttribute('data-gys-image-marker', String(index));
            marker.textContent = '\uFEFF';
            image.replaceWith(marker);
        });
        return { html: tpl.innerHTML, images: images };
    }

    /* Rehydrate each marker at most once. If a model accidentally omits a
       marker, append that image rather than silently losing user content. */
    function restoreImages(html, images) {
        if (!images.length) return html;
        var tpl = document.createElement('template');
        tpl.innerHTML = html;
        var used = Object.create(null);

        Array.prototype.forEach.call(
            tpl.content.querySelectorAll('[data-gys-image-marker]'),
            function (marker) {
                var index = Number(marker.getAttribute('data-gys-image-marker'));
                if (!Number.isInteger(index) || index < 0 || index >= images.length || used[index]) {
                    marker.remove();
                    return;
                }
                var imageTpl = document.createElement('template');
                imageTpl.innerHTML = images[index];
                marker.replaceWith(imageTpl.content.cloneNode(true));
                used[index] = true;
            }
        );

        images.forEach(function (imageHtml, index) {
            if (used[index]) return;
            var imageTpl = document.createElement('template');
            imageTpl.innerHTML = imageHtml;
            tpl.content.appendChild(imageTpl.content.cloneNode(true));
        });
        return tpl.innerHTML;
    }

    /* Prefix `maxChars` (text-node characters) of an HTML string as
       valid markup, plus the FULL visible length of the string. */
    function prefixOf(html, maxChars) {
        var tpl = document.createElement('template');
        tpl.innerHTML = html;
        var walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_TEXT);
        var total = 0;
        var endNode = null;
        var endOffset = 0;
        var node;
        while ((node = walker.nextNode())) {
            var len = node.textContent.length;
            if (!endNode && total + len >= maxChars) {
                endNode = node;
                endOffset = maxChars - total;
            }
            total += len;
        }
        if (!endNode) return { total: total, html: html };
        var range = document.createRange();
        range.setStart(tpl.content, 0);
        range.setEnd(endNode, endOffset);
        var holder = document.createElement('div');
        holder.appendChild(range.cloneContents());
        return { total: total, html: holder.innerHTML };
    }

    window.gysAiStream = {
        streamInto: async function (opts) {
            var quill = opts.quill;
            var sanitize = opts.sanitize || defaultSanitize;
            var ctrl = new AbortController();
            var timer = window.setTimeout(function () { ctrl.abort(); }, opts.timeoutMs || 61000);
            var onExternalAbort = function () { ctrl.abort(); };
            if (opts.signal) {
                if (opts.signal.aborted) ctrl.abort();
                else opts.signal.addEventListener('abort', onExternalAbort, { once: true });
            }

            // Captured BEFORE any mutation; restoring is a no-op while
            // the reveal never started (editor untouched).
            var originalHtml = quill.root.innerHTML;
            var imageSnapshot = opts.preserveImages ? protectImages(originalHtml) : null;
            var requestPayload = imageSnapshot
                ? Object.assign({}, opts.payload, { html: imageSnapshot.html })
                : opts.payload;
            var accumulated = '';
            var revealed = 0;
            var started = false;
            var streamEnded = false;
            var finalHtml = null;
            var streamError = null;

            function replaceContent(html) {
                quill.deleteText(0, quill.getLength(), 'silent');
                quill.clipboard.dangerouslyPasteHTML(0, html, 'silent');
            }

            function restoreOriginal() {
                if (!started) return;
                replaceContent(sanitize(originalHtml));
            }

            function renderFrame() {
                var state = prefixOf(accumulated, revealed);
                if (state.total > revealed) {
                    if (!started) {
                        started = true;
                        replaceContent('');
                    }
                    var remaining = state.total - revealed;
                    var step = REDUCED_MOTION ? remaining : Math.max(3, Math.ceil(remaining / 24));
                    revealed = Math.min(state.total, revealed + step);
                    var cut = revealed >= state.total ? state : prefixOf(accumulated, revealed);
                    replaceContent(sanitize(cut.html));
                    if (opts.focusEnd) quill.setSelection(quill.getLength(), 0, 'silent');
                }
                return state;
            }

            try {
                var response = await fetch(opts.url, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(requestPayload),
                    signal: ctrl.signal
                });
                if (!response.ok) {
                    var detail = null;
                    try { detail = (await response.json()).detail; } catch (e) { /* non-JSON body */ }
                    throw new Error(detail || 'AI request failed.');
                }

                var pump = (async function () {
                    var reader = response.body.getReader();
                    var decoder = new TextDecoder();
                    var buf = '';
                    for (;;) {
                        var chunk = await reader.read();
                        if (chunk.done) break;
                        buf += decoder.decode(chunk.value, { stream: true });
                        var nl;
                        while ((nl = buf.indexOf('\n')) >= 0) {
                            var line = buf.slice(0, nl).trim();
                            buf = buf.slice(nl + 1);
                            if (!line) continue;
                            var evt;
                            try { evt = JSON.parse(line); } catch (e) { continue; }
                            if (evt.error) { streamError = new Error(evt.error); return; }
                            if (typeof evt.d === 'string') accumulated += evt.d;
                            if (typeof evt.final === 'string') finalHtml = evt.final;
                        }
                    }
                    streamEnded = true;
                    // Server died mid-stream without a terminal event:
                    // treat it as an error so the render loop can stop
                    // and the caller gets its original content back.
                    if (finalHtml === null && !streamError) {
                        streamError = new Error('AI request failed.');
                    }
                })();

                var render = new Promise(function (resolve) {
                    function frame() {
                        if (streamError) { resolve(); return; }
                        var state = renderFrame();
                        if (streamEnded && finalHtml !== null && revealed >= state.total) { resolve(); return; }
                        requestAnimationFrame(frame);
                    }
                    requestAnimationFrame(frame);
                });

                await Promise.all([pump, render]);
            } catch (error) {
                restoreOriginal();
                if (error && error.name === 'AbortError') {
                    throw new Error('AI request timed out.');
                }
                throw error && error.message ? error : new Error('AI request failed.');
            } finally {
                window.clearTimeout(timer);
                if (opts.signal) opts.signal.removeEventListener('abort', onExternalAbort);
            }

            if (streamError) {
                restoreOriginal();
                throw streamError;
            }
            if (finalHtml === null) {
                restoreOriginal();
                throw new Error('AI request failed.');
            }
            var finalWithImages = imageSnapshot
                ? restoreImages(finalHtml, imageSnapshot.images)
                : finalHtml;
            var finalClean = sanitize(finalWithImages);
            if (!finalClean.trim()) {
                restoreOriginal();
                throw new Error(opts.emptyMessage || 'The AI returned an empty note.');
            }
            // Terminal swap: the post-processed fragment replaces the
            // revealed approximation — byte-for-byte parity with what
            // the buffered endpoints return today.
            replaceContent(finalClean);
            if (opts.focusEnd) quill.setSelection(quill.getLength(), 0, 'silent');
        }
    };
})();
