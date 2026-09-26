/* =====================================================================
   La Quiniela live board.

   Subscribes to /api/quiniela/stream (Server-Sent Events) and drives the
   #quiniela-board layer that templates/splash/quiniela_live.html renders
   into slideshow.html. Vanilla JS, no framework, nothing external. The
   design is tools/board_reference.html.

   How La Quiniela pays: a token is $1; after the race one token is drawn
   from each of the WIN, PLACE and SHOW cups and its owner takes that cup's
   whole prize, a fixed fraction of the pot. Nobody splits anything, so
   there are no odds and no shares here: the board shows the pot, the three
   prizes, and how many tokens sit in each horse's cup.

   Horses are numbers 1-24: 1-20 the field, 21-24 the also-eligibles, who
   are not in the field until one replaces a scratched horse and, as at
   Churchill, keeps its own program number (The Puma #9 scratches, Ocelli
   runs as #22, not as #9). The rows are the field: every horse the model
   marks in_field, in numeric order, the first ten down the left column
   and the rest down the right. A scratched horse (either kind) is never a
   row; a field short of twenty leaves the trailing slots empty. The row
   set is rebuilt whenever it changes, with no motion of its own: a horse
   that draws in appears with its count, the scratched one is gone.

   Takeover rule: while model.race_state is in model.board_states (the
   server exposes pi5's QUINIELA_BOARD_STATES as "board_states"; nothing is
   hard-coded here) the board crossfades in over the playlist and calls
   window.ddmSlideshow.hold(); when the state leaves that set it fades out
   and calls release(), which restarts the same slide's timer from zero.

   A lost link never hides the board: it stays up with its last data and
   a small NO LINK mark appears after 10 s without any SSE message (data
   or ping), or as soon as the model itself reports link_ok false.

   Freeze rule: in the states whose banner says BETTING CLOSED (3
   AT_THE_POST, 4 RUNNING) the rows (the set and the counts), pot and
   prizes keep the values shown when the state was entered and the CLOSES
   IN line is hidden; the banner stays live, and so do the names and the
   chyron (a late name correction still shows). Back in 1 or 2 the live
   model renders again. The freeze only holds while the board is up: a
   hidden board always takes the live model, so a page that loads
   mid-race, or a board coming back after the server restarted, shows the
   field rather than an earlier hidden paint of an empty model.

   The model (GET /api/quiniela, relayed from pi5 untouched). The board
   reads: race_state, board_states, link_ok, token_value, pot, horses[n]
   {tokens, in_field, scratched, online, cup, name}, events[{horse, delta,
   ts}], and the additive keys now, closes_at, prizes{win,place,show},
   chyron[], scratches[{was:{number,name}, now:{number,name}|null}],
   names_rev. Every new key is optional: before pi5 has been heard the
   model carries none of them and the board renders without errors
   (hidden, since board_states is empty). Without in_field (that empty
   model, or an older pi5) the field is horses 1-20 that are not
   scratched.

   Motion: count changes tween over ~500 ms (requestAnimationFrame writing
   the number) and pulse the row once via a 400 ms CSS animation; the toast
   is a 150 ms pop / 200 ms drop on transform and opacity; the chyron crawl
   is one CSS transform animation. Everything is transform/opacity only
   (see quiniela_board.css).
   ===================================================================== */
