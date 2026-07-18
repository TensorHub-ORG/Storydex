/**
 * Storydex Website - Page Counter & Like Tracker
 *
 * stats.json        → seed file (read once as initial value)
 * localStorage      → primary storage, persists counts per browser
 *
 * On first visit: localStorage seeded from stats.json.
 * On subsequent visits: localStorage is authoritative.
 * stats.json is NOT auto-updated (static hosting limitation).
 */

(function () {
    'use strict';

    var STORAGE_KEY = 'storydex.site.statsV2';
    var STATS_URL = './stats.json';

    function now() { return Date.now(); }

    function defaults() {
        return { totalViews: 0, dailyViews: 0, dailyTimestamp: now(), likes: 0, seeded: false };
    }

    function load() {
        try {
            var raw = window.localStorage.getItem(STORAGE_KEY);
            if (!raw) return null;
            return JSON.parse(raw);
        } catch (e) { return null; }
    }

    function save(data) {
        try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(data)); } catch (e) {}
    }

    function formatNum(n) {
        if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
        if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
        if (n >= 1e3) return (n / 1e3).toFixed(2) + 'K';
        return String(n);
    }

    function updateUI(data) {
        var tv = document.getElementById('totalViews');
        var dv = document.getElementById('dailyViews');
        var lc = document.getElementById('likeCount');
        if (tv) tv.textContent = formatNum(data.totalViews);
        if (dv) dv.textContent = formatNum(data.dailyViews);
        if (lc) lc.textContent = formatNum(data.likes);
    }

    function floatHeart(btn) {
        var heart = document.createElement('span');
        heart.textContent = '\u2665';
        heart.style.cssText =
            'position:absolute;left:' + (Math.random() * 16 + 8) +
            'px;top:0;color:#EF4444;font-size:14px;pointer-events:none;animation:floatUp 0.7s ease-out forwards;';
        btn.style.position = 'relative';
        btn.appendChild(heart);
        setTimeout(function () { heart.remove(); }, 700);
    }

    function init(seed) {
        var data = load();

        // First visit: seed from stats.json
        if (!data || !data.seeded) {
            data = seed && typeof seed.totalViews === 'number'
                ? {
                    totalViews: seed.totalViews || 0,
                    dailyViews: seed.dailyViews || 0,
                    dailyTimestamp: seed.dailyTimestamp || now(),
                    likes: seed.likes || 0,
                    seeded: true
                  }
                : { totalViews: 0, dailyViews: 0, dailyTimestamp: now(), likes: 0, seeded: true };
        }

        // Reset daily counter if 24h passed
        if (now() - data.dailyTimestamp > 86400000) {
            data.dailyViews = 0;
            data.dailyTimestamp = now();
        }

        // Record page view
        data.totalViews += 1;
        data.dailyViews += 1;
        data.dailyTimestamp = now();
        save(data);
        updateUI(data);

        // Like button
        var likeBtn = document.getElementById('likeBtn');
        if (likeBtn) {
            likeBtn.addEventListener('click', function () {
                data.likes += 1;
                save(data);
                updateUI(data);
                likeBtn.classList.remove('popping');
                void likeBtn.offsetWidth;
                likeBtn.classList.add('popping');
                likeBtn.classList.add('liked');
                floatHeart(likeBtn);
            });
        }
    }

    function boot() {
        fetch(STATS_URL, { cache: 'no-cache' })
            .then(function (res) { return res.ok ? res.json() : null; })
            .catch(function () { return null; })
            .then(function (seed) { init(seed); });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
