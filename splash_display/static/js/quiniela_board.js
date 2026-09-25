/* =====================================================================
   La Quiniela live board — commit 3/3.

   Subscribes to /api/quiniela/stream (Server-Sent Events) and drives the
   #quiniela-board layer that templates/splash/quiniela_live.html renders
   into slideshow.html. Vanilla JS, no framework, nothing external.

   Takeover rule: while model.race_state is in model.board_states (the
   server exposes config.QUINIELA_BOARD_STATES as "board_states"; nothing
   is hard-coded here) the board crossfades in over the playlist and calls
   window.ddmSlideshow.hold(); when the state leaves that set it fades out
   and calls release(), which restarts the same slide's timer from zero.

   A lost link never hides the board: it stays up with its last data and
   a small NO LINK mark appears after 10 s without any SSE message (data
   or ping), or as soon as the model itself reports link_ok false.

   Freeze rule: in the states whose banner says BETTING CLOSED (3
   AT_THE_POST, 4 RUNNING) the tiles, pot and ticker keep the values shown
   when the state was entered; only the banner updates. Back in 1 or 2 the
   live model renders again. The freeze only holds while the board is up:
   a hidden board always takes the live model, so a page that loads
   mid-race, or a board coming back after the server restarted, shows the
   field rather than an earlier hidden paint of an empty model.

   Motion: count changes tween over ~500 ms (requestAnimationFrame writing
   the number) and pulse the tile once via a 400 ms CSS animation; share
   bars are a scaleX transform with a 600 ms transition; ticker chips get
   a one-shot entry animation keyed by horse+ts so re-renders never replay
   it. Everything is transform/opacity only (see quiniela_board.css).
   ===================================================================== */
