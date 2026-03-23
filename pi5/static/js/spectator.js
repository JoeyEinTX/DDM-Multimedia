// =====================================================================
// DDM Spectator Tote Board — TV Display JavaScript
// State-machine driven display: Socket.IO for state/odds, SSE for results.
// =====================================================================

// ===== SADDLE CLOTH COLORS (matches ddm_control.js exactly) =====

const SADDLE_CLOTHS = {
    1:  { bg: '#E31837', text: '#FFFFFF' },  // Red, white
    2:  { bg: '#FFFFFF', text: '#000000' },  // White, black
    3:  { bg: '#0033A0', text: '#FFFFFF' },  // Blue, white
    4:  { bg: '#FFCD00', text: '#000000' },  // Yellow, black
    5:  { bg: '#00843D', text: '#FFFFFF' },  // Green, white
    6:  { bg: '#000000', text: '#FFD700' },  // Black, gold
    7:  { bg: '#FF6600', text: '#000000' },  // Orange, black
    8:  { bg: '#FF69B4', text: '#000000' },  // Pink, black
    9:  { bg: '#40E0D0', text: '#000000' },  // Turquoise, black
    10: { bg: '#663399', text: '#FFFFFF' },  // Purple, white
    11: { bg: '#808080', text: '#E31837' },  // Grey, red
    12: { bg: '#32CD32', text: '#000000' },  // Lime, black
    13: { bg: '#8B4513', text: '#FFFFFF' },  // Brown, white
    14: { bg: '#800000', text: '#FFCD00' },  // Maroon, yellow
    15: { bg: '#C4B7A6', text: '#000000' },  // Khaki, black
    16: { bg: '#87CEEB', text: '#E31837' },  // Light blue, red
    17: { bg: '#000080', text: '#FFFFFF' },  // Navy, white
    18: { bg: '#228B22', text: '#FFCD00' },  // Forest green, yellow
    19: { bg: '#00008B', text: '#E31837' },  // Dark royal blue, red
    20: { bg: '#FF00FF', text: '#FFCD00' }   // Fuchsia, yellow
};

// ===== STATE MACHINE MAPPINGS =====

// Map RaceState values → which screen div to show
const STATE_TO_SCREEN = {
    'DORMANT':         'dormant',
    'ENTRIES_LOADED':  'entries',
    'BETTING_OPEN':    'entries',
    'BETTING_CLOSING': 'entries',
    'AT_THE_POST':     'atpost',
    'RUNNING':         'running',
    'FINISHED':        'results',
    'OFFICIAL':        'results'
};

// Map RaceState values → header badge text and CSS class
const STATE_BADGES = {
    'DORMANT':         { text: 'STANDBY',      cls: 'dormant' },
    'ENTRIES_LOADED':  { text: 'ENTRIES',       cls: 'entries' },
    'BETTING_OPEN':    { text: 'BETTING OPEN',  cls: 'betting' },
    'BETTING_CLOSING': { text: 'FINAL CALL',    cls: 'final-call' },
    'AT_THE_POST':     { text: 'AT THE POST',   cls: 'racing' },
    'RUNNING':         { text: 'RACE IN PROG',  cls: 'racing' },
    'FINISHED':        { text: 'FINISHED',      cls: 'official' },
    'OFFICIAL':        { text: 'OFFICIAL',      cls: 'official' }
};

// ===== MODULE STATE =====

let currentState   = 'DORMANT';
let currentHorses  = [];
let currentResults = null;

// =====================================================================
// CORE: Screen Transition
// =====================================================================

function transitionTo(newState, horses, results) {
    var screenName = STATE_TO_SCREEN[newState] || 'dormant';

    // Deactivate all screens
    document.querySelectorAll('.screen').forEach(function (s) {
        s.classList.remove('active');
    });

    // Activate target screen
    var target = document.getElementById('screen-' + screenName);
    if (target) target.classList.add('active');

    // Update header badge
    updateStateBadge(newState);

    // Populate the activated screen with data
    if (screenName === 'entries' && horses && horses.length > 0) {
        updateEntriesScreen(newState, horses);
    }
    if (screenName === 'atpost' && horses && horses.length > 0) {
        updateAtPostScreen(horses);
    }
    if (screenName === 'results' && results) {
        updateResultsScreen(results);
    }

    currentState = newState;
}

function updateStateBadge(state) {
    var badge = STATE_BADGES[state] || { text: state, cls: 'dormant' };
    var el = document.getElementById('state-badge');
    if (el) {
        el.textContent = badge.text;
        el.className = 'state-badge ' + badge.cls;
    }
}

// =====================================================================
// ENTRIES SCREEN
// =====================================================================