(() => {
    'use strict';

    const MODEL_URL  = '/api/quiniela';
    const STREAM_URL = '/api/quiniela/stream';
    const STALE_MS       = 10000;   // NO LINK after this long without any SSE message
    const BACKOFF_MIN_MS = 1000;
    const BACKOFF_MAX_MS = 30000;
    const COUNT_TWEEN_MS = 500;
    const HORSES         = 24;      // 1-20 the field, 21-24 the also-eligibles
    const FIELD_MAX      = 20;      // without in_field, horses 1-20 are the field
    const SLOTS_PER_COL  = 10;      // the first ten rows go left, the rest right
    const NAME_MAX_PX    = 38;      // the name shrinks from here ...
    const NAME_MIN_PX    = 20;      // ... down to here, never wraps
    const TOAST_IN_MS    = 150;     // the pop, matches .qb-toast.is-shown's transition
    const TOAST_HOLD_MS  = 2500;    // fully up for this long, then ...
    const TOAST_OUT_MS   = 200;     // ... the drop, matches .qb-toast.is-leaving
    const TOAST_NAME_MAX_PX = 64;   // the toast's name shrinks from here ...
    const TOAST_NAME_MIN_PX = 36;   // ... down to here, then clips
    const CRAWL_PX_S     = 120;     // chyron speed
    const CLOSES_TICK_MS = 250;     // the countdown is checked 4x a second, written once a second

    // Kentucky Derby saddle-cloth colors by program number: 1-20 the
    // field's, 21-24 placeholders for the also-eligibles, the same as the
    // cup firmware shows.
    const SADDLE = {
      1:{bg:'#E31837',fg:'#FFFFFF'},  2:{bg:'#FFFFFF',fg:'#000000'},  3:{bg:'#0033A0',fg:'#FFFFFF'},
      4:{bg:'#FFCD00',fg:'#000000'},  5:{bg:'#00843D',fg:'#FFFFFF'},  6:{bg:'#000000',fg:'#FFD700'},
      7:{bg:'#FF6600',fg:'#000000'},  8:{bg:'#FF69B4',fg:'#000000'},  9:{bg:'#40E0D0',fg:'#000000'},
     10:{bg:'#663399',fg:'#FFFFFF'}, 11:{bg:'#808080',fg:'#E31837'}, 12:{bg:'#32CD32',fg:'#000000'},
     13:{bg:'#8B4513',fg:'#FFFFFF'}, 14:{bg:'#800000',fg:'#FFCD00'}, 15:{bg:'#C4B7A6',fg:'#000000'},
     16:{bg:'#87CEEB',fg:'#E31837'}, 17:{bg:'#000080',fg:'#FFFFFF'}, 18:{bg:'#228B22',fg:'#FFCD00'},
     19:{bg:'#00008B',fg:'#E31837'}, 20:{bg:'#FF00FF',fg:'#FFCD00'},
     21:{bg:'#FFDAB9',fg:'#000000'}, 22:{bg:'#008080',fg:'#FFFFFF'}, 23:{bg:'#808000',fg:'#FFFFFF'},
     24:{bg:'#2F4F4F',fg:'#FFFFFF'}
    };
    const SADDLE_FALLBACK = { bg: '#808080', fg: '#FFFFFF' };

    // Banner per race state. `frozen` states keep the rows, pot and prizes
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
    const $ = (id) => document.getElementById(id);

    const potEl         = $('qb-pot');
    const tokenValueEl  = $('qb-token-value');
    const prizeEls      = { win: $('qb-prize-win'), place: $('qb-prize-place'), show: $('qb-prize-show') };
    const bannerEl      = $('qb-banner');
    const bannerTextEl  = $('qb-banner-text');
    const closesEl      = $('qb-closes');
    const closesLabelEl = $('qb-closes-label');
    const closesTimeEl  = $('qb-closes-time');
    const colEls        = [$('qb-col-left'), $('qb-col-right')];
    const rowTpl        = $('qb-row-tpl');
    const trackEl       = $('qb-track');
    const toastEl       = $('qb-toast');
    const toastNumEl    = $('qb-toast-num');
    const toastNameEl   = $('qb-toast-name');
    const toastDeltaEl  = $('qb-toast-delta');
    const toastUnitEl   = $('qb-toast-unit');
    const toastPlusEl   = toastEl.querySelector('.qb-toast-plus');
    const noLinkEl      = $('qb-nolink');

    // The rows on screen: horse -> { el, name, bets, displayed, target, raf,
    // nameText }, built from the model's field (renderField). A horse out
    // of the field has no entry, and its record goes with its row, so a
    // horse that comes back (an unscratch) snaps to its count rather than
    // tweening from a value nobody saw.
    const rows = {};
    let field = [];        // the horses with rows, in numeric order
    let fieldKey = '';     // field.join(','): the row set is rebuilt when it changes

    function makeRow(n) {
        const el = rowTpl.content.firstElementChild.cloneNode(true);
        el.dataset.horse = String(n);
        const saddle = el.querySelector('.qb-saddle');
        const colors = SADDLE[n] || SADDLE_FALLBACK;
        saddle.style.background = colors.bg;
        saddle.style.color = colors.fg;
        saddle.textContent = String(n);
        el.addEventListener('animationend', (e) => {
            if (e.target === el) el.classList.remove('is-pulse');
        });
        return {
            el,
            name: el.querySelector('.qb-name'),
            bets: el.querySelector('.qb-bets'),
            displayed: null, target: null, raf: null,
            nameText: null,
        };
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
    let frozen = false;        // rows/pot/prizes held at last values
    let renderedOnce = false;  // rows have been painted at least once
    let es = null;
    let reconnectTimer = null;
    let backoffMs = BACKOFF_MIN_MS;
    let lastMessageAt = performance.now();

    // ---- Slideshow hook ---------------------------------------------
    function slideshow() {
        return window.ddmSlideshow || null;
    }

    function show() {
        if (visible) return;
        visible = true;
        board.classList.add('is-visible');
        board.setAttribute('aria-hidden', 'false');
        flushCrawl();              // a swap during the fade-in is invisible
        const s = slideshow();
        if (s && typeof s.hold === 'function') s.hold();
    }

    function hide() {
        if (!visible) return;
        visible = false;
        board.classList.remove('is-visible');
        board.setAttribute('aria-hidden', 'true');
        dismissToast();
        flushCrawl();
        const s = slideshow();
        if (s && typeof s.release === 'function') s.release();
    }

    // ---- Model handling ---------------------------------------------
    function handleModel(m) {
        if (!m || typeof m !== 'object' || !m.horses) return;
        const first = model === null;
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

        // Rows, pot and prizes: live unless frozen, and the freeze only
        // holds for updates while the board is up. A hidden board always
        // takes the live model: otherwise the last hidden paint (the empty
        // model a restarted server hands out before the gateway reports,
        // or a page loading mid-race) would become the frozen picture.
        frozen = !!spec.frozen;
        if (!frozen || !renderedOnce || !visible) {
            const animate = visible && renderedOnce;
            renderPot(m);
            renderPrizes(m);
            renderRows(m, animate);
            renderedOnce = true;
        }
        renderNames(m);            // live in every state
        renderCloses(m, state);

        if (inBoard) show(); else hide();

        renderChyron(m);           // after show/hide: a running crawl swaps at its loop boundary
        maybeToast(m, first);
        updateNoLink();
    }

    function renderBanner(spec) {
        for (const c of BANNER_CLASSES) bannerEl.classList.remove(c);
        bannerEl.classList.add(spec.cls);
        setText(bannerTextEl, spec.text);
    }

    function setText(el, text) {
        if (el.textContent !== text) el.textContent = text;
    }

    // "$154" while the token is a whole-dollar amount, else "$4.50".
    function formatMoney(amount, tokenValue) {
        const a = Number(amount) || 0;
        const tv = Number(tokenValue);
        if (Number.isFinite(tv) && Number.isInteger(tv)) return '$' + Math.round(a);
        return '$' + a.toFixed(2);
    }

    function renderPot(m) {
        setText(potEl, formatMoney(m.pot, m.token_value));
        setText(tokenValueEl, formatMoney(m.token_value == null ? 1 : m.token_value, m.token_value) + ' a token');
    }

    // Whole dollars, from pi5 (place and show rounded half up, win takes
    // the rest so the three always sum to the pot). "$0" until pi5 says.
    function renderPrizes(m) {
        const p = (m.prizes && typeof m.prizes === 'object') ? m.prizes : {};
        for (const k of ['win', 'place', 'show']) {
            const v = Number(p[k]);
            setText(prizeEls[k], '$' + (Number.isFinite(v) ? Math.round(v) : 0));
        }
    }

    function horseName(h, n) {
        const raw = h && h.name != null ? String(h.name).trim() : '';
        return (raw || ('Horse ' + n)).toUpperCase();
    }

    // Names are written only when they change, and re-fitted then.
    function renderNames(m) {
        for (const n of field) {
            const r = rows[n];
            if (!r) continue;
            const text = horseName(m.horses[String(n)], n);
            if (text === r.nameText) continue;
            r.nameText = text;
            r.name.textContent = text;
            fitName(r.name);
        }
    }

    // The reference's fit: start at 38 px and step down until the text
    // fits its cell, but never below 20 px. No wrap, no ellipsis.
    function fitName(el) {
        let fs = NAME_MAX_PX;
        el.style.fontSize = fs + 'px';
        while (el.scrollWidth > el.clientWidth && fs > NAME_MIN_PX) {
            fs--;
            el.style.fontSize = fs + 'px';
        }
    }

    function refitNames() {
        for (const n of field) if (rows[n]) fitName(rows[n].name);
    }

    // The field: every horse with in_field true, in numeric order. A horse
    // without the key (the relay's empty model before pi5 is heard, or an
    // older pi5) is in the field when it is 1-20 and not scratched.
    function fieldOf(m) {
        const out = [];
        for (let n = 1; n <= HORSES; n++) {
            const h = m.horses[String(n)];
            if (!h || typeof h !== 'object') continue;
            const inField = h.in_field != null ? !!h.in_field : (n <= FIELD_MAX && !h.scratched);
            if (inField) out.push(n);
        }
        return out;
    }

    // The row set follows the field: rows for horses that left it are
    // removed (record and all), horses new to it get a row, and every row
    // is (re)appended in order, the first ten left and the rest right.
    // appendChild moves a row that is already there, so nothing is ever
    // duplicated and a field that only shifted (9 gone, 22 in) reflows
    // without any motion of its own.
    function renderField(m) {
        const next = fieldOf(m);
        const key = next.join(',');
        if (key === fieldKey) return;
        fieldKey = key;
        field = next;
        const keep = new Set(next);
        for (const k of Object.keys(rows)) {
            const n = Number(k);
            if (keep.has(n)) continue;
            cancelTween(rows[n]);
            rows[n].el.remove();
            delete rows[n];
        }
        next.forEach((n, i) => {
            if (!rows[n]) rows[n] = makeRow(n);
            colEls[i < SLOTS_PER_COL ? 0 : 1].appendChild(rows[n].el);
        });
    }

    function renderRows(m, animate) {
        renderField(m);
        // Leader: most tokens among the horses in the field, only when
        // somebody has bet; the lowest number on a tie.
        let leader = null;
        let most = 0;
        for (const n of field) {
            const h = m.horses[String(n)] || {};
            const tokens = parseInt(h.tokens, 10) || 0;
            if (tokens > most) { most = tokens; leader = n; }
        }
        for (const n of field) {
            const r = rows[n];
            const h = m.horses[String(n)] || {};
            const tokens = Math.max(0, parseInt(h.tokens, 10) || 0);
            r.el.classList.toggle('is-offline', h.cup != null && !h.online);
            r.el.classList.toggle('is-leader', leader === n);
            setCount(r, tokens, animate);
        }
    }

    // Writes the bets cell. Animated: tween from the value on screen and
    // pulse the row once. Not animated (first paint, a row new to the
    // field, or board hidden): snap.
    function setCount(r, tokens, animate) {
        if (r.target === tokens) return;
        r.target = tokens;
        if (!animate || r.displayed == null) {
            cancelTween(r);
            paintCount(r, tokens);
            return;
        }
        tween(r, r.displayed, tokens);
        pulse(r.el);
    }

    function paintCount(r, v) {
        r.displayed = v;
        r.el.classList.toggle('is-empty', v === 0);
        setText(r.bets, v === 0 ? 'No bets' : String(v));
    }

    function cancelTween(r) {
        if (r.raf) { cancelAnimationFrame(r.raf); r.raf = null; }
    }

    function tween(r, from, to) {
        cancelTween(r);
        const start = performance.now();
        const step = (now) => {
            const p = Math.min(1, (now - start) / COUNT_TWEEN_MS);
            const eased = 1 - Math.pow(1 - p, 3);
            const v = Math.round(from + (to - from) * eased);
            if (v !== r.displayed) paintCount(r, v);
            r.raf = p < 1 ? requestAnimationFrame(step) : null;
        };
        r.raf = requestAnimationFrame(step);
    }

    function pulse(el) {
        el.classList.remove('is-pulse');
        // eslint-disable-next-line no-unused-expressions
        el.offsetWidth;   // restart the one-shot animation if it is mid-flight
        el.classList.add('is-pulse');
    }

    // ---- CLOSES IN --------------------------------------------------
    // The countdown runs on pi5's clock: closes_at against the model's
    // "now", both stamped by pi5, so the TV's own clock never matters. It
    // ticks locally between messages. pi5 stamps "now" when it serialises,
    // but the splash re-serves the last message it received, so a model
    // can arrive with a stale stamp (a page load on a quiet board, or the
    // relay's link_ok flip). A clock only moves forward: the server-time
    // estimate is the newest stamp seen plus the time since, and an older
    // stamp never re-anchors it. With no stamp at all the TV's clock
    // stands in. Hidden without a closes_at, and in states 3+.
    const NOW_SLACK_S = 2;        // a stamp this little behind the estimate is fresh (latency)
    let closesAtS = null;         // the model's closes_at, pi5 time
    let serverNowS = null;        // the newest "now" seen ...
    let serverAnchor = 0;         // ... and the performance.now() it arrived at
    let closesHiddenByState = false;

    function serverNow() {
        if (serverNowS == null) return Date.now() / 1000;
        return serverNowS + (performance.now() - serverAnchor) / 1000;
    }

    function renderCloses(m, state) {
        const now = Number(m.now);
        if (m.now != null && Number.isFinite(now) && (serverNowS == null || now > serverNow() - NOW_SLACK_S)) {
            serverNowS = now;
            serverAnchor = performance.now();
        }
        const ca = Number(m.closes_at);
        closesAtS = (m.closes_at != null && Number.isFinite(ca)) ? ca : null;
        closesHiddenByState = Number.isFinite(state) && state >= 3;
        tickCloses();
    }

    function tickCloses() {
        const on = closesAtS != null && !closesHiddenByState;
        closesEl.classList.toggle('is-visible', on);
        if (!on) return;
        const remaining = closesAtS - serverNow();
        const secs = Math.max(0, Math.ceil(remaining));
        const mm = Math.floor(secs / 60);
        const ss = secs % 60;
        setText(closesTimeEl, mm + ':' + (ss < 10 ? '0' : '') + ss);
        setText(closesLabelEl, secs === 0 ? 'Closing' : 'Closes in');
    }

    // ---- Toast ------------------------------------------------------
    // Events are newest first and keyed by horse:ts. A message's new keys
    // are the ones the previous message did not carry; the newest positive
    // one gets the toast. Nothing on the first model after load, on a
    // poll/stream duplicate, while the board is hidden, or while the
    // picture is frozen (betting closed). Nor for a horse scratched at the
    // gateway: pi5 still reports the cup's token deltas, but those tokens
    // are out of the pot and the horse is out of the field, so a token
    // dropped in that cup is not a bet and the board must not announce
    // one. Its key is still consumed, so an unscratch later does not toast
    // it. A renumber (9 -> 22) produces no event on pi5, so nothing here.
    let seenKeys = new Set();
    let toastHoldTimer = null;
    let toastOutTimer = null;

    function eventKey(ev) {
        return String(ev.horse) + ':' + String(ev.ts);
    }

    function maybeToast(m, first) {
        const events = Array.isArray(m.events) ? m.events : [];
        const keys = new Set();
        let pick = null;
        for (const ev of events) {
            if (!ev || typeof ev !== 'object') continue;
            const key = eventKey(ev);
            keys.add(key);
            const h = m.horses && m.horses[String(ev.horse)];
            if (h && h.scratched) continue;
            if (!pick && !seenKeys.has(key) && (parseInt(ev.delta, 10) || 0) > 0) pick = ev;
        }
        seenKeys = keys;
        if (first || !visible || frozen || !pick) return;
        showToast(m, pick);
    }

    // The card names the horse by the number the event carries, which is
    // its current program number (after 9 -> 22 pi5 reports 22), in that
    // number's cloth, with the name straight from the model.
    function showToast(m, ev) {
        const n = parseInt(ev.horse, 10);
        const delta = parseInt(ev.delta, 10) || 0;
        const c = SADDLE[n] || SADDLE_FALLBACK;

        toastEl.style.background = c.bg;
        toastEl.style.color = c.fg;
        toastEl.style.boxShadow = '0 0 0 4px ' + c.bg + ', 0 24px 60px #000d';
        toastNumEl.style.background = c.fg;
        toastNumEl.style.color = c.bg;
        toastNumEl.textContent = String(n);
        toastNameEl.textContent = horseName(m.horses && m.horses[String(n)], n);
        fitToastName();
        toastDeltaEl.textContent = '+' + delta;
        toastUnitEl.textContent = delta === 1 ? 'Bet' : 'Bets';
        // The "+1" is white with a dark shadow, except on the light cloths
        // (white, yellow, turquoise, lime, sky, khaki, peach) where it takes
        // the cloth's own text colour.
        toastPlusEl.style.color = relativeLuminance(c.bg) > 0.4 ? c.fg : '#FFFFFF';

        clearTimeout(toastHoldTimer);
        clearTimeout(toastOutTimer);
        toastEl.classList.remove('is-leaving');
        toastEl.classList.add('is-shown');
        toastEl.setAttribute('aria-hidden', 'false');
        // The hold is counted from the end of the pop, so the card is fully
        // up for TOAST_HOLD_MS (in + hold + out on screen in all).
        toastHoldTimer = setTimeout(() => {
            toastHoldTimer = null;
            toastEl.classList.remove('is-shown');
            toastEl.classList.add('is-leaving');
            toastOutTimer = setTimeout(() => {
                toastOutTimer = null;
                toastEl.classList.remove('is-leaving');
                toastEl.setAttribute('aria-hidden', 'true');
            }, TOAST_OUT_MS);
        }, TOAST_IN_MS + TOAST_HOLD_MS);
    }

    // The card is capped to the screen (CSS max-width) and the name is the
    // flexible cell: same fit as the rows, 64 px stepping down to 36 px, and
    // past that the name clips rather than pushing the number block or the
    // "+1 BET" off the screen. The toast is laid out while hidden
    // (visibility, not display), so the widths are real.
    function fitToastName() {
        let fs = TOAST_NAME_MAX_PX;
        toastNameEl.style.fontSize = fs + 'px';
        while (toastNameEl.scrollWidth > toastNameEl.clientWidth && fs > TOAST_NAME_MIN_PX) {
            fs--;
            toastNameEl.style.fontSize = fs + 'px';
        }
    }

    function dismissToast() {
        clearTimeout(toastHoldTimer);
        clearTimeout(toastOutTimer);
        toastHoldTimer = toastOutTimer = null;
        toastEl.classList.remove('is-shown', 'is-leaving');
        toastEl.setAttribute('aria-hidden', 'true');
    }

    // ---- Chyron -----------------------------------------------------
    // Content, in order: chyron[0]; SCRATCHED and every scratch when there
    // are any (a replacement: the scratched horse's badge and name struck
    // through, the arrow, the replacement's badge and name; no replacement:
    // badge, name and a muted "· TOKENS REFUNDED"; an unnamed horse prints
    // HORSE n); the remaining chyron lines. Gold diamonds between items.
    // The track holds the content repeated (an even number of copies, at
    // least two, enough to cover the crawl twice) and translates 0 -> -50 %
    // so the loop is seamless; the duration comes from the track's width
    // at CRAWL_PX_S. A rebuild while the crawl runs waits for the loop
    // boundary (animationiteration) so the text never jumps.
    const SEP = '<span class="qb-crawl-sep">◆</span>';
    let crawlKey = null;
    let crawlPending = null;
    let crawlRunning = false;

    function esc(s) {
        return String(s).replace(/[&<>"']/g, (ch) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
        ));
    }

    function renderChyron(m) {
        const lines = Array.isArray(m.chyron) ? m.chyron.map((l) => String(l)) : [];
        const scratches = Array.isArray(m.scratches) ? m.scratches : [];
        const key = JSON.stringify([lines, scratches, m.names_rev == null ? null : m.names_rev]);
        if (key === crawlKey) return;
        crawlKey = key;
        const html = buildCrawl(lines, scratches);
        if (crawlRunning && visible) crawlPending = html;   // swap at the loop boundary
        else applyCrawl(html);
    }

    // One side of a scratch record, {number, name} -> {n, name}, or null
    // for anything else (the older string-shaped entries, junk).
    function scratchSide(x) {
        if (!x || typeof x !== 'object') return null;
        const n = parseInt(x.number, 10);
        if (!Number.isFinite(n)) return null;
        return { n, name: horseName(x, n) };
    }

    function crawlBadge(n) {
        const c = SADDLE[n] || SADDLE_FALLBACK;
        return '<span class="qb-crawl-badge" style="background:' + c.bg + ';color:' + c.fg + '">' + n + '</span>';
    }

    function buildCrawl(lines, scratches) {
        const item = (inner) => '<span class="qb-crawl-item">' + inner + '</span>';
        const items = [];
        if (lines.length) items.push(item(esc(lines[0])));
        const parts = [];
        for (const x of scratches) {
            const was = scratchSide(x && x.was);
            if (!was) continue;                            // not the record shape: ignored
            if (x.now == null) {
                parts.push(crawlBadge(was.n)
                    + '<span>' + esc(was.name) + '</span>'
                    + '<span class="qb-crawl-note">· Tokens refunded</span>');
                continue;
            }
            const now = scratchSide(x.now);
            if (!now) continue;
            parts.push(crawlBadge(was.n)
                + '<span class="qb-crawl-was">' + esc(was.name) + '</span>'
                + '<span class="qb-crawl-arrow">▶</span>'
                + crawlBadge(now.n)
                + '<span>' + esc(now.name) + '</span>');
        }
        if (parts.length) {
            items.push(item('<span class="qb-crawl-lbl">Scratched</span>' + parts.join('<span class="qb-crawl-gap"></span>')));
        }
        for (const l of lines.slice(1)) items.push(item(esc(l)));
        return items.join(SEP);
    }

    function applyCrawl(html) {
        crawlPending = null;
        if (!html) {
            trackEl.innerHTML = '';
            trackEl.style.animation = 'none';
            crawlRunning = false;
            return;
        }
        const one = html + SEP;
        trackEl.style.animation = 'none';          // stop, so the restart below begins at 0
        trackEl.innerHTML = one;
        const oneWidth = trackEl.offsetWidth;      // layout: one copy's width
        const viewWidth = trackEl.parentElement ? trackEl.parentElement.clientWidth : 0;
        const reps = Math.max(1, Math.ceil(viewWidth / Math.max(1, oneWidth)));
        trackEl.innerHTML = one.repeat(2 * reps);  // half the track = reps copies
        const half = trackEl.offsetWidth / 2;
        const seconds = Math.max(1, half / CRAWL_PX_S);
        trackEl.style.animation = '';              // back to the stylesheet's animation, from 0
        trackEl.style.animationDuration = seconds.toFixed(2) + 's';
        crawlRunning = true;
    }

    function flushCrawl() {
        if (crawlPending != null) applyCrawl(crawlPending);
    }

    trackEl.addEventListener('animationiteration', flushCrawl);

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
    setInterval(tickCloses, CLOSES_TICK_MS);
    // Anton arrives late where Impact is missing: the names' widths change.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(refitNames);
})();