(() => {
    'use strict';

    const MODEL_URL  = '/api/quiniela';
    const STREAM_URL = '/api/quiniela/stream';
    const STALE_MS       = 10000;   // NO LINK after this long without any SSE message
    const BACKOFF_MIN_MS = 1000;
    const BACKOFF_MAX_MS = 30000;
    const COUNT_TWEEN_MS = 500;
    const MAX_CHIPS      = 8;
    const HORSES         = 20;

    // Kentucky Derby saddle-cloth colors by post position.
    const SADDLE = {
      1:{bg:'#E31837',fg:'#FFFFFF'},  2:{bg:'#FFFFFF',fg:'#000000'},  3:{bg:'#0033A0',fg:'#FFFFFF'},
      4:{bg:'#FFCD00',fg:'#000000'},  5:{bg:'#00843D',fg:'#FFFFFF'},  6:{bg:'#000000',fg:'#FFD700'},
      7:{bg:'#FF6600',fg:'#000000'},  8:{bg:'#FF69B4',fg:'#000000'},  9:{bg:'#40E0D0',fg:'#000000'},
     10:{bg:'#663399',fg:'#FFFFFF'}, 11:{bg:'#808080',fg:'#E31837'}, 12:{bg:'#32CD32',fg:'#000000'},
     13:{bg:'#8B4513',fg:'#FFFFFF'}, 14:{bg:'#800000',fg:'#FFCD00'}, 15:{bg:'#C4B7A6',fg:'#000000'},
     16:{bg:'#87CEEB',fg:'#E31837'}, 17:{bg:'#000080',fg:'#FFFFFF'}, 18:{bg:'#228B22',fg:'#FFCD00'},
     19:{bg:'#00008B',fg:'#E31837'}, 20:{bg:'#FF00FF',fg:'#FFCD00'}
    };

    // Banner per race state. `frozen` states keep the tiles, pot and ticker
    // at their last values. States not listed here (0, 5, 6) normally hide
    // the board; if config ever puts one in board_states the banner falls
    // back to the state's name.
    const BANNERS = {
        1: { text: 'Betting open',   cls: 'qb-banner--open',   frozen: false },
        2: { text: 'Final call',     cls: 'qb-banner--final',  frozen: false },
        3: { text: 'Betting closed', cls: 'qb-banner--closed', frozen: true  },
        4: { text: 'Betting closed', cls: 'qb-banner--closed', frozen: true  },
    };
    const BANNER_CLASSES = ['qb-banner--open', 'qb-banner--final', 'qb-banner--closed'];

    // ---- DOM ---------------------------------------------------------
    const board = document.getElementById('quiniela-board');
    if (!board) return;                       // not the slideshow page

    const potEl        = document.getElementById('qb-pot');
    const bannerEl     = document.getElementById('qb-banner');
    const bannerTextEl = document.getElementById('qb-banner-text');
    const tickerEl     = board.querySelector('.qb-ticker');
    const trackEl      = document.getElementById('qb-ticker');
    const noLinkEl     = document.getElementById('qb-nolink');

    const tiles = {};   // horse -> { el, count, bar, displayed, target, raf }
    board.querySelectorAll('.qb-tile[data-horse]').forEach((el) => {
        const n = parseInt(el.dataset.horse, 10);
        const saddle = el.querySelector('.qb-saddle');
        const colors = SADDLE[n] || { bg: '#808080', fg: '#FFFFFF' };
        saddle.style.background = colors.bg;
        saddle.style.color = colors.fg;
        const bar = el.querySelector('.qb-bar-fill');
        bar.style.background = barColor(n);
        el.addEventListener('animationend', (e) => {
            if (e.target === el) el.classList.remove('is-pulse');
        });
        tiles[n] = { el, count: el.querySelector('.qb-count'), bar, displayed: null, target: null, raf: null };
    });

    // The share bar is drawn in the saddle color; for the cloths that are
    // nearly black (6 black, 17 and 19 navy) that would vanish on the dark
    // panel, so those use the cloth's number color instead.
    function barColor(n) {
        const c = SADDLE[n];
        if (!c) return '#FEC600';
        return relativeLuminance(c.bg) < 0.03 ? c.fg : c.bg;
    }

    function relativeLuminance(hex) {
        const v = parseInt(hex.slice(1), 16);
        const lin = (ch) => {
            const s = ch / 255;
            return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
        };
        return 0.2126 * lin((v >> 16) & 255) + 0.7152 * lin((v >> 8) & 255) + 0.0722 * lin(v & 255);
    }

    // ---- State -------------------------------------------------------
    let model = null;          // latest model from the server
    let visible = false;       // board layer shown
    let frozen = false;        // tiles/pot/ticker held at last values
    let renderedOnce = false;  // tiles have been painted at least once
    let es = null;
    let reconnectTimer = null;
    let backoffMs = BACKOFF_MIN_MS;
    let lastMessageAt = performance.now();
    const chips = new Map();   // "horse:ts" -> chip element

    // ---- Slideshow hook ---------------------------------------------
    function slideshow() {
        return window.ddmSlideshow || null;
    }

    function show() {
        if (visible) return;
        visible = true;
        board.classList.add('is-visible');
        board.setAttribute('aria-hidden', 'false');
        const s = slideshow();
        if (s && typeof s.hold === 'function') s.hold();
    }

    function hide() {
        if (!visible) return;
        visible = false;
        board.classList.remove('is-visible');
        board.setAttribute('aria-hidden', 'true');
        const s = slideshow();
        if (s && typeof s.release === 'function') s.release();
    }

    // ---- Model handling ---------------------------------------------
    function handleModel(m) {
        if (!m || typeof m !== 'object' || !m.horses) return;
        model = m;

        const state = Number(m.race_state);
        const spec = BANNERS[state] || {
            text: String(m.race_state_name || ('state ' + state)).replace(/_/g, ' '),
            cls: 'qb-banner--closed',
            frozen: false,
        };
        const inBoard = Array.isArray(m.board_states) && m.board_states.includes(state);

        // Banner always follows the state.
        renderBanner(spec);
        board.dataset.state = String(state);

        // Tiles, pot and ticker: live unless frozen, and the freeze only
        // holds for updates while the board is up. A hidden board always
        // takes the live model: otherwise the last hidden paint (the empty
        // model a restarted server hands out before the gateway reports,
        // or a page loading mid-race) would become the frozen picture.
        frozen = !!spec.frozen;
        if (!frozen || !renderedOnce || !visible) {
            const animate = visible && renderedOnce;
            renderPot(m, animate);
            renderTiles(m, animate);
            renderTicker(m, animate);
            renderedOnce = true;
        }

        if (inBoard) show(); else hide();
        updateNoLink();
    }

    function renderBanner(spec) {
        for (const c of BANNER_CLASSES) bannerEl.classList.remove(c);
        bannerEl.classList.add(spec.cls);
        if (bannerTextEl.textContent !== spec.text) bannerTextEl.textContent = spec.text;
    }

    function formatPot(pot, tokenValue) {
        const amount = Number(pot) || 0;
        const tv = Number(tokenValue);
        if (Number.isFinite(tv) && Number.isInteger(tv)) return '$' + Math.round(amount);
        return '$' + amount.toFixed(2);
    }

    function renderPot(m) {
        const text = formatPot(m.pot, m.token_value);
        if (potEl.textContent !== text) potEl.textContent = text;
    }

    function renderTiles(m, animate) {
        const leader = m.leader == null ? null : Number(m.leader);

        // The bar is relative to the leader, as on a tote board: the horse
        // with the most tokens fills its tile and everyone else is a fraction
        // of that, so twenty horses at even money still read from across the
        // room (raw share would draw twenty 5 % slivers). Every horse at 0
        // means every bar empty. The model's share field stays the true
        // fraction of the pot; only the bar uses this.
        let most = 0;
        for (let n = 1; n <= HORSES; n++) {
            const h = m.horses[String(n)] || {};
            if (h.scratched) continue;
            most = Math.max(most, parseInt(h.tokens, 10) || 0);
        }
        for (let n = 1; n <= HORSES; n++) {
            const t = tiles[n];
            if (!t) continue;
            const h = m.horses[String(n)] || {};
            const tokens = Math.max(0, parseInt(h.tokens, 10) || 0);
            const scratched = !!h.scratched;
            const width = (scratched || most === 0) ? 0 : Math.min(1, tokens / most);

            t.el.classList.toggle('is-scratched', scratched);
            t.el.classList.toggle('is-offline', h.cup != null && !h.online);
            t.el.classList.toggle('is-leader', leader === n && !scratched);
            t.el.classList.toggle('is-empty', tokens === 0);
            t.bar.style.transform = 'scaleX(' + width.toFixed(4) + ')';
            setCount(t, tokens, animate);
        }
    }

    // Writes the count. Animated: tween from the value on screen and pulse
    // the tile once. Not animated (first paint, or board hidden): snap.
    function setCount(t, tokens, animate) {
        if (t.target === tokens) return;
        t.target = tokens;
        if (!animate || t.displayed == null) {
            cancelTween(t);
            t.displayed = tokens;
            t.count.textContent = String(tokens);
            return;
        }
        tween(t, t.displayed, tokens);
        pulse(t.el);
    }

    function cancelTween(t) {
        if (t.raf) { cancelAnimationFrame(t.raf); t.raf = null; }
    }

    function tween(t, from, to) {
        cancelTween(t);
        const start = performance.now();
        const step = (now) => {
            const p = Math.min(1, (now - start) / COUNT_TWEEN_MS);
            const eased = 1 - Math.pow(1 - p, 3);
            const v = Math.round(from + (to - from) * eased);
            if (v !== t.displayed) {
                t.displayed = v;
                t.count.textContent = String(v);
            }
            t.raf = p < 1 ? requestAnimationFrame(step) : null;
        };
        t.raf = requestAnimationFrame(step);
    }

    function pulse(el) {
        el.classList.remove('is-pulse');
        // eslint-disable-next-line no-unused-expressions
        el.offsetWidth;   // restart the one-shot animation if it is mid-flight
        el.classList.add('is-pulse');
    }

    // Ticker: model.events is newest first; the strip shows oldest to newest
    // left to right so the newest slides in from the right. Chips are keyed
    // by horse+ts: existing ones are kept in place (no re-animation), gone
    // ones removed, new ones appended with the entry animation.
    function renderTicker(m, animate) {
        const events = Array.isArray(m.events) ? m.events.slice(0, MAX_CHIPS) : [];
        const keep = new Set();
        const ordered = events.slice().reverse();
        for (const ev of ordered) {
            keep.add(chipKey(ev));
        }
        for (const [key, el] of chips) {
            if (!keep.has(key)) { el.remove(); chips.delete(key); }
        }
        for (const ev of ordered) {
            const key = chipKey(ev);
            if (chips.has(key)) continue;
            const el = makeChip(ev, animate);
            chips.set(key, el);
            trackEl.appendChild(el);
        }
        tickerEl.classList.toggle('has-events', chips.size > 0);
    }

    function chipKey(ev) {
        return String(ev.horse) + ':' + String(ev.ts);
    }

    function makeChip(ev, animate) {
        const horse = parseInt(ev.horse, 10);
        const delta = parseInt(ev.delta, 10) || 0;
        const colors = SADDLE[horse] || { bg: '#808080', fg: '#FFFFFF' };

        const chip = document.createElement('span');
        chip.className = 'qb-chip' + (animate ? ' is-new' : '');

        const num = document.createElement('span');
        num.className = 'qb-chip-num';
        num.style.background = colors.bg;
        num.style.color = colors.fg;
        num.textContent = '#' + horse;

        const d = document.createElement('span');
        d.className = 'qb-chip-delta' + (delta < 0 ? ' qb-chip-delta--neg' : '');
        d.textContent = (delta < 0 ? '−' : '+') + Math.abs(delta);

        chip.appendChild(num);
        chip.appendChild(d);
        if (animate) {
            chip.addEventListener('animationend', () => chip.classList.remove('is-new'), { once: true });
        }
        return chip;
    }

    // ---- NO LINK mark -----------------------------------------------
    function updateNoLink() {
        const stale = (performance.now() - lastMessageAt) > STALE_MS;
        const down = !!model && model.link_ok === false;
        noLinkEl.classList.toggle('is-visible', stale || down);
    }

    function touch() {
        lastMessageAt = performance.now();
        backoffMs = BACKOFF_MIN_MS;
        updateNoLink();
    }

    // ---- Stream ------------------------------------------------------
    function connect() {
        if (es) return;
        if (typeof EventSource === 'undefined') {
            console.warn('[quiniela] EventSource unsupported; board disabled');
            return;
        }
        try {
            es = new EventSource(STREAM_URL);
        } catch (err) {
            console.warn('[quiniela] stream open failed:', err);
            scheduleReconnect();
            return;
        }
        es.onmessage = (e) => {
            touch();
            let m = null;
            try { m = JSON.parse(e.data); } catch (err) { console.warn('[quiniela] bad model:', err); }
            if (m) handleModel(m);
        };
        es.addEventListener('ping', touch);
        es.onerror = () => {
            // Close and reopen on our own backoff rather than trusting the
            // browser's retry; the board stays up with its last data.
            if (es) { es.close(); es = null; }
            scheduleReconnect();
        };
    }

    function scheduleReconnect() {
        if (reconnectTimer) return;
        const delay = backoffMs;
        backoffMs = Math.min(BACKOFF_MAX_MS, backoffMs * 2);
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            connect();
        }, delay);
    }

    async function fetchOnce() {
        try {
            const resp = await fetch(MODEL_URL, { cache: 'no-store' });
            if (!resp.ok) return;
            const m = await resp.json();
            handleModel(m);
        } catch (err) {
            // No server-side model yet (or no network): the stream will bring it.
        }
    }

    // ---- Boot --------------------------------------------------------
    fetchOnce();
    connect();
    setInterval(updateNoLink, 1000);
})();