function updateEntriesScreen(state, horses) {
    var grid = document.getElementById('horse-grid');
    if (!grid) return;

    // First render: build all rows
    if (grid.children.length !== horses.length) {
        grid.innerHTML = '';
        horses.forEach(function (horse) {
            grid.appendChild(createHorseRow(horse));
        });
    } else {
        // Subsequent updates: only flash changed odds in-place
        horses.forEach(function (horse, i) {
            var oddsEl = grid.children[i] ? grid.children[i].querySelector('.horse-odds') : null;
            if (oddsEl) {
                var newOdds = horse.odds || horse.morning_line || '---';
                if (oddsEl.textContent !== newOdds) {
                    oddsEl.textContent = newOdds;
                    oddsEl.classList.add('flash');
                    grid.children[i].classList.add('odds-flash');
                    setTimeout(function () {
                        oddsEl.classList.remove('flash');
                        grid.children[i].classList.remove('odds-flash');
                    }, 600);
                }
            }
        });
    }
}

function createHorseRow(horse) {
    var row = document.createElement('div');
    row.className = 'horse-row';
    row.dataset.position = horse.position;

    var colors = SADDLE_CLOTHS[horse.position] || { bg: '#808080', text: '#FFF' };
    var odds   = horse.odds || horse.morning_line || '---';
    var posStr = String(horse.position).padStart(2, '0');

    row.innerHTML =
        '<div class="cloth" style="background:' + colors.bg + ';color:' + colors.text + '">' + posStr + '</div>' +
        '<div class="horse-info">' +
            '<div class="horse-name">' + (horse.name || 'HORSE ' + horse.position) + '</div>' +
            '<div class="horse-jockey">' + (horse.jockey || '') + '</div>' +
        '</div>' +
        '<div class="horse-jockey-wide">' + (horse.jockey || '') + '</div>' +
        '<div class="horse-odds">' + odds + '</div>';

    return row;
}

/**
 * Flash individual horse odds from a Socket.IO odds_update event.
 * @param {Array} oddsData - Array of {position, odds} objects
 */
function flashOddsUpdate(oddsData) {
    if (!oddsData || !Array.isArray(oddsData)) return;

    oddsData.forEach(function (item) {
        var rows = document.querySelectorAll('[data-position="' + item.position + '"]');
        rows.forEach(function (row) {
            var oddsEl = row.querySelector('.horse-odds');
            if (oddsEl && item.odds) {
                oddsEl.textContent = item.odds;
                oddsEl.classList.add('flash');
                row.classList.add('odds-flash');
                setTimeout(function () {
                    oddsEl.classList.remove('flash');
                    row.classList.remove('odds-flash');
                }, 600);
            }
        });
    });

    updateFooterTimestamp();
}

function updateFooterTimestamp() {
    var el = document.getElementById('odds-updated');
    if (!el) return;
    var now  = new Date();
    var h    = now.getHours();
    var m    = String(now.getMinutes()).padStart(2, '0');
    var ampm = h >= 12 ? 'PM' : 'AM';
    el.textContent = 'ODDS UPDATED ' + (h % 12 || 12) + ':' + m + ' ' + ampm;
}

// =====================================================================
// AT THE POST SCREEN
// =====================================================================

function updateAtPostScreen(horses) {
    var gateRow = document.getElementById('gate-row');
    if (!gateRow || gateRow.children.length === horses.length) return;

    gateRow.innerHTML = '';
    horses.slice(0, 20).forEach(function (horse) {
        var colors = SADDLE_CLOTHS[horse.position] || { bg: '#808080' };
        var gate = document.createElement('div');
        gate.className = 'gate';
        gate.innerHTML =
            '<div class="gate-number">' + horse.position + '</div>' +
            '<div class="gate-cloth" style="background:' + colors.bg + '"></div>';
        gateRow.appendChild(gate);
    });
}

// =====================================================================
// RESULTS SCREEN
// =====================================================================

function updateResultsScreen(results) {
    if (!results) return;

    var board = document.getElementById('results-board');
    if (!board) return;

    var positions = [
        { key: 'win',   label: 'WIN',   cls: 'win' },
        { key: 'place', label: 'PLACE', cls: 'place' },
        { key: 'show',  label: 'SHOW',  cls: 'show' }
    ];

    board.innerHTML = '';

    positions.forEach(function (pos) {
        var horseNum = results[pos.key];
        if (!horseNum) return;

        var colors    = SADDLE_CLOTHS[horseNum] || { bg: '#808080', text: '#FFF' };
        var horse     = currentHorses.find(function (h) { return h.position === horseNum; });
        var horseName = horse ? horse.name.toUpperCase() : 'HORSE ' + horseNum;
        var horseOdds = horse ? (horse.odds || horse.morning_line || '') : '';
        var posStr    = String(horseNum).padStart(2, '0');

        var row = document.createElement('div');
        row.className = 'result-row ' + pos.cls;
        row.innerHTML =
            '<div class="result-label ' + pos.cls + '">' + pos.label + '</div>' +
            '<div class="result-cloth-big" style="background:' + colors.bg + ';color:' + colors.text + '">' + posStr + '</div>' +
            '<div>' +
                '<div class="result-horse-name">' + horseName + '</div>' +
                (horseOdds ? '<div class="result-horse-meta">ML ' + horseOdds + '</div>' : '') +
            '</div>';

        board.appendChild(row);
    });
}

