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
   pi5's set is 1-5: the board keeps the TV through WINNER, when the prizes
   are needed, and hands it back in 0 (PRE_RACE) and 6 (AFTER_PARTY).

   A lost link never hides the board: it stays up with its last data and
   a small NO LINK mark appears after 10 s without any SSE message (data
   or ping), or as soon as the model itself reports link_ok false.

   Freeze rule: once betting is closed (3 AT_THE_POST, 4 RUNNING, 5
   WINNER) the rows (the set and the counts), pot and prizes show the
   figures at the post and the CLOSES IN line is hidden; the banner stays
   live, and so do the names and the chyron (a late name correction still
   shows). pi5 holds those figures (the model's closing: taken when
   betting closed, kept through the draw and a restart of pi5), so every
   screen shows the same numbers whenever it was loaded: a TV reloaded
   after the cups were emptied for the draw, a second screen, a phone.
   They are painted when they arrive and again only if pi5 takes them
   again; the live counts underneath (the cups being emptied) change
   nothing here. A model without closing (an older pi5) keeps the page's
   own freeze: the values it showed when betting closed. Back in 1 or 2
   the live model renders again. Either freeze only holds while the board
   is up: a hidden board always paints, so a page that loads mid-race, or
   a board coming back after the server restarted, never shows an earlier
   hidden paint of an empty model.

   Results: in WINNER the banner reads OFFICIAL RESULTS COMING over the
   frozen betting board until the model carries its results (results:
   {win, place, show}, three different horses; null until the dashboard
   has them). The moment it does, the stage crossfades to the results
   screen, the banner reads OFFICIAL RESULTS, and the three rows show the
   horse's cloth and name, the bets its cup held and its prize. The bets
   and the prizes are the figures at the post: by then the cups are being
   emptied for the draw, and what counts is what they held when betting
   closed.

   The model (GET /api/quiniela, relayed from pi5 untouched). The board
   reads: race_state, board_states, link_ok, token_value, pot, horses[n]
   {tokens, in_field, scratched, online, cup, name, odds}, events[{horse,
   delta, ts}], and the additive keys now, closes_at, prizes{win,place,
   show}, chyron[], scratches[{was:{number,name}, now:{number,name}|null}],
   names_rev, results{win,place,show}|null, closing{pot, prizes,
   total_tokens, horses{n: {tokens}}, at}|null, hand_counted (pi5 says it
   while the pot and the prizes are the host's count of the cash box, which
   closing's pot and prizes then are: the pot wears a small HAND COUNTED
   tag), race{name, year, post_at,
   post_local, tz} (La Quiniela's race info, the only copy: the crawl's
   clock and time to post, and the slideshow's race slides) and
   weather{location, temp_f, condition}|null (the crawl). odds is the
   track's line for the roster slide, a string or null. cup is only tested for null
   (it is the MAC of the cup claiming the horse, never a number). Every
   new key is optional: before pi5 has been heard the
   model carries none of them and the board renders without errors
   (hidden, since board_states is empty). Without in_field (that empty
   model, or an older pi5) the field is horses 1-20 that are not
   scratched.

   Motion: count changes tween over ~500 ms (requestAnimationFrame writing
   the number) and pulse the row once via a 400 ms CSS animation; the toast
   is a 150 ms pop / 200 ms drop on transform and opacity; the chyron crawl
   is one CSS transform animation, except in "dots", where it is a fixed
   row of tiles that the message steps across (stepCrawl, from
   requestAnimationFrame timestamps); in "dots" a name too long for its
   row scrolls by one transform animation of its own. Everything else is
   transform/opacity only (see quiniela_board.css).

   Looks. The board's data-look (the server writes it: ?look= on the URL,
   else config.QUINIELA_LOOK, "dots" unless set) is "impact", "dots" or
   "numbers". In the tote looks the stylesheet sets the tote's fields
   (data-tote, and the crawl's text) in the dot-matrix face, one element a
   field as in "impact", and a text that is too wide is made to fit by
   stepping the dot pitch down (font-size = 8 x pitch, whole pitches only,
   so the dots stay on the pixel grid) where the Impact look steps the
   font size, the strip of tiles behind it a whole number of tiles. The
   rows of "dots" are the exception: each row is one strip of tiles that
   fills the room from the cloth to the row's right edge, the same pitch
   and the same number of tiles in every row (layoutStrips: the tiles a
   little wider or narrower than the pitch makes them, so that a whole
   number of them fills the room), the bets in its last tiles and the
   name in the rest but one; a name longer than that does not shrink, it
   scrolls (updateScroll). The crawl of "dots" is a row of tiles of its own
   at the crawl's pitch, by the same fill rule: the tiles stand still and
   the message steps across them (The crawl of "dots", below).

   The slideshow. slideshow.html loads this script whether the board is
   up or not, and window.ddmQuiniela is what the rest of the page reads
   from it: race() (the model's race, or null), now() (pi5's clock)
   for the countdown slide, and fillRoster(root) for the roster slide,
   which is these rows again: the field and the track's odds in the tote
   look's strip, whatever the board's look (see The roster slide below).
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
    const RESULT_NAME_MAX_PX  = 100;   // the results screen's names, likewise
    const RESULT_NAME_MIN_PX  = 44;
    const RESULT_PRIZE_MAX_PX = 150;   // ... and its prizes, should one ever need four figures
    const RESULT_PRIZE_MIN_PX = 80;
    const BANNER_MAX_PX  = 42;      // the banner's text shrinks from here ...
    const BANNER_MIN_PX  = 26;      // ... down to here until the sign fits its cell
    // The tote look: dot pitches in px (the face's em is 8 pitches, its
    // cell 6 wide). A name starts at the first and steps down to the second.
    const DOT_EM  = 8;
    const DOT_CELL = 6;
    const RESULT_NAME_PITCH  = [12, 4];
    const RESULT_COUNT_PITCH = [10, 5];
    const RESULT_PRIZE_PITCH = [18, 8];
    const TOTE_FACE = '56px "DDM Tote"';
    // Every character DDMTote.ttf draws (its cmap, less the every-bulb socket
    // at U+E000, which nothing prints): a lower-case letter and an accented
    // one are drawn as the plain capital, a curly quote and a dash as the
    // straight one. The tiles of the crawl take only these (toteChar); the
    // tests keep this list equal to the face's.
    const TOTE_CHARS = new Set([
        ' !"#$%&\'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ_`abcdefghijklmnopqrstuvwxyz|',
        '\u00a0°´·',
        'ÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÑÒÓÔÕÖ×ØÙÚÛÜÝ',
        'àáâãäåçèéêëìíîïñòóôõöøùúûüýÿ',
        '–—‘’“”•−▶◆'
    ].join(''));
    // The rows of "dots": one strip of tiles each (layoutStrips). The pitch
    // comes from the row's height: the tile, 8 pitches tall, clears it by
    // STRIP_CLEAR_PX above and below, whole pitches, never more than
    // STRIP_PITCH_MAX, the dotted names' size at their largest (a 56 px tile
    // in the 64 px row of the 1080-line board). The tiles fill the row's
    // room: as many as fit at the pitch's width (6 pitches), stretched to
    // fill it, or one more, squeezed, when each is still STRIP_TILE_MIN of
    // that width. A name longer than its area scrolls a whole tile at a
    // time at the crawl's speed (a tile every 335 ms in the 40.25 px tiles
    // of 1920 x 1080), its start held SCROLL_HOLD_START_MS, its end
    // SCROLL_HOLD_END_MS, then straight back to the start (updateScroll).
    const STRIP_PITCH_MAX = 7;
    const STRIP_PITCH_MIN = 3;
    const STRIP_CLEAR_PX  = 2;
    const STRIP_TILE_MIN  = 0.94;
    const SCROLL_HOLD_START_MS = 2000;
    const SCROLL_HOLD_END_MS   = 1000;
    const TOAST_IN_MS    = 150;     // the pop, matches .qb-toast.is-shown's transition
    const TOAST_HOLD_MS  = 2500;    // fully up for this long, then ...
    const TOAST_OUT_MS   = 200;     // ... the drop, matches .qb-toast.is-leaving
    const TOAST_NAME_MAX_PX = 64;   // the toast's name shrinks from here ...
    const TOAST_NAME_MIN_PX = 36;   // ... down to here, then clips
    // The tag by the POT figure when the pot is the host's hand count
    // (placeCounted): its text, the one that fits when that does not, and the
    // gap to the figure and to the logo, in px.
    const COUNTED_LONG   = 'HAND COUNTED';
    const COUNTED_SHORT  = 'COUNTED';
    const COUNTED_GAP_PX = 22;
    const COUNTED_LOGO_GAP_PX = 24;
    const CRAWL_PX_S     = 120;     // chyron speed (impact and numbers: a CSS transform animation)
    // The crawl of "dots" (stepCrawl): a fixed row of tiles, a character a
    // tile, the message stepping one tile left CRAWL_TILES_PER_SEC times a
    // second. 5 is the speed of the track the sign replaced, CRAWL_PX_S, in
    // tiles (5 tiles of 23.7 px at 1920 px are 119 px/s against its 120); 8
    // was tried on DevPi and was far too fast. ?crawl_tps= on the URL
    // overrides it, for tuning on the TV, from CRAWL_TPS_MIN to CRAWL_TPS_MAX.
    // The tile is the rows' at the crawl's own pitch, 4 px: 24 x 32 px before
    // the fill rule stretches it, the crawl's size in the tote look all along.
    // CRAWL_PAD_TILES blank tiles either side of the diamond between two
    // items, which is also the gap between the end of the message and its
    // start again (the track's 34 px each side, in tiles).
    const CRAWL_TILES_PER_SEC = 5;
    const CRAWL_TPS_MIN       = 0.25;
    const CRAWL_TPS_MAX       = 60;
    const CRAWL_PITCH_PX      = 4;
    const CRAWL_PAD_TILES     = 1;
    const CLOSES_TICK_MS = 250;     // the countdown is checked 4x a second, written once a second
    const LIVE_TICK_MS   = 1000;    // the crawl's clock and time to post, in place
    const POST_SECONDS_UNDER_S = 600;   // under ten minutes to post: minutes and seconds

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
    // at their last values. States not listed here (0, 6) normally hide
    // the board; if config ever puts one in board_states the banner falls
    // back to the state's name.
    const BANNERS = {
        1: { text: 'Betting open',   cls: 'qb-banner--open',   frozen: false },
        2: { text: 'Final call',     cls: 'qb-banner--final',  frozen: false },
        3: { text: 'Betting closed', cls: 'qb-banner--closed', frozen: true  },
        4: { text: 'Betting closed', cls: 'qb-banner--closed', frozen: true  },
        5: { text: 'Official results coming', cls: 'qb-banner--closed', frozen: true },
    };
    // The race is over and the results are in: the results screen. WINNER,
    // and AFTER_PARTY too should config ever keep the board up in it (the
    // results outlive the state; pi5's reset clears them).
    const RESULTS_BANNER = { text: 'Official results', cls: 'qb-banner--official', frozen: true };
    const RESULT_STATES  = [5, 6];
    const PLACES         = ['win', 'place', 'show'];
    const BANNER_CLASSES = ['qb-banner--open', 'qb-banner--final', 'qb-banner--closed', 'qb-banner--official'];

    // ---- DOM ---------------------------------------------------------
    const board = document.getElementById('quiniela-board');
    if (!board) return;                       // not the slideshow page
    const $ = (id) => document.getElementById(id);

    // ---- Look --------------------------------------------------------
    const LOOKS = ['impact', 'dots', 'numbers'];
    const look = LOOKS.includes(board.dataset.look) ? board.dataset.look : 'dots';
    board.dataset.look = look;
    const dottedNames   = look === 'dots';      // names in the dot-matrix face, the rows one strip each
    const dottedFigures = look !== 'impact';    // figures and the crawl
    const tileCrawl     = look === 'dots';      // the crawl is a row of tiles that stand still (stepCrawl)

    const potEl         = $('qb-pot');
    const countedEl     = $('qb-counted');
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
    const crawlEl       = trackEl.parentElement;    // .qb-crawl: the band's room
    const toastEl       = $('qb-toast');
    const toastNumEl    = $('qb-toast-num');
    const toastNameEl   = $('qb-toast-name');
    const toastDeltaEl  = $('qb-toast-delta');
    const toastUnitEl   = $('qb-toast-unit');
    const toastPlusEl   = toastEl.querySelector('.qb-toast-plus');
    const noLinkEl      = $('qb-nolink');
    const headerEl      = board.querySelector('.qb-header');
    const resultsEl     = $('qb-results');

    // The results screen's three rows: place -> { el, saddle, name, count,
    // unit, prize } and what each last showed (horse, nameText, prizeText).
    const resultRows = {};
    for (const place of PLACES) {
        const el = $('qb-result-' + place);
        resultRows[place] = {
            el,
            saddle: el.querySelector('.qb-result-saddle'),
            name:   el.querySelector('.qb-result-name'),
            bets:   el.querySelector('.qb-result-bets'),
            count:  el.querySelector('.qb-result-count'),
            unit:   el.querySelector('.qb-result-unit'),
            prize:  el.querySelector('.qb-result-prize'),
            horse: null, nameText: null, prizeText: null, countText: null,
        };
    }

    // The rows on screen: horse -> { el, name, bets, displayed, target, raf,
    // nameText, and in "dots" digits, need, scroll, scrollKey }, built from
    // the model's field (renderField). A horse out of the field has no
    // entry, and its record goes with its row, so a horse that comes back
    // (an unscratch) snaps to its count rather than tweening from a value
    // nobody saw.
    const rows = {};
    let field = [];        // the horses with rows, in numeric order
    let fieldKey = '';     // field.join(','): the row set is rebuilt when it changes
    // "dots": every row's strip, its dot pitch, its length in tiles and a
    // tile's width in px (the room / tiles), 0 until measured (layoutStrips)
    const boardStrip = { root: board, pitch: 0, tiles: 0, tile: 0 };

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
            // "dots": the bets' tiles, the tiles the name needs, its scroll
            digits: 1, need: 0, scroll: null, scrollKey: '',
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
    let shown = null;          // the model the rows, pot and prizes show (the figures at the post while frozen)
    let visible = false;       // board layer shown
    let frozen = false;        // betting is closed: rows/pot/prizes are the figures at the post
    let closingKey = null;     // the closing figures on the board (their JSON), null when they are not
    let countedWanted = false; // pi5 says the pot is the host's hand count
    let countedFaceReady = false;   // the face the tag is lettered in has loaded (without document.fonts: always)
    let countedOn = false;     // ... and its tag is up (wanted, and its face is there)
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
        layoutCrawl();
        crawlStart();
        syncScrolls();
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
        syncScrolls();             // a hidden board spends nothing on names nobody sees
        const s = slideshow();
        if (s && typeof s.release === 'function') s.release();
    }

    // ---- Model handling ---------------------------------------------
    function handleModel(m) {
        if (!m || typeof m !== 'object' || !m.horses) return;
        const first = model === null;
        model = m;

        const state = Number(m.race_state);
        const results = RESULT_STATES.includes(state) ? resultsOf(m) : null;
        const spec = results ? RESULTS_BANNER : (BANNERS[state] || {
            text: String(m.race_state_name || ('state ' + state)).replace(/_/g, ' '),
            cls: 'qb-banner--closed',
            frozen: false,
        });
        const inBoard = Array.isArray(m.board_states) && m.board_states.includes(state);

        // Banner always follows the state.
        renderBanner(spec);
        board.dataset.state = String(state);

        // Rows, pot and prizes: live while betting is open. Once it has
        // closed, the figures at the post: pi5's closing, painted when they
        // arrive (or pi5 takes them again), else the page's own freeze.
        // Either way a hidden board always paints: otherwise the last
        // hidden paint (the empty model a restarted server hands out before
        // the gateway reports, or a page loading mid-race) would become the
        // frozen picture.
        frozen = !!spec.frozen;
        const closing = frozen ? closingOf(m) : null;
        const key = closing ? JSON.stringify(closing) : null;
        if (closing) {
            if (key !== closingKey || !renderedOnce || !visible) paint(atTheClose(m, closing));
        } else if (!frozen || !renderedOnce || !visible) {
            paint(m);
        }
        closingKey = key;
        renderCounted(m.hand_counted === true);   // after the paint: the tag hangs off the figure just written (99 -> 100 moves it)
        renderNames(m);            // live in every state
        renderResults(m, results); // the results screen, or back to the rows
        renderCloses(m, state);

        if (inBoard) show(); else hide();

        renderChyron(m);           // after show/hide: a running crawl swaps at its loop boundary
        maybeToast(m, first);
        updateNoLink();
        if (rosterPending && rosterPending.isConnected) fillRoster(rosterPending);
    }

    // Rows, pot and prizes from `view`: the live model, or the live model
    // with the figures at the post in their place (atTheClose). Counts tween
    // only on a board that is up and was painted before.
    function paint(view) {
        const animate = visible && renderedOnce;
        renderPot(view);
        renderPrizes(view);
        renderRows(view, animate);
        shown = view;
        renderedOnce = true;
    }

    function renderBanner(spec) {
        for (const c of BANNER_CLASSES) bannerEl.classList.remove(c);
        bannerEl.classList.add(spec.cls);
        if (bannerTextEl.textContent === spec.text) return;
        bannerTextEl.textContent = spec.text;
        fitBanner();
    }

    // The banner is a sign in the header's right-hand cell. A text too long
    // for the cell at 42 px (OFFICIAL RESULTS COMING) shrinks step by step
    // until the sign fits; never wraps.
    function fitBanner() {
        const cols = getComputedStyle(headerEl).gridTemplateColumns.split(' ');
        const room = parseFloat(cols[cols.length - 1]) || 480;
        let fs = BANNER_MAX_PX;
        bannerEl.style.fontSize = fs + 'px';
        while (bannerEl.offsetWidth > room && fs > BANNER_MIN_PX) {
            fs--;
            bannerEl.style.fontSize = fs + 'px';
        }
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

    // The hand count's tag. The scales are estimates: once the host has
    // counted the cash box and entered it, pi5 says hand_counted and the pot
    // and the prizes above are that count, so the pot wears a small tag,
    // HAND COUNTED (COUNTED when the longer text does not fit between the
    // logo and the figure). The tag is out of the flow, hung to the left of
    // the POT figure at its middle (placeCounted): the figure, the prizes
    // and the header's columns stand exactly where they do without it. It
    // follows the model's hand_counted alone: pi5 says it only while the pot
    // shown is the count's (the frozen board and the results screen), never
    // for a live pot.
    //
    // It is lettered in Impact in every look: the board's own stack, Impact
    // where installed and Anton from static/fonts where not, which the
    // stylesheet gives it like any label. Anton is a web font, so the text
    // is laid out in the fallback until it has arrived; a width measured then
    // would pick the wrong text. The tag therefore stays down until the face
    // has loaded (countedFaceReady, once document.fonts.load() has settled for
    // the tag's own font), and is measured again whenever a font finishes
    // loading and whenever the window is resized.
    function renderCounted(on) {
        countedWanted = on;
        syncCounted();
    }

    function syncCounted() {
        if (!countedEl) return;
        const show = countedWanted && countedFaceReady;
        if (show !== countedOn) {
            countedOn = show;
            countedEl.classList.toggle('is-on', show);
            countedEl.setAttribute('aria-hidden', show ? 'false' : 'true');
        }
        placeCounted();
    }

    // The tag's face is ready: loaded, or not coming (a face that fails to
    // load is what the tag has to be set in, so it is shown all the same).
    function countedFaceArrived() {
        countedFaceReady = true;
        syncCounted();
    }

    function placeCounted() {
        if (!countedEl || !countedOn) return;
        const box = potEl.parentElement.getBoundingClientRect();         // .qb-pot
        const range = document.createRange();
        range.selectNodeContents(potEl);
        const fig = range.getBoundingClientRect();                       // the figure's own text
        if (!fig.width || !box.width) return;                            // not laid out yet: the next paint places it
        const logo = headerEl.querySelector('.qb-logo');
        const room = fig.left - COUNTED_GAP_PX - (logo ? logo.getBoundingClientRect().right + COUNTED_LOGO_GAP_PX : box.left);
        setText(countedEl, COUNTED_LONG);
        if (countedEl.offsetWidth > room) setText(countedEl, COUNTED_SHORT);
        countedEl.style.left = (fig.left - COUNTED_GAP_PX - box.left) + 'px';
        countedEl.style.top = ((fig.top + fig.bottom) / 2 - box.top) + 'px';
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
            fitName(r);
        }
    }

    // The reference's fit: start at 38 px and step down until the text
    // fits its cell, but never below 20 px. No wrap, no ellipsis. In "dots"
    // nothing shrinks: the name keeps the strip's pitch and scrolls when it
    // is longer than its area.
    function fitName(r) {
        if (dottedNames) measureName(r);
        else fitText(r.name, NAME_MAX_PX, NAME_MIN_PX);
    }

    // ---- The rows of "dots": one strip of tiles each -----------------
    // A set of rows shares one strip: the board's rows (boardStrip), and
    // the slideshow's roster slide, which is these rows again (fillRoster).
    // A set is {root, pitch, tiles, tile}. Every row's strip has the same
    // pitch and the same tiles, taken from one row's room (.qb-strip-cell:
    // from just right of the cloth to the row's right padding, which is the
    // gap after the cloth again): the pitch from its height; from its
    // width, N = as many tiles 6 pitches wide as it holds, or N + 1 when
    // that many are each still STRIP_TILE_MIN of 6 pitches, and the tile's
    // width the room / N, so the tiles fill the room edge to edge. The few
    // pixels a tile gains or loses are the tile's: its bulbs and a
    // character's dots keep the pitch, centred in it. They go on the set's
    // root as --qb-strip-pitch, --qb-strip-tiles and --qb-strip-tile-w,
    // which the stylesheet sizes every strip under it from. The room is
    // measured without transforms (computed style, offsetHeight), so a row
    // that is pulsing measures as it stands, and so does a slide mid-
    // transition. null while there is no row to measure.
    function stripFit(cell) {
        if (!cell || !cell.offsetWidth || !cell.offsetHeight) return null;
        const pitch = Math.max(STRIP_PITCH_MIN, Math.min(STRIP_PITCH_MAX,
            Math.floor((cell.offsetHeight - 2 * STRIP_CLEAR_PX) / DOT_EM)));
        const room = parseFloat(getComputedStyle(cell).width) || cell.offsetWidth;
        const fit = tileFit(room, pitch);
        return { pitch, tiles: fit.tiles, tile: fit.tile };
    }

    // The fill rule: N = as many tiles 6 pitches wide as a room holds, or N + 1
    // when that many are each still STRIP_TILE_MIN of 6 pitches, and the tile's
    // width the room / N. The rows use it (stripFit), and so does the crawl of
    // "dots" (layoutCrawl) at its own pitch.
    function tileFit(room, pitch) {
        const nominal = DOT_CELL * pitch;
        let tiles = Math.max(3, Math.floor(room / nominal));
        if (room / (tiles + 1) >= STRIP_TILE_MIN * nominal) tiles++;
        return { tiles, tile: room / tiles };
    }

    // Puts a fit on its set and the set's root. Returns whether it changed.
    function applyStrip(set, fit) {
        if (!fit || (fit.pitch === set.pitch && fit.tiles === set.tiles && fit.tile === set.tile)) return false;
        set.pitch = fit.pitch;
        set.tiles = fit.tiles;
        set.tile = fit.tile;
        set.root.style.setProperty('--qb-strip-pitch', fit.pitch + 'px');
        set.root.style.setProperty('--qb-strip-tiles', String(fit.tiles));
        set.root.style.setProperty('--qb-strip-tile-w', fit.tile + 'px');
        return true;
    }

    // How many tiles a row's name needs: its width in the face, in whole
    // tiles (the face is one tile a character, letter-spaced to the tile's
    // width; a character it lacks falls back to Impact, which the width
    // still counts).
    function stripNeed(set, r) {
        r.need = r.nameText ? Math.ceil(r.name.offsetWidth / set.tile - 0.05) : 0;
    }

    // The board's own set: every row of the betting board.
    function layoutStrips() {
        if (!dottedNames) return false;
        if (!applyStrip(boardStrip, stripFit(board.querySelector('.qb-rows .qb-strip-cell')))) return false;
        for (const n of field) if (rows[n]) measureName(rows[n]);
        return true;
    }

    function measureName(r) {
        if (!boardStrip.pitch) return;
        stripNeed(boardStrip, r);
        updateScroll(r);
    }

    // The bets take as many tiles as they have digits, the name's area the
    // rest but one dark tile, so a count of 10 takes a tile from the name.
    function setDigits(r, digits) {
        if (digits === r.digits) return;
        r.digits = digits;
        r.el.style.setProperty('--qb-digits', String(digits));
        updateScroll(r);
    }

    // A name longer than its area scrolls inside it, right to left, and
    // only it: the area clips it at its edges, the dark tile and the bets
    // never move. The start held SCROLL_HOLD_START_MS, then one whole tile
    // at a time at the crawl's speed until the name's last character is in
    // the area's last tile, that held SCROLL_HOLD_END_MS, then straight back
    // to the start, and again. Whole tiles, not a smooth slide: the tiles
    // and their unlit bulbs are the strip's and stay put, so a character
    // always sits in a tile as it does on the dashboard's ticker; a smooth
    // slide would drag the lit dots across the dark ones between them. One
    // Web Animation on the name's transform per row, run by the compositor,
    // each row on its own clock. A name that fits (again) stands still at
    // the start of its area. running false: made paused (a board that is
    // not showing its rows).
    function stripScroll(set, r, running) {
        const area = Math.max(1, set.tiles - r.digits - 1);
        const over = r.need - area;
        const key = over > 0 ? [over, set.tile, r.nameText].join('|') : '';
        if (key === r.scrollKey) return;
        r.scrollKey = key;
        if (r.scroll) { r.scroll.cancel(); r.scroll = null; }
        if (over <= 0 || typeof r.name.animate !== 'function') return;
        const tile = set.tile;
        const stepMs = tile / CRAWL_PX_S * 1000;
        const total = SCROLL_HOLD_START_MS + (over - 1) * stepMs + SCROLL_HOLD_END_MS;
        const at = (k) => 'translateX(' + (-k * tile) + 'px)';
        const frames = [{ offset: 0, transform: at(0), easing: 'step-end' }];
        for (let k = 1; k <= over; k++) {
            frames.push({ offset: (SCROLL_HOLD_START_MS + (k - 1) * stepMs) / total, transform: at(k), easing: 'step-end' });
        }
        frames.push({ offset: 1, transform: at(over) });
        r.scroll = r.name.animate(frames, { duration: total, iterations: Infinity });
        if (!running) r.scroll.pause();
    }

    function updateScroll(r) {
        if (!boardStrip.pitch) return;
        stripScroll(boardStrip, r, rowsShowing());
    }

    // The scrolls run only while the rows are on screen: not while the
    // board is hidden, nor while the results screen stands in their place.
    function rowsShowing() {
        return visible && board.dataset.view !== 'results';
    }

    function syncScrolls() {
        const on = rowsShowing();
        for (const n of field) {
            const r = rows[n];
            if (!r || !r.scroll) continue;
            if (on) r.scroll.play(); else r.scroll.pause();
        }
    }

    // ---- The roster slide ----------------------------------------------
    // The slideshow's "field and odds" slide (templates/splash/
    // horse_roster.html) is this board's rows again: the same row template,
    // cloths and names, the field in the same order (every horse in_field,
    // by number, a replacement under its own number: 21, 22...), in the tote
    // look's strip whatever the board's look (its container is a
    // .qb.qb--embed[data-look="dots"]), with the track's odds where the bets
    // sit: as many tiles as the odds have characters ("5-1" three, "50-1"
    // four), one dark tile before them, a dim dash for a horse with none.
    // The strip is measured on the slide once the face is there (the fill
    // rule, a long name scrolling a tile at a time), so every row of both
    // columns fits the panel. A slide filled before the first model has
    // arrived is filled when it does.
    const NO_ODDS = '—';
    let rosterPending = null;

    function faceReady() {
        return (document.fonts && document.fonts.load) ? document.fonts.load(TOTE_FACE, 'A0') : Promise.resolve();
    }

    function fillRoster(root) {
        if (!root) return false;
        if (!model) { rosterPending = root; return false; }
        if (rosterPending === root) rosterPending = null;
        const cols = root.querySelectorAll('[data-roster-col]');
        if (cols.length < 2) return false;
        for (const col of cols) col.textContent = '';
        const set = { root, pitch: 0, tiles: 0, tile: 0 };
        const recs = [];
        fieldOf(model).forEach((n, i) => {
            const r = makeRow(n);
            const h = model.horses[String(n)] || {};
            const odds = (h.odds != null && String(h.odds).trim()) ? String(h.odds).trim().toUpperCase() : null;
            r.nameText = horseName(h, n);
            r.name.textContent = r.nameText;
            r.bets.textContent = odds || NO_ODDS;
            r.digits = (odds || NO_ODDS).length;
            r.el.style.setProperty('--qb-digits', String(r.digits));
            r.el.classList.toggle('is-empty', !odds);
            cols[i < SLOTS_PER_COL ? 0 : 1].appendChild(r.el);
            recs.push(r);
        });
        const lay = () => {
            if (!root.isConnected) return;
            applyStrip(set, stripFit(root.querySelector('.qb-rows .qb-strip-cell')));
            if (!set.pitch) return;
            for (const r of recs) { stripNeed(set, r); stripScroll(set, r, true); }
        };
        faceReady().then(lay, lay);
        return true;
    }

    // What the slideshow reads: the race (its countdown slide, the roster's
    // post time), pi5's clock, and the roster filler. crawl() is a read-only
    // snapshot of the crawl of "dots" (null in the other looks), for tuning
    // on the TV and for the tests.
    window.ddmQuiniela = {
        race: () => (model && model.race && typeof model.race === 'object') ? model.race : null,
        now: () => serverNow(),
        fillRoster,
        crawl: crawlSnapshot,
    };

    function fitText(el, maxPx, minPx) {
        let fs = maxPx;
        el.style.fontSize = fs + 'px';
        while (el.scrollWidth > el.clientWidth && fs > minPx) {
            fs--;
            el.style.fontSize = fs + 'px';
        }
    }

    // The tote look's fit. The pitch steps down a whole pixel at a time
    // until the text fits `box` (the element itself unless given). Half a
    // pitch of grace: the last half pitch of a character's cell is its
    // margin, not dots, so a name may run that far past its cell.
    function fitDots(el, pitches, box) {
        const within = box || el;
        let p = pitches[0];
        el.style.fontSize = (DOT_EM * p) + 'px';
        while (within.scrollWidth > within.clientWidth + p / 2 && p > pitches[1]) {
            p--;
            el.style.fontSize = (DOT_EM * p) + 'px';
        }
        return p;
    }

    // ... and for a field that is a strip of tiles (a name, a prize): the
    // strip is as many whole tiles as its cell holds at that pitch, the
    // ones the text does not reach unlit. The cell is measured with the
    // strip's own width taken off, so a refit starts from the cell again.
    function fitTiles(el, pitches) {
        el.style.width = '';
        const cell = el.clientWidth;
        const p = fitDots(el, pitches);
        const tile = DOT_CELL * p;
        const tiles = Math.max(1, Math.floor((cell + p / 2) / tile));
        el.style.width = Math.min(cell, tiles * tile) + 'px';
        return p;
    }

    function fitResultName(el) {
        if (dottedNames) fitTiles(el, RESULT_NAME_PITCH);
        else fitText(el, RESULT_NAME_MAX_PX, RESULT_NAME_MIN_PX);
    }

    // The prize's strip hangs from the right of its cell: the cell is what
    // the strip may fill, so it is measured on the row, not on the strip.
    function fitResultPrize(r) {
        if (!dottedFigures) { fitText(r.prize, RESULT_PRIZE_MAX_PX, RESULT_PRIZE_MIN_PX); return; }
        r.prize.style.width = '100%';
        const cell = r.prize.clientWidth;
        let p = RESULT_PRIZE_PITCH[0];
        const chars = r.prize.textContent.length;
        while (chars * DOT_CELL * p > cell + p / 2 && p > RESULT_PRIZE_PITCH[1]) p--;
        const tile = DOT_CELL * p;
        r.prize.style.fontSize = (DOT_EM * p) + 'px';
        r.prize.style.width = Math.min(cell, Math.max(chars, Math.floor((cell + p / 2) / tile)) * tile) + 'px';
    }

    function fitResultCount(r) {
        if (dottedFigures) fitDots(r.count, RESULT_COUNT_PITCH, r.bets);
    }

    function refitNames() {
        for (const n of field) if (rows[n]) fitName(rows[n]);
        for (const place of PLACES) {
            const r = resultRows[place];
            if (r.nameText != null) fitResultName(r.name);
            if (r.prizeText != null) { fitResultPrize(r); fitResultCount(r); }
        }
    }

    // A face that arrives late changes every width: the names are fitted
    // again and the crawl is measured again (the track restarts from its
    // start; the tiles of "dots" do not depend on the face and keep their
    // message).
    function refitAll() {
        layoutStrips();
        layoutCrawl();
        refitNames();
        placeCounted();            // the dotted POT figure changed width: its tag follows
        if (crawlHtml != null) applyCrawl(crawlHtml);
    }

    // ---- The figures at the post -------------------------------------
    // The model's closing: {pot, prizes, total_tokens, horses{n: {tokens}},
    // at}, the live fields' shapes as they were when betting closed; null
    // while betting is open, or absent (an older pi5).
    function closingOf(m) {
        const c = m.closing;
        if (!c || typeof c !== 'object' || !c.horses || typeof c.horses !== 'object') return null;
        return c;
    }

    // The live model with the figures at the post in place of the live
    // ones: the pot, the prizes, the token count and every horse's tokens
    // are closing's; the field, the names and the cups stay the model's.
    function atTheClose(m, c) {
        const horses = {};
        for (const k of Object.keys(m.horses)) {
            const at = c.horses[k];
            const tokens = at && typeof at === 'object' ? Math.max(0, parseInt(at.tokens, 10) || 0) : 0;
            horses[k] = Object.assign({}, m.horses[k], { tokens: tokens });
        }
        return Object.assign({}, m, {
            pot: c.pot, prizes: c.prizes, total_tokens: c.total_tokens, horses: horses,
        });
    }

    // ---- Results ----------------------------------------------------
    // The model's results, {win, place, show}, when all three are in: three
    // different horses 1-24. Anything else (null until the dashboard has
    // them, a place still missing) is no results yet.
    function resultsOf(m) {
        const r = m.results;
        if (!r || typeof r !== 'object') return null;
        const out = {};
        const seen = new Set();
        for (const place of PLACES) {
            const n = Number(r[place]);
            if (!Number.isInteger(n) || n < 1 || n > HORSES || seen.has(n)) return null;
            seen.add(n);
            out[place] = n;
        }
        return out;
    }

    // The results screen. Who won and the names come from the live model;
    // the bets each cup held and the prizes from the picture on the board
    // (`shown`: the figures at the post, pi5's closing, or without them the
    // page's own freeze): the cups are emptied for the draw while this
    // screen is up, and the count that matters is the one they held. A
    // horse that was never in the field still gets its row (cloth, name,
    // 0 bets): the results say what they say.
    function renderResults(m, results) {
        const on = !!results;
        const view = on ? 'results' : 'rows';
        if (board.dataset.view !== view) {
            board.dataset.view = view;
            resultsEl.setAttribute('aria-hidden', on ? 'false' : 'true');
            syncScrolls();         // the rows' scrolls stop under the results screen
        }
        if (!on) return;
        const from = shown || m;
        const prizes = (from.prizes && typeof from.prizes === 'object') ? from.prizes : {};
        for (const place of PLACES) {
            const n = results[place];
            const r = resultRows[place];
            if (r.horse !== n) {
                r.horse = n;
                r.el.dataset.horse = String(n);
                const c = SADDLE[n] || SADDLE_FALLBACK;
                r.saddle.style.background = c.bg;
                r.saddle.style.color = c.fg;
                r.saddle.textContent = String(n);
            }
            const name = horseName(m.horses[String(n)], n);
            if (name !== r.nameText) {
                r.nameText = name;
                r.name.textContent = name;
                fitResultName(r.name);
            }
            const h = from.horses && from.horses[String(n)];
            const tokens = Math.max(0, parseInt(h && h.tokens, 10) || 0);
            const count = String(tokens);
            setText(r.unit, tokens === 1 ? 'Bet' : 'Bets');
            if (count !== r.countText) {
                r.countText = count;
                r.count.textContent = count;
                fitResultCount(r);
            }
            const v = Number(prizes[place]);
            const prize = '$' + (Number.isFinite(v) ? Math.round(v) : 0);
            if (prize !== r.prizeText) {
                r.prizeText = prize;
                r.prize.textContent = prize;
                fitResultPrize(r);
            }
        }
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
            if (rows[n].scroll) rows[n].scroll.cancel();
            rows[n].el.remove();
            delete rows[n];
        }
        next.forEach((n, i) => {
            if (!rows[n]) rows[n] = makeRow(n);
            colEls[i < SLOTS_PER_COL ? 0 : 1].appendChild(rows[n].el);
        });
        layoutStrips();            // "dots": the strips' pitch and length, once there is a row to measure
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

    // An empty cup reads NO BETS, or in "dots" a dim 0 in one tile; there
    // the count's digits are the bets' tiles, as the tween writes them.
    function paintCount(r, v) {
        r.displayed = v;
        r.el.classList.toggle('is-empty', v === 0);
        if (dottedNames) {
            setText(r.bets, String(v));
            setDigits(r, String(v).length);
        } else {
            setText(r.bets, v === 0 ? 'No bets' : String(v));
        }
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
    // stands in. Hidden without a closes_at, and in states 3+ (betting is
    // closed, the race is on or over).
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
    // Content, in order: chyron[0]; the live items: the time of day on the
    // race's clock (7:42 PM), the time to post (POST IN 1:14, hours and
    // minutes; under ten minutes POST IN 9:42, minutes and seconds; gone
    // once the post time has passed or while none is set) and pi5's weather
    // (DALLAS 88°F SUNNY; left out when there is none); SCRATCHED and every
    // scratch when there are any (a replacement: the scratched horse's
    // badge and name struck through, the arrow, the replacement's badge and
    // name; no replacement: badge, name and "· RE-BET YOUR TOKENS", as
    // bright as the name, since it tells the bettors what to do; an
    // unnamed horse prints HORSE n); the remaining chyron lines. Gold
    // diamonds between items. (The dots look says the scratches in words,
    // without cloths, struck names or arrows: tileMessage.)
    // The live items change in place, every second (tickLive), a text for
    // one of the same length: in the tote look every character is a tile,
    // so the track never changes width and the crawl never restarts. A
    // change of shape (an item coming or going, a text of another length:
    // 9:59 PM to 10:00 PM, new weather) is a rebuild like any other,
    // swapped in at the loop boundary.
    // The track holds the content repeated (an even number of copies, at
    // least two, enough to cover the crawl twice) and translates 0 -> -50 %
    // so the loop is seamless; the duration comes from the track's width
    // at CRAWL_PX_S. A rebuild while the crawl runs waits for the loop
    // boundary (animationiteration) so the text never jumps.
    // Every piece of text in the crawl is a .qb-crawl-text span, so the
    // tote look can set it in dots (and lay its tiles under it); the badges
    // are cloths and stay as they are.
    // "dots" has no track: it builds the same content, in the same order
    // with the same separators, as a message of cells for a row of tiles
    // (tileMessage, The crawl of "dots" below).
    const crawlText = (s, cls) => '<span class="qb-crawl-text' + (cls ? ' ' + cls : '') + '">' + esc(s) + '</span>';
    const SEP = crawlText('◆', 'qb-crawl-sep');
    let crawlKey = null;
    let crawlHtml = null;      // what the track shows, or is about to
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
        const live = liveItems(m);
        const shape = live.map(([k, t]) => (k === 'weather' ? k + ':' + t : k + ':' + t.length)).join('|');
        // The scratches as the crawl reads them, not as the JSON happened to order their keys: the model the relay
        // hands a page when it loads and pi5's stream carry the same record with its keys in two orders, and that
        // is no update.
        const said = scratches.map((x) => [scratchSide(x && x.was), x && x.now == null ? 'none' : scratchSide(x && x.now)]);
        const key = JSON.stringify([lines, said, m.names_rev == null ? null : m.names_rev, shape]);
        if (key === crawlKey) { updateLive(live); return; }
        crawlKey = key;
        if (tileCrawl) { setTileCrawl(tileMessage(lines, scratches, live)); return; }
        const html = buildCrawl(lines, scratches, live);
        crawlHtml = html;
        if (crawlRunning && visible) crawlPending = html;   // swap at the loop boundary
        else applyCrawl(html);
    }

    // The live items as [kind, text], in the crawl's order.
    function liveItems(m) {
        const race = (m && m.race && typeof m.race === 'object') ? m.race : null;
        const now = serverNow();
        const out = [];
        const time = clockText(race && race.tz, now);
        if (time) out.push(['time', time]);
        const post = postText(race, now);
        if (post) out.push(['post', post]);
        const weather = weatherText(m && m.weather);
        if (weather) out.push(['weather', weather]);
        return out;
    }

    function updateLive(live) {
        if (tileCrawl) { tileUpdateLive(live); return; }
        for (const [kind, text] of live) {
            if (kind === 'weather') continue;                 // part of the shape: never in place
            for (const el of trackEl.querySelectorAll('[data-live="' + kind + '"]')) {
                if (el.textContent !== text) el.textContent = text;
            }
        }
    }

    // "7:42 PM" on the race's clock (the zone pi5 names; this screen's own
    // without one). Assembled from its parts: the ICU the browser carries may
    // put a narrow no-break space before PM, which the face has no tile for.
    const clockFormats = {};
    function clockText(tz, epochS) {
        if (!Number.isFinite(epochS)) return null;
        const key = tz || '';
        if (!(key in clockFormats)) {
            let fmt = null;
            for (const zone of [tz || undefined, undefined]) {
                try { fmt = new Intl.DateTimeFormat('en-US', { timeZone: zone, hour: 'numeric', minute: '2-digit', hour12: true }); break; }
                catch (err) { fmt = null; }
            }
            clockFormats[key] = fmt;
        }
        const fmt = clockFormats[key];
        if (!fmt) return null;
        const parts = {};
        for (const p of fmt.formatToParts(new Date(epochS * 1000))) parts[p.type] = p.value;
        return (parts.hour && parts.minute) ? parts.hour + ':' + parts.minute + ' ' + String(parts.dayPeriod || '').toUpperCase() : null;
    }

    // "Post in 1:14" (hours:minutes), "Post in 9:42" under ten minutes
    // (minutes:seconds), null once the post time has passed or while none
    // is set. Counted on pi5's clock, like CLOSES IN.
    function postText(race, nowS) {
        if (!race || race.post_at == null) return null;
        const at = Number(race.post_at);
        if (!Number.isFinite(at) || !Number.isFinite(nowS)) return null;
        const secs = Math.ceil(at - nowS);
        if (secs <= 0) return null;
        const two = (v) => (v < 10 ? '0' : '') + v;
        if (secs < POST_SECONDS_UNDER_S) return 'Post in ' + Math.floor(secs / 60) + ':' + two(secs % 60);
        const mins = Math.floor(secs / 60);
        return 'Post in ' + Math.floor(mins / 60) + ':' + two(mins % 60);
    }

    // "Dallas 88°F Sunny": whatever pi5's weather has of the place, the
    // temperature and the sky; null without a temperature or a sky.
    function weatherText(w) {
        if (!w || typeof w !== 'object') return null;
        const t = Number(w.temp_f);
        const temp = (w.temp_f != null && Number.isFinite(t)) ? Math.round(t) + '°F' : null;
        const sky = (typeof w.condition === 'string' && w.condition.trim()) ? w.condition.trim() : null;
        if (!temp && !sky) return null;
        const place = (typeof w.location === 'string' && w.location.trim()) ? w.location.trim() : null;
        return [place, temp, sky].filter(Boolean).join(' ');
    }

    function tickLive() {
        if (model) renderChyron(model);
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

    function buildCrawl(lines, scratches, live) {
        const item = (inner) => '<span class="qb-crawl-item">' + inner + '</span>';
        const items = [];
        if (lines.length) items.push(item(crawlText(lines[0])));
        for (const [kind, text] of (live || [])) {
            items.push(item('<span class="qb-crawl-text qb-crawl-live" data-live="' + kind + '">' + esc(text) + '</span>'));
        }
        const parts = [];
        for (const x of scratches) {
            const was = scratchSide(x && x.was);
            if (!was) continue;                            // not the record shape: ignored
            if (x.now == null) {
                parts.push(crawlBadge(was.n)
                    + crawlText(was.name)
                    + crawlText('· RE-BET YOUR TOKENS', 'qb-crawl-note'));
                continue;
            }
            const now = scratchSide(x.now);
            if (!now) continue;
            parts.push(crawlBadge(was.n)
                + crawlText(was.name, 'qb-crawl-was')
                + crawlText('▶', 'qb-crawl-arrow')
                + crawlBadge(now.n)
                + crawlText(now.name));
        }
        if (parts.length) {
            items.push(item(crawlText('Scratched', 'qb-crawl-lbl') + parts.join('<span class="qb-crawl-gap"></span>')));
        }
        for (const l of lines.slice(1)) items.push(item(crawlText(l)));
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
        if (tileCrawl) { if (crawlSign.pending) crawlApply(crawlSign.pending); return; }
        if (crawlPending != null) applyCrawl(crawlPending);
    }

    trackEl.addEventListener('animationiteration', flushCrawl);

    // ---- The crawl of "dots": tiles that stand still -------------------
    // In "dots" the chyron is a dot-matrix sign. A row of tiles across the
    // band, built once, never moves; the message steps across it, a
    // character a tile. Tile i shows the loop's cell offset + i, and each
    // step offset goes up by one: the whole message is a tile to the left,
    // the next cell comes in at the right and the left tile's is gone. The
    // loop is the items, each followed by its gap (CRAWL_PAD_TILES blank
    // tiles, the diamond, as many blank again), so the end of the message
    // and its start again are one gap apart, and a message shorter than the
    // band shows itself more than once. A message that starts (the board
    // comes up, or a rebuild that did not wait) starts from blank tiles,
    // offset -tiles, and comes in from the right.
    //
    // The row is the rows' fill rule at the crawl's own pitch (tileFit): as
    // many 24 px tiles as the band holds, or one more at 94 % of that, each
    // the band's width / N, the bulbs at the pitch and the character
    // centred. layoutCrawl builds the tiles once and adds or takes some away
    // when the window changes size. A step writes the cells whose character
    // or style changed and nothing else; there is no transform and no
    // animation, so no frame ever has a tile anywhere but where it is. The
    // steps come from requestAnimationFrame timestamps (crawlFrame): step n
    // is due at n / crawlTps after the crawl started, so a late frame makes
    // the next one take two steps and the rate does not drift; a stall of
    // more than half a second is not made up for.
    //
    // The content is buildCrawl's, in capitals, a cell a character (one the
    // face lacks: toteChar), all of it the lit amber but SCRATCHED in red,
    // except that the scratches are said in words, a tile a character like
    // the rest: no cloths, no struck names, no arrows (tileMessage).
    //
    // A live item that keeps its length (the clock's minute, the countdown)
    // is written into the message where it stands, at once. Anything that
    // changes the message waits for the loop boundary (offset back at 0, the
    // message's start at the left edge), as the track's rebuild does: nothing
    // an update does starts the message again from its start, and while one
    // waits the live items are kept up to date in it too.
    const crawlRow  = { el: null, list: [], tile: 0 };       // the row, its tiles {el, node, c, s}, a tile's width
    const crawlSign = { msg: null, pending: null, offset: 0, generation: 0 };
    let crawlRaf = 0;          // the step loop's pending frame
    let crawlClock = null;     // {t0, steps}: step n is due at t0 + n / crawlTps ms
    const crawlTps = (() => {
        const v = Number(new URLSearchParams(location.search).get('crawl_tps'));
        return (Number.isFinite(v) && v > 0) ? Math.min(CRAWL_TPS_MAX, Math.max(CRAWL_TPS_MIN, v)) : CRAWL_TILES_PER_SEC;
    })();

    // A cell's look, by number: 0 is the plain amber one. Its classes, and
    // colours when a look has its own, go on the tile (paintTile); the
    // stylesheet does the rest (.qb-ct).
    const tileStyles = [{ cls: 'qb-ct', bg: '', fg: '' }];
    const tileStyleIds = new Map([['qb-ct||', 0]]);
    function tileStyle(cls, bg, fg) {
        const key = cls + '|' + (bg || '') + '|' + (fg || '');
        let id = tileStyleIds.get(key);
        if (id === undefined) {
            id = tileStyles.length;
            tileStyles.push({ cls, bg: bg || '', fg: fg || '' });
            tileStyleIds.set(key, id);
        }
        return id;
    }
    const TS_LABEL = tileStyle('qb-ct is-lbl');

    // What a character is on the tiles: itself in capitals when the face has
    // it, else its base letter (an accent is dropped, as the face itself
    // draws SEÑOR as SENOR), else a blank tile. null: no tile at all (a
    // combining mark, a zero-width character). The face has no [ ] { } ^ ~ \
    // and, outside Latin-1, only its dashes, quotes, bullet, minus, arrow and
    // diamond: those that are not in it, and every emoji, are blanks.
    function toteChar(ch) {
        if (/\s/.test(ch)) return ' ';
        if (/[\u0300-\u036f\u200b-\u200f\u2060\ufe0e\ufe0f]/.test(ch)) return null;
        const up = ch.toUpperCase();
        if (up.length === ch.length && TOTE_CHARS.has(up)) return up;
        if (TOTE_CHARS.has(ch)) return ch;
        const base = ch.normalize('NFD').charAt(0);
        if (base && base !== ch && TOTE_CHARS.has(base.toUpperCase())) return base.toUpperCase();
        return ' ';
    }

    // A string as cells: one character a tile, '' a blank tile; a run of
    // blanks is one and there are none at either end (the track's HTML
    // collapsed white space the same way).
    function tileChars(s) {
        const out = [];
        for (const ch of String(s)) {
            const c = toteChar(ch);
            if (c === null) continue;
            if (c === ' ') { if (out.length && out[out.length - 1] !== '') out.push(''); } else out.push(c);
        }
        if (out.length && out[out.length - 1] === '') out.pop();
        return out;
    }

    // The loop for a model: buildCrawl's items in the same order with the
    // same separators, but for the scratches (below), as {ch, st} cells (a
    // character, a look) the same length, `at` where each live item stands
    // (a kind -> {at, len, item}) and `items` the items as the track's DOM
    // would read them ("time:7:42 PM").
    function tileMessage(lines, scratches, live) {
        const ch = [];
        const st = [];
        const at = {};
        const items = [];
        let raw = '';              // what the item being built says
        const cells = (list, style) => { for (const c of list) { ch.push(c); st.push(style); } };
        const gap = (n) => cells(new Array(n).fill(''), 0);
        const words = (s, style) => { raw += s; cells(tileChars(s), style || 0); };
        const sep = () => { gap(CRAWL_PAD_TILES); cells(['◆'], 0); gap(CRAWL_PAD_TILES); };
        const item = (kind, build) => {
            if (items.length) sep();
            const from = ch.length;
            raw = '';
            build();
            if (kind) at[kind] = { at: from, len: ch.length - from, item: items.length };
            items.push(kind ? kind + ':' + raw : raw);
        };
        if (lines.length) item(null, () => words(lines[0]));
        for (const [kind, text] of (live || [])) item(kind, () => words(text));
        // The scratches in words, the lit amber like the rest. A replacement
        // is an item of its own: 9 THE PUMA SCRATCHED - 22 OCELLI DRAWS IN,
        // in the model's order (pi5's: by number). The same-day scratches
        // follow in one item under a red SCRATCHED, each its number, name and
        // · RE-BET YOUR TOKENS, a two-tile gap between them.
        const sameDay = [];
        for (const x of scratches) {
            const was = scratchSide(x && x.was);
            if (!was) continue;                            // not the record shape: ignored
            if (x.now == null) {
                sameDay.push(() => { words(String(was.n)); gap(1); words(was.name); gap(1); words('· RE-BET YOUR TOKENS'); });
                continue;
            }
            const now = scratchSide(x.now);
            if (!now) continue;
            item(null, () => words(was.n + ' ' + was.name + ' SCRATCHED - ' + now.n + ' ' + now.name + ' DRAWS IN'));
        }
        if (sameDay.length) {
            item(null, () => {
                words('Scratched', TS_LABEL);
                sameDay.forEach((part, i) => { gap(i ? 2 : 1); part(); });
            });
        }
        for (const l of lines.slice(1)) item(null, () => words(l));
        if (items.length) sep();                           // the loop's own gap, before its start comes round again
        return { ch, st, len: ch.length, at, items };
    }

    // A message: at once when nothing is on the sign yet or the board is
    // hidden (it starts from blank tiles), else at the loop boundary.
    function setTileCrawl(msg) {
        if (crawlSign.msg && crawlSign.msg.len && visible) crawlSign.pending = msg;
        else crawlApply(msg);
    }

    function crawlApply(msg) {
        crawlSign.pending = null;
        crawlSign.msg = msg;
        crawlSign.generation++;
        layoutCrawl();
        crawlSign.offset = msg.len ? -crawlRow.list.length : 0;
        drawCrawl();
        crawlStart();
    }

    // The live items that keep their length, written where they stand, in the
    // message on the sign and in the one waiting. A text that has not the
    // length the message was built with (the key says it has) is not
    // squeezed in: the message is rebuilt at the next tick.
    function tileUpdateLive(live) {
        let drawn = false;
        for (const [kind, text] of live) {
            if (kind === 'weather') continue;                // part of the shape: never in place
            const next = tileChars(text);
            for (const msg of [crawlSign.msg, crawlSign.pending]) {
                const w = msg && msg.at[kind];
                if (!w) continue;
                if (w.len !== next.length) { crawlKey = null; return; }
                for (let i = 0; i < w.len; i++) msg.ch[w.at + i] = next[i];
                msg.items[w.item] = kind + ':' + text;
                if (msg === crawlSign.msg) drawn = true;
            }
        }
        if (drawn) drawCrawl();
    }

    function makeTile() {
        const el = document.createElement('span');
        el.className = 'qb-ct';
        const node = document.createTextNode('');
        el.appendChild(node);
        return { el, node, c: '', s: 0 };
    }

    // The row of tiles, measured: built once, then only added to or taken
    // from when the band's width changes the fill rule's answer. Returns
    // whether it changed. Nothing while the band has no width to measure.
    function layoutCrawl() {
        if (!tileCrawl) return false;
        const room = parseFloat(getComputedStyle(crawlEl).width) || crawlEl.offsetWidth;
        if (!room) return false;
        const fit = tileFit(room, CRAWL_PITCH_PX);
        if (fit.tiles === crawlRow.list.length && fit.tile === crawlRow.tile) return false;
        if (!crawlRow.el) {
            crawlRow.el = document.createElement('div');
            crawlRow.el.className = 'qb-crawl-tiles';
            crawlRow.el.setAttribute('aria-hidden', 'true');
            crawlRow.el.style.setProperty('--qb-ct-pitch', CRAWL_PITCH_PX + 'px');
            crawlEl.appendChild(crawlRow.el);
        }
        crawlRow.tile = fit.tile;
        crawlRow.el.style.setProperty('--qb-ct-w', fit.tile + 'px');
        while (crawlRow.list.length < fit.tiles) {
            const t = makeTile();
            crawlRow.el.appendChild(t.el);
            crawlRow.list.push(t);
        }
        while (crawlRow.list.length > fit.tiles) crawlRow.el.removeChild(crawlRow.list.pop().el);
        drawCrawl();
        return true;
    }

    function paintTile(el, id) {
        const style = tileStyles[id];
        el.className = style.cls;
        el.style.backgroundColor = style.bg;
        el.style.color = style.fg;
    }

    // Each tile shows the loop's cell offset + i (blank before the message
    // starts); only a tile whose character or look changed is written.
    function drawCrawl() {
        const list = crawlRow.list;
        const msg = crawlSign.msg;
        const len = msg ? msg.len : 0;
        const offset = crawlSign.offset;
        for (let i = 0; i < list.length; i++) {
            const p = offset + i;
            let c = '';
            let s = 0;
            if (p >= 0 && len) {
                const k = p < len ? p : p % len;
                c = msg.ch[k];
                s = msg.st[k];
            }
            const t = list[i];
            if (t.c !== c) { t.c = c; t.node.data = c; }
            if (t.s !== s) { t.s = s; paintTile(t.el, s); }
        }
    }

    // One step: the message a tile to the left. The loop boundary is the
    // message's start reaching the left edge (offset back at 0, which is also
    // where a message that has just come in from the right first gets to): a
    // message that was waiting takes over there.
    function stepCrawl() {
        const msg = crawlSign.msg;
        if (!msg || !msg.len) return;
        crawlSign.offset++;
        if (crawlSign.offset >= msg.len) crawlSign.offset -= msg.len;
        if (crawlSign.offset === 0 && crawlSign.pending) {
            crawlSign.msg = crawlSign.pending;
            crawlSign.pending = null;
            crawlSign.generation++;
        }
    }

    // A frame that finds the board hidden, or nothing to show, does not ask for another: the loop ends by itself,
    // and a hidden board spends nothing on it. crawlStart (the board comes up, a message arrives) begins it again
    // on a clock of its own, so the time away is not made up for.
    function crawlFrame(now) {
        crawlRaf = 0;
        const msg = crawlSign.msg;
        if (!visible || !msg || !msg.len) return;
        crawlRaf = requestAnimationFrame(crawlFrame);
        const stepMs = 1000 / crawlTps;
        if (!crawlClock) { crawlClock = { t0: now, steps: 0 }; return; }
        const due = Math.floor((now - crawlClock.t0) / stepMs);
        let owed = due - crawlClock.steps;
        if (owed <= 0) return;
        if (owed > Math.max(2, Math.ceil(crawlTps / 2))) {       // a stall: carry on from here, do not catch up
            crawlClock.t0 = now - crawlClock.steps * stepMs;
            return;
        }
        crawlClock.steps = due;
        while (owed-- > 0) stepCrawl();
        drawCrawl();
    }

    function crawlStart() {
        if (!tileCrawl || crawlRaf || !visible || !crawlSign.msg || !crawlSign.msg.len) return;
        crawlClock = null;
        crawlRaf = requestAnimationFrame(crawlFrame);
    }

    // window.ddmQuiniela.crawl(): the sign as it stands.
    function crawlSnapshot() {
        if (!tileCrawl) return null;
        const msg = crawlSign.msg;
        return {
            tps: crawlTps, tiles: crawlRow.list.length, tile: crawlRow.tile,
            offset: crawlSign.offset, length: msg ? msg.len : 0, generation: crawlSign.generation,
            pending: crawlSign.pending !== null,
            items: msg ? msg.items.slice() : [],
            text: msg ? msg.ch.map((c) => c || ' ').join('') : '',
        };
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
    // The tag's face (see renderCounted): load whatever its own computed font
    // names, so that a page that is told "hand counted" on its first message
    // still measures the tag in the right face. Without document.fonts there
    // is nothing to wait for.
    if (countedEl && document.fonts && document.fonts.load) {
        const tagStyle = getComputedStyle(countedEl);
        document.fonts.load(tagStyle.font || (tagStyle.fontSize + ' ' + tagStyle.fontFamily), COUNTED_LONG)
            .then(countedFaceArrived, countedFaceArrived);
    } else {
        countedFaceReady = true;
    }
    layoutCrawl();
    fetchOnce();
    connect();
    setInterval(updateNoLink, 1000);
    setInterval(tickCloses, CLOSES_TICK_MS);
    setInterval(tickLive, LIVE_TICK_MS);
    // Anton arrives late where Impact is missing: the names' widths change,
    // and the tag is measured again. So does every dotted width when the tote
    // face arrives.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { refitNames(); placeCounted(); });
    if (document.fonts && document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', () => { placeCounted(); });
    // A window that changes size moves the POT figure, and its tag with it.
    window.addEventListener('resize', () => { placeCounted(); });
    // In "dots" a window that changes size (a kiosk settling into full
    // screen) measures the strips and the crawl's tiles again.
    if (dottedNames) {
        let resizeRaf = null;
        window.addEventListener('resize', () => {
            if (resizeRaf) return;
            resizeRaf = requestAnimationFrame(() => { resizeRaf = null; layoutStrips(); layoutCrawl(); });
        });
    }
    if (dottedFigures && document.fonts && document.fonts.load) {
        document.fonts.load(TOTE_FACE, '$0A').then(refitAll, () => {});
    }
})();
