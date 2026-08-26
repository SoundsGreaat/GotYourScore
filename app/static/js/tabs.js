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

   - Clicks move .active + aria-selected and glide the underline
     (reduced-motion users get instant placement via CSS).
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
        var buttons = Array.prototype.slice.call(track.querySelectorAll('button[data-tab]'));
        if (!buttons.length) return;
        var armed = false;

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

        function activate(btn) {
            buttons.forEach(function (b) {
                b.classList.toggle('active', b === btn);
                b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
            });
            place();
        }

        buttons.forEach(function (btn) {
            btn.addEventListener('click', function () {
                if (btn.disabled) return;
                activate(btn);
                if (track.hasAttribute('data-hash')) {
                    // Mirror into the hash so reloads reopen the same tab.
                    history.replaceState(null, '', '#' + btn.getAttribute('data-tab'));
                }
            });
        });

        if (track.hasAttribute('data-hash')) {
            var wanted = window.location.hash.replace('#', '');
            if (wanted) {
                var restored = null;
                buttons.forEach(function (b) {
                    if (b.getAttribute('data-tab') === wanted) restored = b;
                });
                if (restored && !restored.disabled) {
                    activate(restored);
                    var targetSelector = restored.getAttribute('hx-target');
                    var eagerTarget = targetSelector && document.querySelector(targetSelector);
                    if (eagerTarget && restored.getAttribute('hx-get')) {
                        eagerTarget.setAttribute('hx-get', restored.getAttribute('hx-get'));
                    }
                }
            }
        }

        place();
        window.addEventListener('resize', function () { place(); });
        if (document.fonts && document.fonts.ready) {
            document.fonts.ready.then(function () { place(); });
        }
    });
})();
