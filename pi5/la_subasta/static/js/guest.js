/* =============================================================================
   La Subasta — Guest JS (Phase 2A)

   Responsibilities:
     - Identity modal on first visit; persist to localStorage
     - Render horse list from /la-subasta/api/horses
     - Place bids with +1 / +3 / +5 / custom; disable out-of-range buttons
     - Live updates via SocketIO (bid_placed, horse_scratched, auction_locked,
       settings_changed)
     - Countdown strip (refreshes every 30s)

   Out of scope for Phase 2A (coming in 2B): portfolio view, undo toast,
   outbid banner, push notifications.
   ============================================================================= */

(function () {
    'use strict';

    const API = {
        state:    '/la-subasta/api/state',
        horses:   '/la-subasta/api/horses',
        register: '/la-subasta/api/register',
        bid:      '/la-subasta/api/bid',
        settings: '/la-subasta/api/admin/settings',
    };

    const IDENTITY_KEY   = 'la_subasta_identity';
    const ONBOARDED_KEY  = 'la_subasta_onboarded';
    const SPLASH_DURATION_MS = 2000;

    // Runtime state
    const state = {
        identity: null,          // { bidder_id, name, emoji, identity }
        horses:   {},            // keyed by horse_id
        socket:   null,
        settings: {              // cached — refreshed on settings_changed
            MAX_RAISE: 5,
            MIN_BID: 1,
        },
        auctionState: 'NOT_STARTED',
        countdownInterval: null,
        settingsRefreshedAt: 0,
    };

    // ---------------------------------------------------------------------
    // Fetch helpers
    // ---------------------------------------------------------------------

    async function getJSON(url) {
        const resp = await fetch(url, { credentials: 'same-origin' });
        const data = await resp.json().catch(() => ({}));
        return { ok: resp.ok, status: resp.status, data };
    }

    async function postJSON(url, body) {
        const resp = await fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        const data = await resp.json().catch(() => ({}));
        return { ok: resp.ok, status: resp.status, data };
    }

    // 2027 funny-money formatter — singular "1 peso", plural elsewhere
    function pesos(n) {
        return n === 1 ? '1 peso' : n + ' pesos';
    }

    // ---------------------------------------------------------------------
    // Identity (localStorage + modal)
    // ---------------------------------------------------------------------

    function loadStoredIdentity() {
        try {
            const raw = localStorage.getItem(IDENTITY_KEY);
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            if (parsed && parsed.bidder_id && parsed.name && parsed.emoji) {
                return parsed;
            }
        } catch (e) {
            // Corrupt entry — fall through to registration flow
        }
        return null;
    }

    function storeIdentity(identity) {
        localStorage.setItem(IDENTITY_KEY, JSON.stringify(identity));
    }

    function setupIdentityModal() {
        const modal = document.getElementById('identity-modal');
        const nameInput = document.getElementById('ls-name-input');
        const submitBtn = document.getElementById('ls-submit-btn');
        const errorEl = document.getElementById('ls-modal-error');
        const grid = document.getElementById('ls-emoji-grid');

        let selectedEmoji = null;

        function refreshSubmitState() {
            const name = nameInput.value.trim();
            submitBtn.disabled = !(name && selectedEmoji);
        }

        nameInput.addEventListener('input', refreshSubmitState);

        grid.addEventListener('click', function (e) {
            const btn = e.target.closest('.ls-emoji-btn');
            if (!btn) return;
            grid.querySelectorAll('.ls-emoji-btn').forEach(function (el) {
                el.setAttribute('aria-checked', 'false');
            });
            btn.setAttribute('aria-checked', 'true');
            selectedEmoji = btn.dataset.emoji;
            refreshSubmitState();
        });

        submitBtn.addEventListener('click', async function () {
            errorEl.hidden = true;
            const name = nameInput.value.trim();
            if (!name || !selectedEmoji) return;

            submitBtn.disabled = true;
            submitBtn.textContent = 'Registering…';

            const resp = await postJSON(API.register, {
                name: name,
                emoji: selectedEmoji,
            });

            if (resp.ok && resp.data.success) {
                const bidder = resp.data.bidder;
                state.identity = {
                    bidder_id: bidder.id,
                    name: bidder.name,
                    emoji: bidder.emoji,
                    identity: bidder.identity,
                };
                storeIdentity(state.identity);
                modal.hidden = true;
                submitBtn.textContent = 'Enter La Subasta';
                bootApp();
                return;
            }

            submitBtn.textContent = 'Enter La Subasta';
            submitBtn.disabled = false;

            if (resp.status === 409) {
                errorEl.textContent = 'That name + emoji combo is taken. Pick another emoji.';
                errorEl.hidden = false;
                grid.classList.remove('ls-highlight-grid');
                // Force reflow so the animation re-runs if fired twice
                void grid.offsetWidth;
                grid.classList.add('ls-highlight-grid');
            } else {
                errorEl.textContent = (resp.data && resp.data.error) || 'Something went wrong — try again.';
                errorEl.hidden = false;
            }
        });

        modal.hidden = false;
    }

    // ---------------------------------------------------------------------
    // App boot
    // ---------------------------------------------------------------------

    function bootApp() {
        const app = document.getElementById('ls-app');
        app.hidden = false;
        renderIdentityBadge();
        initSocket();
        // All three reads run in parallel, then one final render + countdown
        // start so button-enabled state reflects the fully-loaded snapshot
        // (auctionState, settings, and horses all in).
        Promise.all([refreshSettings(), refreshHorses(), refreshState()])
            .then(function () {
                renderHorseList();
                renderIdentityTotal();
                startCountdown();
            });
    }

    function renderIdentityBadge() {
        const display = document.getElementById('ls-identity-display');
        display.textContent = state.identity.identity;
    }

    // ---------------------------------------------------------------------
    // Settings
    // ---------------------------------------------------------------------

    async function refreshSettings() {
        const resp = await getJSON(API.settings);
        if (!resp.ok || !resp.data.settings) return;
        state.settings.MAX_RAISE = resp.data.settings.MAX_RAISE.value;
        state.settings.MIN_BID   = resp.data.settings.MIN_BID.value;
        state.settingsRefreshedAt = Date.now();
        // Re-render to update button disabled states
        renderHorseList();
    }

    // ---------------------------------------------------------------------
    // Horses
    // ---------------------------------------------------------------------

    async function refreshHorses() {
        const resp = await getJSON(API.horses);
        if (!resp.ok) return;
        state.horses = {};
        (resp.data.horses || []).forEach(function (h) {
            state.horses[h.horse_id] = h;
        });
        renderHorseList();
        renderIdentityTotal();
    }

    async function refreshState() {
        const resp = await getJSON(API.state);
        if (resp.ok) {
            state.auctionState = resp.data.state;
            updateLockedBanner();
            // Buttons depend on auctionState — re-render so they toggle
            // enabled when we land on OPEN / FINAL_HOUR.
            renderHorseList();
            tickCountdown();
        }
    }

    function renderHorseList() {
        const list = document.getElementById('ls-horse-list');
        const horses = Object.values(state.horses)
            .sort(function (a, b) { return a.horse_id - b.horse_id; });

        if (horses.length === 0) {
            list.innerHTML = '<div class="ls-placeholder">Waiting for horses…</div>';
            return;
        }

        // Cheap DOM diff: if every card already exists, update in place;
        // otherwise rebuild the list.
        const existing = list.querySelectorAll('.ls-horse-card');
        if (existing.length !== horses.length) {
            list.innerHTML = '';
            horses.forEach(function (h) { list.appendChild(buildHorseCard(h)); });
        } else {
            horses.forEach(function (h, i) {
                updateHorseCard(existing[i], h);
            });
        }
    }

    function buildHorseCard(horse) {
        const card = document.createElement('div');
        card.className = 'ls-horse-card';
        card.dataset.horseId = horse.horse_id;
        card.innerHTML = `
            <div class="ls-card-top">
                <div class="ls-saddle-cloth" data-role="saddle-cloth"></div>
                <div class="ls-horse-info">
                    <div class="ls-horse-name" data-role="name"></div>
                    <div class="ls-jockey" data-role="jockey"></div>
                    <div class="ls-leader-row" data-role="leader"></div>
                </div>
                <div class="ls-bid-block">
                    <div class="ls-bid-amount" data-role="bid-amount"></div>
                    <div class="ls-bid-empty" data-role="bid-empty" hidden>No bids yet</div>
                </div>
            </div>
            <div class="ls-bid-row" data-role="bid-row">
                <button type="button" class="ls-bid-btn" data-delta="1"></button>
                <button type="button" class="ls-bid-btn" data-delta="3"></button>
                <button type="button" class="ls-bid-btn" data-delta="5"></button>
                <button type="button" class="ls-bid-btn ls-custom" data-custom="1">Custom</button>
            </div>
            <div class="ls-bid-error" data-role="error" hidden></div>
        `;
        card.querySelector('[data-role="bid-row"]').addEventListener('click', function (e) {
            const btn = e.target.closest('.ls-bid-btn');
            if (!btn || btn.disabled) return;
            if (btn.dataset.custom) {
                openCustomBidModal(horse.horse_id);
            } else {
                placeQuickBid(horse.horse_id, parseInt(btn.dataset.delta, 10), btn);
            }
        });
        updateHorseCard(card, horse);
        return card;
    }

    function updateHorseCard(card, horse) {
        card.dataset.horseId = horse.horse_id;
        const saddle = card.querySelector('[data-role="saddle-cloth"]');
        saddle.textContent = horse.saddle_cloth;
        if (horse.saddle_cloth_color) {
            saddle.style.setProperty('--ls-saddle-color', horse.saddle_cloth_color);
        }

        card.querySelector('[data-role="name"]').textContent =
            horse.name || ('Horse #' + horse.saddle_cloth);
        card.querySelector('[data-role="jockey"]').textContent =
            horse.jockey ? ('Jockey: ' + horse.jockey) : '';

        const leader = card.querySelector('[data-role="leader"]');
        const bidAmountEl = card.querySelector('[data-role="bid-amount"]');
        const bidEmptyEl  = card.querySelector('[data-role="bid-empty"]');
        const currentAmt  = horse.current_high_bid ? horse.current_high_bid.amount : 0;

        leader.classList.remove('ls-you-leading', 'ls-no-bids');
        if (horse.current_high_bid) {
            bidAmountEl.textContent = pesos(currentAmt);
            bidAmountEl.hidden = false;
            bidEmptyEl.hidden = true;

            const isYou = state.identity &&
                          horse.current_leader_bidder_id === state.identity.bidder_id;
            if (isYou) {
                leader.innerHTML = '👑 You\u2019re leading';
                leader.classList.add('ls-you-leading');
            } else {
                leader.textContent = horse.current_leader_identity + ' leading';
            }
        } else {
            bidAmountEl.hidden = true;
            bidEmptyEl.hidden = false;
            bidEmptyEl.textContent = 'No bids yet — open at ' + pesos(state.settings.MIN_BID);
            leader.textContent = 'No bids yet — open at ' + pesos(state.settings.MIN_BID);
            leader.classList.add('ls-no-bids');
        }

        // Scratched overrides everything
        card.classList.toggle('ls-scratched', !!horse.scratched);
        let scratchedBadge = card.querySelector('.ls-scratched-badge');
        if (horse.scratched) {
            if (!scratchedBadge) {
                scratchedBadge = document.createElement('div');
                scratchedBadge.className = 'ls-scratched-badge';
                scratchedBadge.textContent = 'Scratched';
                card.querySelector('.ls-horse-info').appendChild(scratchedBadge);
            }
        } else if (scratchedBadge) {
            scratchedBadge.remove();
        }

        updateBidButtons(card, horse);
    }

    function updateBidButtons(card, horse) {
        const currentAmt = horse.current_high_bid ? horse.current_high_bid.amount : 0;
        const minBid = state.settings.MIN_BID;
        const maxRaise = state.settings.MAX_RAISE;
        const isYou = state.identity &&
                      horse.current_leader_bidder_id === state.identity.bidder_id;
        const biddable = (state.auctionState === 'OPEN' ||
                          state.auctionState === 'FINAL_HOUR') &&
                         !horse.scratched && !isYou;

        card.querySelectorAll('.ls-bid-btn').forEach(function (btn) {
            if (btn.dataset.custom) {
                btn.disabled = !biddable;
                return;
            }
            const delta = parseInt(btn.dataset.delta, 10);
            const nextAmt = currentAmt > 0 ? currentAmt + delta : minBid + (delta - 1);
            // For opening bids, delta N maps to minBid (for +1) up to
            // minBid + MAX_RAISE. We only show +1/+3/+5, so any delta > MAX_RAISE is disabled.
            let disabled = !biddable;
            if (currentAmt > 0) {
                if (delta > maxRaise) disabled = true;
            } else {
                // Opening: delta N means the bid is minBid + (N-1). +1 opens at minBid.
                const openingAmount = minBid + (delta - 1);
                if (openingAmount > minBid + maxRaise) disabled = true;
            }
            btn.disabled = disabled;
            // Raise buttons show the delta and the resulting total in pesos
            // ("+3 = 6 pesos"); opening buttons read "Bid N" (N = first-bid
            // amount). Surrounding card copy makes the pesos unit unambiguous.
            btn.textContent = currentAmt > 0
                ? ('+' + delta + ' = ' + pesos(nextAmt))
                : ('Bid ' + (minBid + (delta - 1)));
        });
    }

    // ---------------------------------------------------------------------
    // Bid placement
    // ---------------------------------------------------------------------

    function computeBidAmount(horse, delta) {
        const current = horse.current_high_bid ? horse.current_high_bid.amount : 0;
        if (current > 0) return current + delta;
        // Opening: +1 → minBid, +3 → minBid+2, +5 → minBid+4
        return state.settings.MIN_BID + (delta - 1);
    }

    async function placeQuickBid(horseId, delta, btn) {
        const horse = state.horses[horseId];
        if (!horse) return;
        const amount = computeBidAmount(horse, delta);
        await placeBid(horseId, amount, btn);
    }

    async function placeBid(horseId, amount, btn) {
        const card = document.querySelector('.ls-horse-card[data-horse-id="' + horseId + '"]');
        const errEl = card ? card.querySelector('[data-role="error"]') : null;
        if (errEl) errEl.hidden = true;

        if (btn) btn.classList.add('ls-loading');

        const resp = await postJSON(API.bid, {
            bidder_id: state.identity.bidder_id,
            horse_id:  horseId,
            amount:    amount,
        });

        if (btn) btn.classList.remove('ls-loading');

        if (resp.ok && resp.data.success) {
            // Optimistic update — the socketio broadcast will confirm
            const bid = resp.data.bid;
            state.horses[horseId].current_high_bid = {
                amount: bid.amount,
                bidder_id: bid.bidder_id,
                bidder_identity: bid.bidder_identity,
                bid_time: bid.bid_time,
            };
            state.horses[horseId].current_leader_identity = bid.bidder_identity;
            state.horses[horseId].current_leader_bidder_id = bid.bidder_id;
            renderHorseList();
            renderIdentityTotal();
            flashCard(horseId);
            vibrate();
            return true;
        }

        const msg = (resp.data && resp.data.error) || 'Bid failed — try again.';
        if (errEl) {
            errEl.textContent = msg;
            errEl.hidden = false;
            setTimeout(function () { errEl.hidden = true; }, 5000);
        }
        return false;
    }

    function flashCard(horseId) {
        const card = document.querySelector('.ls-horse-card[data-horse-id="' + horseId + '"]');
        if (!card) return;
        card.classList.remove('ls-flash-green');
        void card.offsetWidth;
        card.classList.add('ls-flash-green');
    }

    function vibrate() {
        if (navigator.vibrate) {
            try { navigator.vibrate(50); } catch (e) { /* ignore */ }
        }
    }

    // ---------------------------------------------------------------------
    // Custom bid modal
    // ---------------------------------------------------------------------

    function openCustomBidModal(horseId) {
        const horse = state.horses[horseId];
        if (!horse) return;

        const modal = document.getElementById('ls-custom-bid-modal');
        const horseEl = document.getElementById('ls-custom-horse');
        const input = document.getElementById('ls-custom-amount');
        const hint = document.getElementById('ls-custom-hint');
        const err = document.getElementById('ls-custom-error');
        const submit = document.getElementById('ls-custom-submit');
        const cancel = document.getElementById('ls-custom-cancel');

        const current = horse.current_high_bid ? horse.current_high_bid.amount : 0;
        const minBid = state.settings.MIN_BID;
        const maxRaise = state.settings.MAX_RAISE;
        const minAllowed = current > 0 ? current + 1 : minBid;
        const maxAllowed = current > 0 ? current + maxRaise : minBid + maxRaise;

        horseEl.textContent = '#' + horse.saddle_cloth + ' ' + (horse.name || '');
        input.min = minAllowed;
        input.max = maxAllowed;
        input.value = minAllowed;
        hint.textContent = 'Enter ' + minAllowed + '–' + maxAllowed + ' pesos';
        err.hidden = true;
        modal.hidden = false;
        setTimeout(function () { input.focus(); input.select(); }, 50);

        function cleanup() {
            modal.hidden = true;
            submit.removeEventListener('click', onSubmit);
            cancel.removeEventListener('click', onCancel);
        }

        async function onSubmit() {
            const val = parseInt(input.value, 10);
            if (isNaN(val) || val < minAllowed || val > maxAllowed) {
                err.textContent = 'Amount must be ' + minAllowed + '–' + maxAllowed + ' pesos';
                err.hidden = false;
                return;
            }
            submit.disabled = true;
            const ok = await placeBid(horseId, val, null);
            submit.disabled = false;
            if (ok) cleanup();
            else {
                err.textContent = 'Bid rejected — check the horse card';
                err.hidden = false;
            }
        }

        function onCancel() { cleanup(); }

        submit.addEventListener('click', onSubmit);
        cancel.addEventListener('click', onCancel);
    }

    // ---------------------------------------------------------------------
    // Identity total (sum of currently-leading bids)
    // ---------------------------------------------------------------------

    function renderIdentityTotal() {
        const totalEl = document.getElementById('ls-identity-total');
        if (!totalEl || !state.identity) return;
        let total = 0;
        Object.values(state.horses).forEach(function (h) {
            if (h.current_leader_bidder_id === state.identity.bidder_id &&
                !h.scratched) {
                total += h.current_high_bid.amount;
            }
        });
        // HTML wraps the span as "Bid <span>N</span> pesos", so emit a bare number
        totalEl.textContent = total;
    }

    // ---------------------------------------------------------------------
    // Countdown
    // ---------------------------------------------------------------------

    function startCountdown() {
        if (state.countdownInterval) clearInterval(state.countdownInterval);
        tickCountdown();
        state.countdownInterval = setInterval(tickCountdown, 30000);
    }

    function tickCountdown() {
        const text = document.getElementById('ls-countdown-text');
        const strip = document.getElementById('ls-countdown');
        if (!text) return;

        // Phase 2A: no real lockdown clock yet on the backend. Display a
        // state-driven message instead. Phase 2B will wire the real T-15 timer.
        const s = state.auctionState;
        if (s === 'NOT_STARTED') {
            text.textContent = 'Auction not yet open';
            strip.classList.remove('ls-countdown-urgent');
        } else if (s === 'OPEN') {
            text.textContent = 'Bidding is OPEN';
            strip.classList.remove('ls-countdown-urgent');
        } else if (s === 'FINAL_HOUR') {
            text.textContent = 'FINAL HOUR — get your bids in';
            strip.classList.add('ls-countdown-urgent');
        } else if (s === 'LOCKED' || s === 'RACE_COMPLETE' || s === 'SETTLED') {
            text.textContent = 'Bidding closed';
            strip.classList.remove('ls-countdown-urgent');
        } else {
            text.textContent = s;
        }
    }

    // ---------------------------------------------------------------------
    // SocketIO
    // ---------------------------------------------------------------------

    function initSocket() {
        if (!window.io) return;
        const socket = io();
        state.socket = socket;

        socket.on('bid_placed', function (payload) {
            const h = state.horses[payload.horse_id];
            if (!h) return;
            h.current_high_bid = {
                amount: payload.amount,
                bidder_id: payload.bidder_id,
                bidder_identity: payload.bidder_identity,
            };
            h.current_leader_identity = payload.bidder_identity;
            h.current_leader_bidder_id = payload.bidder_id;
            renderHorseList();
            renderIdentityTotal();
            flashCard(payload.horse_id);
        });

        socket.on('horse_scratched', function (payload) {
            const h = state.horses[payload.horse_id];
            if (!h) return;
            h.scratched = true;
            renderHorseList();
            renderIdentityTotal();
        });

        socket.on('auction_locked', function () {
            state.auctionState = 'LOCKED';
            updateLockedBanner();
            tickCountdown();
            renderHorseList();
        });

        // Every admin-driven state transition fires this. Guest UI uses
        // it to flip bid-button enabled state live (e.g. admin hits Start →
        // every guest's buttons enable without a refresh).
        socket.on('auction_state_changed', function (payload) {
            if (!payload || !payload.new_state) return;
            state.auctionState = payload.new_state;
            updateLockedBanner();
            tickCountdown();
            renderHorseList();
        });

        socket.on('settings_changed', function () {
            refreshSettings();
        });

        socket.on('bid_voided', function (payload) {
            const h = state.horses[payload.horse_id];
            if (!h) return;
            if (payload.new_high_bid) {
                h.current_high_bid = {
                    amount: payload.new_high_bid.amount,
                    bidder_id: payload.new_high_bid.bidder_id,
                    bidder_identity: payload.new_high_bid.identity,
                };
                h.current_leader_identity = payload.new_high_bid.identity;
                h.current_leader_bidder_id = payload.new_high_bid.bidder_id;
            } else {
                h.current_high_bid = null;
                h.current_leader_identity = null;
                h.current_leader_bidder_id = null;
            }
            renderHorseList();
            renderIdentityTotal();
        });
    }

    function updateLockedBanner() {
        const banner = document.getElementById('ls-locked-banner');
        if (!banner) return;
        banner.hidden = state.auctionState !== 'LOCKED' &&
                        state.auctionState !== 'RACE_COMPLETE' &&
                        state.auctionState !== 'SETTLED';
    }

    // ---------------------------------------------------------------------
    // Onboarding (splash + how-it-works) + persistent help button
    // ---------------------------------------------------------------------

    function isOnboarded() {
        try {
            return localStorage.getItem(ONBOARDED_KEY) === 'true';
        } catch (e) {
            return false;
        }
    }

    function markOnboarded() {
        try { localStorage.setItem(ONBOARDED_KEY, 'true'); } catch (e) { /* ignore */ }
    }

    function showSplash() {
        const splash = document.getElementById('ls-splash');
        if (!splash) return;
        splash.hidden = false;

        let dismissed = false;
        const dismiss = function () {
            if (dismissed) return;
            dismissed = true;
            splash.hidden = true;
            splash.removeEventListener('click', dismiss);
            showHowItWorksFirstVisit();
        };

        splash.addEventListener('click', dismiss);
        setTimeout(dismiss, SPLASH_DURATION_MS);
    }

    function showHowItWorksFirstVisit() {
        showHowItWorks(false);
    }

    function showHowItWorksModal() {
        showHowItWorks(true);
    }

    // asModal=false → first-visit screen, no X. ¡VAMOS! marks onboarded
    //                 and advances to identity flow.
    // asModal=true  → help modal, X visible. Both X and ¡VAMOS! just close.
    function showHowItWorks(asModal) {
        const elem = document.getElementById('ls-howitworks');
        const closeBtn = document.getElementById('ls-hiw-close');
        const scrollEl = elem.querySelector('.ls-hiw-scroll');
        if (!elem) return;
        closeBtn.hidden = !asModal;
        elem.classList.toggle('ls-howitworks-modal', !!asModal);
        elem.hidden = false;
        if (scrollEl) scrollEl.scrollTop = 0;
        state.howItWorksModalMode = !!asModal;
    }

    function closeHowItWorks() {
        const elem = document.getElementById('ls-howitworks');
        if (!elem) return;
        elem.hidden = true;
        if (state.howItWorksModalMode) {
            // Modal mode → user is already in the app, scroll position
            // in the horse list is preserved automatically (the overlay
            // doesn't move the underlying scroll).
            return;
        }
        // First-visit mode → mark onboarded, then continue to identity
        markOnboarded();
        proceedToIdentityFlow();
    }

    function setupHowItWorksHandlers() {
        const closeBtn = document.getElementById('ls-hiw-close');
        const vamosBtn = document.getElementById('ls-hiw-vamos');
        if (closeBtn) closeBtn.addEventListener('click', closeHowItWorks);
        if (vamosBtn) vamosBtn.addEventListener('click', closeHowItWorks);
    }

    function setupHelpButton() {
        const helpBtn = document.getElementById('ls-help-btn');
        if (!helpBtn) return;
        helpBtn.addEventListener('click', showHowItWorksModal);
    }

    function proceedToIdentityFlow() {
        const stored = loadStoredIdentity();
        if (stored) {
            state.identity = stored;
            bootApp();
        } else {
            setupIdentityModal();
        }
    }

    // ---------------------------------------------------------------------
    // Boot
    // ---------------------------------------------------------------------

    document.addEventListener('DOMContentLoaded', function () {
        setupHowItWorksHandlers();
        setupHelpButton();

        if (!isOnboarded()) {
            showSplash();
        } else {
            proceedToIdentityFlow();
        }
    });
})();
