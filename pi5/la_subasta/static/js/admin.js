// la_subasta/static/js/admin.js - the La Subasta admin page
//
// A small page over the /la-subasta/api routes; it holds no state of its own
// and reads everything again every few seconds and after each action.
//
//   - Auction: Open, Final hour, Lock. A lock with horses nobody bid on is a
//     409 {"unsold": [...]} (there is no House to take them): the warning
//     lists them and the confirm button locks anyway ({"confirm": true}).
//   - Bidders: what each owes, Mark paid, and the cap exemption ("No cap":
//     the host, who buys the horses nobody else bid on, may lead more than
//     the max-horses cap; it still applies to everyone else).
//   - Payouts: the ledger, derived on pi5 from the result SET WINNERS
//     recorded on the Control Center and the ownership as they stand (GET
//     /api/admin/payouts): nothing is entered here. A slot whose horse has no
//     owner is flagged and has a picker for the horse that pays it instead
//     (the next finisher). Each slot has a paid-out mark (Mark paid out /
//     Unmark); a mark the result moved away from is listed on top, in red,
//     as paid on an earlier result, until it is unmarked.
//
// Names are what guests typed: every piece of text goes in with textContent.
//
// The page and these routes are the host's, behind the PIN in pi5/.env
// (pi5/host_pin.py; host_pin.js sends a page whose PIN stopped working back to the form).

