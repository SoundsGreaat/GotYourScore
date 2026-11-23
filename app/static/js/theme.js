(function () {
    var root = document.documentElement;
    var scheme = document.querySelector('meta[name="color-scheme"]');
    var system = window.matchMedia('(prefers-color-scheme: dark)');
    var motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    var override = scheme.content === 'light dark' ? null : scheme.content;
    var pendingThemeTimer;
    var fadeTimer;
    var version = 0;

    function systemTheme() { return system.matches ? 'dark' : 'light'; }

    function syncToggles(theme) {
        var dark = theme === 'dark';
        document.querySelectorAll('[data-theme-toggle]').forEach(function (cb) {
            cb.checked = dark;
            // A class allows the thumb transition to run on :checked changes.
            var wrap = cb.closest('.toggle');
            if (wrap) wrap.classList.toggle('toggle-on', dark);
        });
    }

    function applyTheme(theme, animate, token) {
        function set() {
            if (token !== version) return;
            scheme.content = override || 'light dark';
            root.dataset.theme = theme;
        }
        if (!animate || root.dataset.theme === theme || motion.matches) {
            set();
        } else if (document.startViewTransition) {
            document.startViewTransition(set);
        } else {
            // Arm the CSS transitions before changing palette tokens.
            root.classList.add('gys-theme-fade');
            requestAnimationFrame(set);
            clearTimeout(fadeTimer);
            fadeTimer = window.setTimeout(function () { root.classList.remove('gys-theme-fade'); }, 400);
        }
    }

    function refreshTheme(fromToggle) {
        var theme = override || systemTheme();
        var token = ++version;
        clearTimeout(pendingThemeTimer);
        syncToggles(theme);
        if (fromToggle && !motion.matches) {
            // Let the thumb finish its glide before taking transition snapshots.
            pendingThemeTimer = setTimeout(function () { applyTheme(theme, true, token); }, 210);
        } else {
            applyTheme(theme, false, token);
        }
    }

    document.addEventListener('change', function (event) {
        var cb = event.target.closest('[data-theme-toggle]');
        if (!cb) return;
        var theme = cb.checked ? 'dark' : 'light';
        override = theme === systemTheme() ? null : theme;
        try {
            if (override) localStorage.setItem('theme', override);
            else localStorage.removeItem('theme');
        } catch (e) { /* storage blocked */ }
        refreshTheme(true);
    });
    system.addEventListener('change', function () { if (!override) refreshTheme(false); });
    window.addEventListener('storage', function (event) {
        if (event.storageArea !== localStorage || (event.key !== 'theme' && event.key !== null)) return;
        override = event.newValue === 'light' || event.newValue === 'dark' ? event.newValue : null;
        refreshTheme(false);
    });
    refreshTheme(false);
})();