// =====================================================================
// CLOCK
// =====================================================================

function updateClock() {
    var el = document.getElementById('sp-clock');
    if (!el) return;
    var now  = new Date();
    var h    = now.getHours();
    var m    = String(now.getMinutes()).padStart(2, '0');
    var s    = String(now.getSeconds()).padStart(2, '0');
    var ampm = h >= 12 ? 'PM' : 'AM';
    el.textContent = (h % 12 || 12) + ':' + m + ':' + s + ' ' + ampm;
}

// =====================================================================
// SOCKET.IO — realtime state + odds
// =====================================================================

function connectSocketIO() {
    if (typeof io === 'undefined') {
        console.warn('[Spectator] Socket.IO not loaded, retrying in 2 s...');
        setTimeout(connectSocketIO, 2000);
        return;
    }

    var socket = io();

    socket.on('connect', function () {
        console.log('[Spectator] Socket.IO connected');
    });

    socket.on('race_state_change', function (data) {
        console.log('[Spectator] State change:', data);
        var newState = data.state || data.race_state || data.new_state || 'DORMANT';
        var horses   = data.horses || data.race_data?.horses || currentHorses;
        if (horses && horses.length > 0) currentHorses = horses;
        transitionTo(newState, currentHorses, currentResults);
    });

    socket.on('odds_update', function (data) {
        console.log('[Spectator] Odds update:', data);
        var oddsArray = data.horses || data.odds || [];
        flashOddsUpdate(oddsArray);
    });

    socket.on('disconnect', function () {
        console.log('[Spectator] Socket.IO disconnected, will auto-reconnect');
    });
}

// =====================================================================
// SSE — results stream
// =====================================================================

function connectResultsStream() {
    var es = new EventSource('/api/results/stream');

    es.addEventListener('results', function (e) {
        try {
            var data = JSON.parse(e.data);
            console.log('[Spectator] Results received:', data);
            currentResults = data;

            // Always keep the results board up to date
            updateResultsScreen(data);

            // Auto-transition to results screen when race is finishing
            if (['RUNNING', 'FINISHED', 'OFFICIAL'].indexOf(currentState) !== -1) {
                transitionTo('OFFICIAL', currentHorses, data);
            }
        } catch (err) {
            console.error('[Spectator] Error parsing results SSE:', err);
        }
    });

    es.onerror = function () {
        es.close();
        console.warn('[Spectator] SSE error, reconnecting in 5 s...');
        setTimeout(connectResultsStream, 5000);
    };
}

// =====================================================================
// INIT
// =====================================================================

function init() {
    // Start clock
    updateClock();
    setInterval(updateClock, 1000);

    // Build animated running dots (saddle cloth colors 1-5)
    var dotsEl = document.getElementById('running-dots');
    if (dotsEl) {
        [1, 2, 3, 4, 5].forEach(function (n) {
            var d = document.createElement('div');
            d.className = 'running-dot';
            d.style.background = SADDLE_CLOTHS[n].bg;
            dotsEl.appendChild(d);
        });
    }

    // Use server-injected state for instant first paint (no flash of wrong screen)
    if (window.DDM_INITIAL_STATE && window.DDM_INITIAL_STATE.state !== 'DORMANT') {
        var s = window.DDM_INITIAL_STATE;
        currentHorses  = s.horses  || [];
        currentResults = s.results || null;
        transitionTo(s.state, currentHorses, currentResults);
        startRealtime();
    } else {
        // Fallback: fetch from API
        fetch('/api/spectator/state')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) {
                    currentHorses  = data.horses  || [];
                    currentResults = data.results || null;
                    transitionTo(data.state || 'DORMANT', currentHorses, currentResults);
                } else {
                    transitionTo('DORMANT', [], null);
                }
            })
            .catch(function () {
                transitionTo('DORMANT', [], null);
            })
            .finally(startRealtime);
    }
}

function startRealtime() {
    connectSocketIO();
    connectResultsStream();
}

document.addEventListener('DOMContentLoaded', init);
