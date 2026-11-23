/* Sliding-underline tab strip, shared by the dashboard tracker and the
   admin panel. Loaded once (deferred) — runs before htmx initializes,
   so a hash-restored tab can retarget the eager load in time.

   Markup contract (styles: .gys-tabs block in css/src/input.css):

     <div class="gys-tabs" data-tabs [data-hash] role="tablist">
       <button type="button" role="tab" class="active"? data-tab="id"
               [hx-get="..." hx-target="..." | disabled]>Label</button>
       ...
       <span class="gys-tabs-line" aria-hidden="true"></span>
     </div>

   - Arrows move focus; Enter/Space activates the focused tab. Wait for
     the view to swap before changing selection or the panel's label.
   - Requests are declarative: buttons with hx-get/hx-target are fired
     by htmx itself; disabled buttons never activate.
   - data-hash mirrors the active data-tab into the location hash and
     restores it on load (e.g. /admin#scorecards): the matching button
     is activated and its hx-target's eager hx-get is retargeted BEFORE
     htmx fires it. Without data-hash (dashboard) the page's own
     hash-sync owns the URL. */
(function () {
    var tracks = document.querySelectorAll('[data-tabs]');
    if (!tracks.length) return;

    Array.prototype.forEach.call(tracks, function (track) {
        var line = track.querySelector('.gys-tabs-line');
        var buttons = Array.prototype.slice.call(
            // data-tab = declarative navigation tabs (admin panel);
            // data-cat = the dashboard's category-filter tabs, whose
            // click behavior lives in js/dashboard.js — visuals here
            // are shared, so the strip must recognize BOTH.
            track.querySelectorAll('button[role="tab"]:not(:disabled)')
        );
        if (!buttons.length) return;
        var armed = false;
        var responsive = track.hasAttribute('data-responsive-orientation');
        var vertical = false;

        function syncOrientation() {
            if (!responsive) return;
            vertical = getComputedStyle(track).flexDirection === 'column';
            track.setAttribute('aria-orientation', vertical ? 'vertical' : 'horizontal');
        }
        syncOrientation();

        function place() {
            var active = track.querySelector('button.active');
            if (!active || !line) return;
            line.style.left = active.offsetLeft + 'px';
            line.style.width = active.offsetWidth + 'px';
            if (!armed) {
                // Arm transitions one frame AFTER first placement so
                // page load never slides the line in from x=0.
                requestAnimationFrame(function () {
                    track.classList.add('gys-ready');
                    armed = true;
                });
            }
        }

        function reveal(btn) {
            if (vertical) return;
            var item = btn.getBoundingClientRect();
            var list = track.getBoundingClientRect();
            var start = list.left + 2;
            var end = list.left + track.clientWidth - 2;
            if (item.left < start) track.scrollLeft += item.left - start;
            else if (item.right > end) track.scrollLeft += item.right - end;
        }

        function setTabStop(btn) {
            buttons.forEach(function (b) { b.tabIndex = b === btn ? 0 : -1; });
        }

        function syncSelection() {
            var selected = track.querySelector('button[aria-selected="true"]');
            if (!selected) return;
            var focused = document.activeElement;
            var focusInTrack = buttons.indexOf(focused) !== -1;
            setTabStop(focusInTrack ? focused : selected);
            var panel = document.getElementById(track.dataset.panel);
            if (panel) panel.setAttribute('aria-labelledby', selected.id);
            place();
            reveal(focusInTrack ? focused : selected);
        }

        function activate(btn) {
            buttons.forEach(function (b) {
                b.classList.toggle('active', b === btn);
                b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
            });
            syncSelection();
        }

        track.addEventListener('keydown', function (event) {
            var index = buttons.indexOf(event.target);
            if (index < 0 || event.altKey || event.ctrlKey || event.metaKey) return;
            var direction = getComputedStyle(track).direction === 'rtl' ? -1 : 1;
            switch (event.key) {
                case 'ArrowLeft': if (vertical) return; index -= direction; break;
                case 'ArrowRight': if (vertical) return; index += direction; break;
                case 'ArrowUp': if (!vertical) return; index -= 1; break;
                case 'ArrowDown': if (!vertical) return; index += 1; break;
                case 'Home': index = 0; break;
                case 'End': index = buttons.length - 1; break;
                default: return;
            }
            event.preventDefault();
            var next = buttons[(index + buttons.length) % buttons.length];
            setTabStop(next);
            next.focus({preventScroll: true});
            reveal(next);
        });
        track.addEventListener('focusin', function (event) {
            if (buttons.indexOf(event.target) !== -1) setTabStop(event.target);
        });
        track.addEventListener('focusout', function (event) {
            if (!track.contains(event.relatedTarget)) {
                setTabStop(track.querySelector('button[aria-selected="true"]'));
            }
        });

        if (track.hasAttribute('data-hash')) {
            var panel = document.getElementById(track.dataset.panel);
            var wanted = window.location.hash.slice(1);
            var restored = buttons.find(function (b) { return b.dataset.tab === wanted; });
            if (panel && restored) {
                activate(restored);
                panel.setAttribute('hx-get', restored.getAttribute('hx-get'));
            }

            // Keep the last loaded tab on a failed or no-swap response.
            var tabSwaps = new WeakMap();
            document.addEventListener('htmx:before:swap', function (event) {
                var detail = event.detail;
                var source = detail.ctx.sourceElement;
                if (buttons.indexOf(source) !== -1 && detail.tasks.some(function (task) {
                    return task.type === 'main' && task.target === panel && task.swapSpec.style !== 'none';
                })) {
                    tabSwaps.set(detail.ctx, source);
                }
            });
            document.addEventListener('htmx:after:swap', function (event) {
                var tab = tabSwaps.get(event.detail.ctx);
                if (!tab) return;
                activate(tab);
                history.replaceState(null, '', '#' + tab.dataset.tab);
            });
        }

        syncSelection();
        // dashboard.js toggles .active programmatically (hash restore,
        // category filter switches) — it fires this event because plain
        // class toggles never pass through the click handler above and
        // the underline would keep pointing at the previous tab.
        track.addEventListener('gys-tabs-sync', syncSelection);
        window.addEventListener('resize', function () {
            if (responsive) {
                syncOrientation();
                syncSelection();
            } else {
                place();
            }
        });
        if (document.fonts && document.fonts.ready) {
            document.fonts.ready.then(syncSelection);
        }
    });
})();