(function () {
    'use strict';

    const API = '/la-subasta/api/';
    const ADMIN = API + 'admin/';
    const POLL_MS = 4000;

    const model = { state: null, horses: [], bidders: [], ledger: null };
    const picks = {};          // a payout slot's picker choice, kept across refreshes
    const pickerOpen = {};     // slots whose picker was opened with "Change"

    function $(id) { return document.getElementById(id); }

    function pesos(n) {
        n = Number(n) || 0;
        const s = Number.isInteger(n) ? String(n) : n.toFixed(2);
        return s + (n === 1 ? ' peso' : ' pesos');
    }

    // ---------------------------------------------------------------------
    // Requests
    // ---------------------------------------------------------------------

    async function getJSON(url) {
        try {
            const resp = await fetch(url, { credentials: 'same-origin' });
            const data = await resp.json().catch(function () { return {}; });
            return { ok: resp.ok, status: resp.status, data: data };
        } catch (err) {
            return { ok: false, status: 0, data: {}, networkError: true };
        }
    }

    async function postJSON(url, body) {
        try {
            const resp = await fetch(url, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body || {}),
            });
            const data = await resp.json().catch(function () { return {}; });
            return { ok: resp.ok, status: resp.status, data: data };
        } catch (err) {
            return { ok: false, status: 0, data: {}, networkError: true };
        }
    }

    function say(msg, ok) {
        const el = $('ls-admin-status');
        el.textContent = msg || '';
        el.className = 'adm-status' + (msg ? (ok ? ' ok' : ' err') : '');
    }

    function errorOf(r, fallback) {
        if (r.networkError) return 'Could not reach pi5';
        return (r.data && r.data.error) || fallback;
    }

    // ---------------------------------------------------------------------
    // Reading
    // ---------------------------------------------------------------------

    let lastSeen = '';

    async function refresh() {
        const got = await Promise.all([
            getJSON(API + 'state'),
            getJSON(API + 'horses'),
            getJSON(API + 'bidders'),
            getJSON(ADMIN + 'payouts'),
        ]);
        if (got[0].ok) model.state = got[0].data;
        if (got[1].ok) model.horses = got[1].data.horses || [];
        if (got[2].ok) model.bidders = got[2].data.bidders || [];
        if (got[3].ok) model.ledger = got[3].data;
        // Draw only when something changed: a page rebuilt every few seconds
        // swallows a tap that lands while it is being rebuilt.
        const seen = JSON.stringify(model);
        if (seen === lastSeen) return;
        lastSeen = seen;
        render();
    }

    function horse(id) {
        for (let i = 0; i < model.horses.length; i++) {
            if (model.horses[i].horse_id === id) return model.horses[i];
        }
        return null;
    }

    function horseLabel(id) {
        const h = horse(id);
        return '#' + id + (h && h.name ? ' ' + h.name : '');
    }

    // ---------------------------------------------------------------------
    // Drawing
    // ---------------------------------------------------------------------

    function text(tag, cls, str) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        node.textContent = str;
        return node;
    }

    function button(label, cls, onClick) {
        const b = document.createElement('button');
        b.type = 'button';
        if (cls) b.className = cls;
        b.textContent = label;
        b.addEventListener('click', onClick);
        return b;
    }

    function render() {
        renderHead();
        renderControls();
        renderBidders();
        renderPayouts();
    }

    function renderHead() {
        const s = model.state || {};
        $('ls-admin-state').textContent = (s.state || '—') + (s.settled ? ' · results in' : '');
        $('ls-admin-pot').textContent = s.total_pot === undefined ? '—' : pesos(s.total_pot);
        $('ls-admin-bidders').textContent = s.num_bidders === undefined ? '—' : s.num_bidders;
        $('ls-admin-bids').textContent = s.num_bids === undefined ? '—' : s.num_bids;
    }

    function auctionState() { return model.state ? model.state.state : null; }

    function renderControls() {
        const s = auctionState();
        const biddable = s === 'OPEN' || s === 'FINAL_HOUR';
        $('ls-admin-open').disabled = s !== 'NOT_STARTED';
        $('ls-admin-final').disabled = s !== 'OPEN';
        $('ls-admin-lock').disabled = !biddable;
        if (!biddable) hideUnsold();                 // nothing left to lock
    }

    function renderBidders() {
        const box = $('ls-admin-bidders-list');
        box.textContent = '';
        if (!model.bidders.length) {
            box.appendChild(text('div', 'adm-empty', 'Nobody has registered yet.'));
            return;
        }
        const rows = model.bidders.slice().sort(function (a, b) {
            return (b.owed || 0) - (a.owed || 0) || String(a.identity).localeCompare(String(b.identity));
        });
        rows.forEach(function (b) {
            const row = document.createElement('div');
            row.className = 'adm-row';

            const left = document.createElement('div');
            left.appendChild(text('div', 'adm-who', b.identity));
            const horses = ((b.portfolio && b.portfolio.horses) || []).map(function (h) {
                return '#' + h.horse_id + ' ' + pesos(h.amount);
            });
            const meta = document.createElement('div');
            meta.className = 'adm-meta';
            meta.appendChild(document.createTextNode(
                (horses.length ? horses.join(', ') : 'no horses') + ' · owes ' + pesos(b.owed || 0) + ' · '));
            meta.appendChild(b.paid ? text('span', 'adm-good', 'paid ' + pesos(b.paid_amount))
                                    : document.createTextNode('unpaid'));
            if (b.refund_owed > 0) {
                meta.appendChild(document.createTextNode(' · '));
                meta.appendChild(text('span', 'adm-bad', 'refund owed ' + pesos(b.refund_owed)));
            }
            left.appendChild(meta);
            row.appendChild(left);

            const acts = document.createElement('div');
            acts.className = 'adm-acts';
            if (!b.paid) acts.appendChild(button('Mark paid', '', function () { markPaid(b); }));
            const exempt = !!b.cap_exempt;
            // A toggle: lit and pressed while the bidder is exempt from the cap.
            const cap = button('No cap', exempt ? 'adm-on' : '', function () { toggleCap(b); });
            cap.setAttribute('aria-pressed', exempt ? 'true' : 'false');
            acts.appendChild(cap);
            row.appendChild(acts);
            box.appendChild(row);
        });
    }

    function renderPayouts() {
        const box = $('ls-admin-payouts');
        box.textContent = '';
        const led = model.ledger;
        if (!led) {
            box.appendChild(text('div', 'adm-empty', 'Waiting for pi5…'));
            return;
        }

        // Paid on an earlier result: the result moved away from these marks (a correction in SET WINNERS, RESET,
        // Reset betting, an owner voided). Each line says who was paid what and what the slot pays now; the host
        // settles up and unmarks it. Shown first: it is money handed over.
        const stale = led.paid_on_old_result || [];
        if (stale.length) {
            const warn = document.createElement('div');
            warn.className = 'adm-warn adm-stale';
            warn.setAttribute('role', 'alert');
            warn.appendChild(text('strong', '', 'Paid on an earlier result'));
            warn.appendChild(text('p', '', 'The result changed under these payouts. Settle up, then unmark each one.'));
            stale.forEach(function (s) {
                const line = document.createElement('div');
                line.className = 'adm-stale-line';
                line.appendChild(text('span', '', s.line));
                line.appendChild(button('Unmark', '', function () { markPayout(s.finish, false, s.horse_id); }));
                warn.appendChild(line);
            });
            box.appendChild(warn);
        }

        if (!led.results) {
            box.appendChild(text('div', 'adm-empty', (auctionState() === 'LOCKED' ? '' : 'Lock the auction. ')
                + 'The result is entered once, in SET WINNERS on the Control Center; the payouts follow it here.'));
            return;
        }
        box.appendChild(text('div', 'adm-ledger-figures',
            'Pot ' + pesos(led.total_pot) + ' · WIN ' + pesos(led.amounts.win) + ' · PLACE ' + pesos(led.amounts.place)
            + ' · SHOW ' + pesos(led.amounts.show) + ' (whole pesos, WIN takes the rounding)'));
        led.payouts.forEach(function (p) {
            const row = document.createElement('div');
            row.className = 'adm-row';

            const left = document.createElement('div');
            left.appendChild(text('div', 'adm-who', p.finish.toUpperCase() + ' · ' + pesos(p.amount)));
            let finished = horseLabel(p.horse_id) + ' finished';
            if (p.pays_horse_id) finished += '; ' + horseLabel(p.pays_horse_id) + ' pays it instead';
            left.appendChild(text('div', 'adm-meta', finished));
            const owner = document.createElement('div');
            owner.className = 'adm-meta';
            if (p.unowned) {
                owner.appendChild(text('span', 'adm-flag', 'NO OWNER'));
                owner.appendChild(document.createTextNode(' nobody is paid this until you name the horse that pays it'));
            } else if (p.paid) {
                owner.appendChild(text('span', 'adm-good', 'paid out to ' + p.paid.bidder_identity + ', ' + pesos(p.paid.amount)));
            } else {
                owner.appendChild(document.createTextNode('pay ' + p.bidder_identity));
            }
            left.appendChild(owner);
            row.appendChild(left);

            const open = p.unowned || !!pickerOpen[p.finish];
            const acts = document.createElement('div');
            acts.className = 'adm-acts';
            if (!p.unowned) {
                acts.appendChild(p.paid
                    ? button('Unmark', '', function () { markPayout(p.finish, false, null); })
                    : button('Mark paid out', '', function () { markPayout(p.finish, true, null); }));
                acts.appendChild(button(open ? 'Hide' : 'Change', '', function () {
                    pickerOpen[p.finish] = !pickerOpen[p.finish];
                    renderPayouts();
                }));
            }
            row.appendChild(acts);

            const pick = document.createElement('div');
            pick.className = 'adm-slot-pick';
            pick.hidden = !open;
            const sel = document.createElement('select');
            sel.setAttribute('aria-label', 'Horse that pays the ' + p.finish + ' slot');
            const blank = document.createElement('option');
            blank.value = '';
            blank.textContent = 'Name the horse that pays…';
            sel.appendChild(blank);
            model.horses.forEach(function (h) {
                const o = document.createElement('option');
                o.value = String(h.horse_id);
                const leader = h.current_leader_identity;
                o.textContent = horseLabel(h.horse_id) + ' — ' + (leader || 'unsold');
                o.disabled = !leader;
                sel.appendChild(o);
            });
            sel.value = picks[p.finish] || '';
            sel.addEventListener('change', function () { picks[p.finish] = sel.value; });
            pick.appendChild(sel);
            pick.appendChild(button('Set', '', function () { setSlot(p.finish, sel.value); }));
            row.appendChild(pick);
            box.appendChild(row);
        });
    }

    // ---------------------------------------------------------------------
    // The lock's warning
    // ---------------------------------------------------------------------

    function showUnsold(numbers) {
        const list = $('ls-admin-unsold-list');
        list.textContent = '';
        numbers.forEach(function (n) {
            list.appendChild(text('span', 'adm-chip', horseLabel(n)));
            list.appendChild(document.createTextNode(' '));      // so it reads as a list too
        });
        $('ls-admin-unsold').hidden = false;
    }

    function hideUnsold() { $('ls-admin-unsold').hidden = true; }

    async function lock(confirmed) {
        const r = await postJSON(ADMIN + 'lock', confirmed ? { confirm: true } : {});
        if (!r.ok && r.data && Array.isArray(r.data.unsold)) {
            showUnsold(r.data.unsold);
            say('', true);
            return;
        }
        hideUnsold();
        say(r.ok && r.data.success ? 'Auction locked' : errorOf(r, 'Lock failed'), r.ok && r.data.success);
        refresh();
    }

    // ---------------------------------------------------------------------
    // Actions
    // ---------------------------------------------------------------------

    async function simple(path, ok, fallback) {
        const r = await postJSON(ADMIN + path, {});
        say(r.ok && r.data.success ? ok : errorOf(r, fallback), r.ok && r.data.success);
        refresh();
    }

    async function markPaid(b) {
        const r = await postJSON(ADMIN + 'paid', { bidder_id: b.id });
        say(r.ok && r.data.success ? b.identity + ' marked paid' : errorOf(r, 'Mark paid failed'),
            r.ok && r.data.success);
        refresh();
    }

    async function toggleCap(b) {
        const exempt = !b.cap_exempt;
        const r = await postJSON(ADMIN + 'cap-exempt', { bidder_id: b.id, exempt: exempt });
        say(r.ok && r.data.success
                ? b.identity + (exempt ? ' is exempt from the cap' : ' is under the cap again')
                : errorOf(r, 'Could not change the cap'),
            r.ok && r.data.success);
        refresh();
    }

    async function setSlot(finish, value) {
        const horseId = parseInt(value, 10);
        if (!horseId) { say('Pick the horse that pays the ' + finish + ' slot', false); return; }
        const r = await postJSON(ADMIN + 'payout-slot', { finish: finish, horse_id: horseId });
        const done = r.ok && r.data.success;
        say(done ? finish.toUpperCase() + ' slot is now paid to the owner of ' + horseLabel(horseId)
                 : errorOf(r, 'Could not set the slot'), done);
        if (done) { delete picks[finish]; delete pickerOpen[finish]; }
        refresh();
    }

    // The paid-out mark on a slot: paid true marks the slot's finisher as the result names it now; false clears
    // the mark, on the horse given (a mark paid on an earlier result) or on the current finisher.
    async function markPayout(finish, paid, horseId) {
        const body = { finish: finish, paid: paid };
        if (horseId) body.horse_id = horseId;
        const r = await postJSON(ADMIN + 'payout-paid', body);
        const done = r.ok && r.data.success;
        say(done ? finish.toUpperCase() + (paid ? ' marked paid out' : ' unmarked') : errorOf(r, 'Could not change the mark'), done);
        refresh();
    }

    // ---------------------------------------------------------------------
    // Boot
    // ---------------------------------------------------------------------

    document.addEventListener('DOMContentLoaded', function () {
        $('ls-admin-open').addEventListener('click', function () { simple('start', 'Auction opened', 'Open failed'); });
        $('ls-admin-final').addEventListener('click', function () { simple('final-hour', 'Final hour', 'Final hour failed'); });
        $('ls-admin-lock').addEventListener('click', function () { lock(false); });
        $('ls-admin-unsold-confirm').addEventListener('click', function () { lock(true); });
        $('ls-admin-unsold-cancel').addEventListener('click', function () { hideUnsold(); say('Lock cancelled', true); });

        refresh();
        setInterval(function () {
            // A picker that is open is being used: leave it alone.
            if (document.activeElement && document.activeElement.tagName === 'SELECT') return;
            refresh();
        }, POLL_MS);
    });
})();
