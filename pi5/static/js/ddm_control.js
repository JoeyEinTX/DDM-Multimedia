// ddm_control.js - Dashboard control JavaScript

// Track if this device just submitted results (to skip reveal popup)
let justSubmittedResults = false;

// Race control: MANUAL (default) or AUTO
let raceControlMode = 'manual';
let raceStatePollInterval = null;

// Track active button
let activeButton = null;

// Track loading start time
let loaderStartTime = null;
const MIN_LOADER_DISPLAY_TIME = 2000; // 2 seconds minimum

// Track finish timer for automatic transition
let finishTimer = null;

// Show loading indicator
function showLoader() {
    const loader = document.getElementById('loader');
    loader.classList.add('show');
    loaderStartTime = Date.now(); // Record when loader started
}

// Hide loading indicator (with minimum display time)
function hideLoader(immediate = false) {
    const loader = document.getElementById('loader');
    
    // If immediate flag is set (e.g., for errors), skip minimum time
    if (immediate || loaderStartTime === null) {
        loader.classList.remove('show');
        loaderStartTime = null;
        return;
    }
    
    const elapsed = Date.now() - loaderStartTime;
    const remaining = MIN_LOADER_DISPLAY_TIME - elapsed;
    
    if (remaining > 0) {
        // Not enough time has passed, delay hiding
        setTimeout(() => {
            loader.classList.remove('show');
            loaderStartTime = null;
        }, remaining);
    } else {
        // Minimum time has passed, hide immediately
        loader.classList.remove('show');
        loaderStartTime = null;
    }
}

// Set active button
function setActiveButton(button) {
    // Remove active class from previous button
    if (activeButton) {
        activeButton.classList.remove('active');
    }
    
    // Set new active button
    if (button) {
        button.classList.add('active');
        activeButton = button;
    } else {
        activeButton = null;
    }
}

// Clear active button
function clearActiveButton() {
    if (activeButton) {
        activeButton.classList.remove('active');
        activeButton.blur(); // Remove focus to clear any lingering highlights
        activeButton = null;
    }
}

// Clear ALL active button highlights across main panel and animations modal
function clearAllActiveButtons() {
    // Clear tracked active button
    clearActiveButton();
    // Clear main panel buttons
    document.querySelectorAll('.panels .btn.active').forEach(btn => btn.classList.remove('active'));
    // Clear animations modal buttons
    document.querySelectorAll('.anim-btn.active').forEach(btn => btn.classList.remove('active'));
}

// Dot matrix patterns (5×7 grid: 5 columns × 7 rows) - 1 = lit, 0 = unlit
// Slashed zero distinguishes 0 from O (authentic tote board style)
const dotPatterns = {
    '0': [0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E],
    '1': [0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E],
    '2': [0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F],
    '3': [0x0E, 0x11, 0x01, 0x06, 0x01, 0x11, 0x0E],
    '4': [0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02],
    '5': [0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E],
    '6': [0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E],
    '7': [0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08],
    '8': [0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E],
    '9': [0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C],
    'A': [0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
    'B': [0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E],
    'C': [0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E],
    'D': [0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C],
    'E': [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F],
    'F': [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10],
    'G': [0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F],
    'H': [0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
    'I': [0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E],
    'J': [0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C],
    'K': [0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11],
    'L': [0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F],
    'M': [0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11],
    'N': [0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11],
    'O': [0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
    'P': [0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10],
    'Q': [0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D],
    'R': [0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11],
    'S': [0x0E, 0x11, 0x10, 0x0E, 0x01, 0x11, 0x0E],
    'T': [0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
    'U': [0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
    'V': [0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04],
    'W': [0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A],
    'X': [0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11],
    'Y': [0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04],
    'Z': [0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F],
    ' ': [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    ':': [0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x00],
    '|': [0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
    // Punctuation a horse's name can carry (the results tote prints names)
    "'": [0x04, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
    '.': [0x00, 0x00, 0x00, 0x00, 0x00, 0x04, 0x00],
    ',': [0x00, 0x00, 0x00, 0x00, 0x00, 0x04, 0x08],
    '-': [0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00],
    '&': [0x08, 0x14, 0x14, 0x08, 0x15, 0x12, 0x0D],
    '!': [0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04],
    '/': [0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10],
    // What La Quiniela's TV board prints besides names: money, its crawl's
    // marks, whatever a chyron line may carry. The board's dot-matrix face
    // (splash_display/static/fonts/DDMTote.ttf) is built from THIS table by
    // splash_display/tools/make_tote_font.py, so the TV's dots are these
    // dots. After adding or changing a pattern, run that script.
    '$': [0x04, 0x0F, 0x14, 0x0E, 0x05, 0x1E, 0x04],
    '+': [0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00],
    '#': [0x0A, 0x0A, 0x1F, 0x0A, 0x1F, 0x0A, 0x0A],
    '%': [0x18, 0x19, 0x02, 0x04, 0x08, 0x13, 0x03],
    '(': [0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02],
    ')': [0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08],
    '?': [0x0E, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04],
    '"': [0x0A, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x00],
    '*': [0x00, 0x04, 0x15, 0x0E, 0x15, 0x04, 0x00],
    '=': [0x00, 0x00, 0x1F, 0x00, 0x1F, 0x00, 0x00],
    ';': [0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x08],
    '<': [0x02, 0x04, 0x08, 0x10, 0x08, 0x04, 0x02],
    '>': [0x08, 0x04, 0x02, 0x01, 0x02, 0x04, 0x08],
    '@': [0x0E, 0x11, 0x17, 0x15, 0x17, 0x10, 0x0E],
    '_': [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F],
    '\u00B7': [0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00],   // the middle dot
    '\u25C6': [0x00, 0x04, 0x0E, 0x1F, 0x0E, 0x04, 0x00],   // the crawl's diamond
    '\u25B6': [0x08, 0x0C, 0x0E, 0x0F, 0x0E, 0x0C, 0x08],   // the crawl's arrow (9 scratched, 22 in)
    '\u00B0': [0x0C, 0x12, 0x12, 0x0C, 0x00, 0x00, 0x00],   // the degree sign (the crawl's weather: 88\u00B0F)
};

// Create a digit element with dot matrix (5×7 grid: 5 columns × 7 rows)
function createDotDigit(char) {
    const digit = document.createElement('div');
    digit.className = 'dot-digit';
    const pattern = dotPatterns[char] || [0,0,0,0,0,0,0];
    
    // Generate 7 rows × 5 columns = 35 dots
    for (let row = 0; row < 7; row++) {
        for (let col = 0; col < 5; col++) {
            const dot = document.createElement('div');
            dot.className = 'dot';
            // Check if this dot should be lit based on the pattern
            if (pattern[row] & (1 << (4 - col))) {
                dot.classList.add('lit');
            }
            digit.appendChild(dot);
        }
    }
    return digit;
}

// Create colon separator
function createColon() {
    const colon = document.createElement('div');
    colon.className = 'dot-colon';
    const dot1 = document.createElement('div');
    const dot2 = document.createElement('div');
    dot1.className = 'dot lit';
    dot2.className = 'dot lit';
    colon.appendChild(dot1);
    colon.appendChild(dot2);
    return colon;
}

// Update dot matrix tote board clock (spectator.html still uses tote-display)
function updateClock() {
    const now = new Date();
    let hours = now.getHours();
    const minutes = now.getMinutes();
    const ampm = hours >= 12 ? 'PM' : 'AM';

    hours = hours % 12;
    hours = hours ? hours : 12;

    const timeStr = String(hours).padStart(2, '0') + String(minutes).padStart(2, '0');
    const display = document.getElementById('tote-display');
    if (display) {
        display.innerHTML = '';

        // Add digits
        display.appendChild(createDotDigit(timeStr[0]));
        display.appendChild(createDotDigit(timeStr[1]));
        display.appendChild(createColon());
        display.appendChild(createDotDigit(timeStr[2]));
        display.appendChild(createDotDigit(timeStr[3]));

        // Add AM/PM
        const ampmDiv = document.createElement('div');
        ampmDiv.className = 'dot-ampm';
        ampmDiv.appendChild(createDotDigit(ampm[0]));
        ampmDiv.appendChild(createDotDigit(ampm[1]));
        display.appendChild(ampmDiv);
    }
}

// Show notification
function showNotification(message, type = 'success') {
    const notification = document.getElementById('notification');
    notification.textContent = message;
    notification.className = `notification ${type}`;
    notification.classList.add('show');
    
    setTimeout(() => {
        notification.classList.remove('show');
    }, 3000);
}

// =====================================================================
// The devices: the LED wall and the tote board (Joey, DevPi, 2026-10-20)
// =====================================================================
// Neither blocks the race: pi5 hands every send to the device's worker and answers at once, so the page never
// waits on a device and never asks after one per tap. GET /api/status, every 5 s, is what the workers last
// learned; it feeds the two pills in the top bar (LED wall · online / unreachable since hh:mm / off), the rows
// of the device modal, the power figures in the Tuning modal, and one toast when a device goes away (the race
// goes on; the device catches up when it returns) and one when it is back. The AUTO card's dot reads it too.
const DEVICE_NAMES = { leds: 'LED wall', tote: 'Tote board' };
let devices = null;                                   // the last /api/status
const deviceWasOnline = { leds: null, tote: null };   // per device, online as last rendered (null: never known)

function hhmm(seconds) {
    const t = new Date(seconds * 1000);
    return String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0');
}

// 'online', 'unreachable since hh:mm', 'off' (no address set), or '' while nothing has been exchanged yet
function deviceWord(device) {
    if (!device.enabled) return 'off';
    if (device.online === null) return '';
    return device.online ? 'online' : 'unreachable since ' + hhmm(device.unreachable_since);
}

function ledsOnline() {
    return !!(devices && devices.leds.online);
}

function renderDevices(status) {
    devices = status;
    const rows = [];
    for (const which of ['leds', 'tote']) {
        const device = status[which];
        const name = DEVICE_NAMES[which];
        const word = deviceWord(device);
        const pill = document.getElementById('device-' + which);
        pill.textContent = word ? `${name} · ${word}` : name;
        pill.className = 'device-pill' + (!device.enabled ? ' off' : device.online === null ? '' : device.online ? ' online' : ' unreachable');
        rows.push(`<div class="device-item"><span class="device-name">${name}</span>` +
                  `<span class="device-address">${device.enabled ? device.address : ''}</span>` +
                  `<span class="device-status-value${!device.enabled || device.online === null ? '' : device.online ? ' connected' : ' offline'}">` +
                  `${word}</span></div>`);
        // a toast on a change only: none for a device that was never away, or that was already away when the page opened
        if (device.enabled && device.online !== null && deviceWasOnline[which] !== null && device.online !== deviceWasOnline[which]) {
            showNotification(device.online ? `${name} back online`
                : `${name} unreachable since ${hhmm(device.unreachable_since)} · the race goes on; it catches up when it returns`,
                device.online ? 'success' : 'error');
        }
        if (device.enabled && device.online !== null) deviceWasOnline[which] = device.online;
    }
    document.getElementById('device-list').innerHTML = rows.join('');
    const power = status.leds.power;
    document.getElementById('tuning-power-display').textContent = power
        ? `⚡ ${(power.current_ma / 1000).toFixed(1)}A (peak: ${(power.peak_ma / 1000).toFixed(1)}A)` : '⚡ --';
    updateRaceControlDot();
}

async function pollDevices() {
    try {
        const response = await fetch('/api/status');
        renderDevices(await response.json());
    } catch (error) {
        console.error('Error polling the devices:', error);
    }
}

// Toggle device status modal
function toggleDeviceModal() {
    const modal = document.getElementById('device-modal');
    modal.classList.add('active');
}

// Close device status modal
function closeDeviceModal() {
    const modal = document.getElementById('device-modal');
    modal.classList.remove('active');
}

// Basic dotted-quad IP validator (each octet 0-255)
function isValidIPv4(ip) {
    if (typeof ip !== 'string') return false;
    const parts = ip.trim().split('.');
    if (parts.length !== 4) return false;
    for (const p of parts) {
        if (!/^\d{1,3}$/.test(p)) return false;
        const n = parseInt(p, 10);
        if (n < 0 || n > 255) return false;
    }
    return true;
}

// Set the small status indicator next to the Update button
function setEsp32ConfigStatus(state, message) {
    const el = document.getElementById('esp32-config-status');
    if (!el) return;
    el.classList.remove('ok', 'fail', 'pending');
    if (state === 'ok') {
        el.textContent = '✓';
        el.classList.add('ok');
    } else if (state === 'fail') {
        el.textContent = '✗';
        el.classList.add('fail');
    } else if (state === 'pending') {
        el.textContent = '…';
        el.classList.add('pending');
    } else {
        el.textContent = '';
    }
    if (message) el.title = message;
}

// Fetch current ESP32 IP/port and pre-populate the inputs
async function loadEsp32Config() {
    try {
        const response = await fetch('/api/esp32/config');
        const data = await response.json();
        const ipInput = document.getElementById('esp32-ip-input');
        const portInput = document.getElementById('esp32-port-display');
        if (ipInput && data.ip) ipInput.value = data.ip;
        if (portInput && data.port != null) portInput.value = String(data.port);
        setEsp32ConfigStatus('');
    } catch (error) {
        console.error('Error loading ESP32 config:', error);
    }
}

// Send a new IP to the server, then show ✓ / ✗ based on PING result
async function updateEsp32Config() {
    const ipInput = document.getElementById('esp32-ip-input');
    if (!ipInput) return;
    const newIp = ipInput.value.trim();

    if (!isValidIPv4(newIp)) {
        showNotification('Invalid IP address', 'error');
        setEsp32ConfigStatus('fail', 'Invalid IP format');
        return;
    }

    setEsp32ConfigStatus('pending');
    try {
        const response = await fetch('/api/esp32/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ip: newIp})
        });
        const data = await response.json();

        if (!data.success) {
            setEsp32ConfigStatus('fail', data.error || 'Update failed');
            showNotification(data.error || 'ESP32 update failed', 'error');
            return;
        }

        if (data.connected) {
            setEsp32ConfigStatus('ok', 'PING succeeded');
            showNotification(`ESP32 updated to ${data.ip} — connected`, 'success');
        } else {
            setEsp32ConfigStatus('fail', 'PING failed');
            showNotification(`ESP32 IP saved (${data.ip}) but PING failed`, 'error');
        }

        pollDevices();                   // the pills and the modal's rows follow the new address
    } catch (error) {
        console.error('Error updating ESP32 config:', error);
        setEsp32ConfigStatus('fail', 'Network error');
        showNotification('Connection error updating ESP32', 'error');
    }
}

// Send command to ESP32
async function sendCommand(command, buttonElement, toggle = false) {
    // If toggle mode and button is already active, turn it off
    const isToggleOff = toggle && (activeButton === buttonElement);

    // Clear all highlights before sending any command
    clearAllActiveButtons();

    if (isToggleOff) {
        showLoader();
        try {
            // Send command to stop (turn off LEDs)
            await fetch('/api/command', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ command: 'LED:ALL_OFF' })
            });
            showNotification('Test stopped', 'success');
            document.getElementById('current-mode').textContent = 'IDLE';
            filterTuningGroups('IDLE');
            hideLoader();
        } catch (error) {
            hideLoader(true); // Hide immediately on error
            console.error('Error stopping command:', error);
            showNotification('Connection error', 'error');
        }
        return;
    }

    // One-shot mode: flash briefly
    if (!toggle) {
        flashButton(buttonElement);
    }

    showLoader();
    try {
        await fetch('/api/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ command: command })
        });
        showNotification(`Command sent: ${command}`, 'success');
        document.getElementById('current-mode').textContent = command.split(':')[0];
        filterTuningGroups(command.split(':')[0].toUpperCase());

        // If toggle mode, set button as active
        if (toggle) {
            setActiveButton(buttonElement);
        }
        hideLoader();
    } catch (error) {
        hideLoader(true); // Hide immediately on error
        console.error('Error sending command:', error);
        showNotification('Connection error', 'error');
    }
}

// Flash button briefly (for one-shot actions)
function flashButton(buttonElement) {
    if (buttonElement) {
        buttonElement.classList.add('flash');
        setTimeout(() => {
            buttonElement.classList.remove('flash');
        }, 100);
    }
}

// =====================================================================
// One race state
// =====================================================================
// The dashboard's modes are the race's state. A mode button starts its LED
// animation, as it always did, and names its mode to pi5, which derives La
// Quiniela's race state from it (the cups, the TV board). The table is
// pi5's (la_quiniela/betting.py MODE_STATES) and is not repeated here; a
// button carries its mode in data-mode. SET WINNERS and RESET are told by
// the routes they already call (/api/results once the results are applied,
// /api/results/clear). Another dashboard, or Reset betting on La Quiniela's
// admin page after the race, can change it too, so what it is now is read
// back every 5 s, and again as a tap that could reopen betting is made. The
// race state does not wait on the LEDs: pi5 hands the animation to the wall's
// worker and answers at once, and the wall's health is the pill's, top left.
// (The Control Center has no ticker since 2026-10-13: the TV's crawl is the
// only crawl.)
let raceModeInfo = null;   // {state, state_name, mode, label, source} as pi5 last reported it

async function setRaceMode(mode, confirmed = {}) {
    try {
        const response = await fetch('/api/quiniela/mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: mode, ...confirmed })
        });
        const data = await response.json();
        if (data && data.ok) {
            raceModeInfo = data;
            return data;
        }
        console.error('[Race state] pi5 refused mode', mode, data && data.error);
        return { ok: false, error: (data && data.error) || ('HTTP ' + response.status) };
    } catch (error) {
        console.error('[Race state] Error setting mode:', error);
        return { ok: false, error: 'connection error' };
    }
}

// The race state as pi5 holds it now, with the figures at the post (GET /api/quiniela/mode: state,
// at_post, the modes table); what was read last when pi5 does not answer.
let raceModes = null;      // pi5's table, {mode: race state}, from the last GET that carried it
// A page left open across an update reloads itself (static/js/page_rev.js, the admin page's mechanism), off this
// 5 s poll, and only while no modal is open and nothing is being typed. Nothing else reloads the page.
function controlCenterIdle() {
    const a = document.activeElement;
    return !document.querySelector('.modal.active, .results-reveal-modal.active') && !(a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
}
async function freshRaceMode() {
    try {
        const response = await fetch('/api/quiniela/mode', { cache: 'no-store' });
        if (window.DDMPageRev) DDMPageRev.check(response.headers.get(DDMPageRev.header), controlCenterIdle);
        const data = await response.json();
        if (data && data.ok) {
            raceModeInfo = data;
            if (data.modes) raceModes = data.modes;
        }
    } catch (error) {
        // pi5 not answering: what was read last stays
    }
    return raceModeInfo;
}

async function pollRaceMode() {
    await freshRaceMode();
    await followServerResults();
    return raceModeInfo;
}

// The results live on pi5 until Reset betting or RESET, a restart included,
// so a page loaded after one shows them (the results tote, the race buttons
// locked). While it does, it checks they are still there: Reset betting on
// La Quiniela's admin page clears them, and the page follows within 5 s (the
// tote goes, the race buttons come back).
async function followServerResults() {
    if (!bannerResults) return;
    try {
        const response = await fetch('/api/results', { cache: 'no-store' });
        const data = await response.json();
        if (data && data.success === false && !data.results) {
            localStorage.removeItem('raceResults');
            hideResultsBanner();
            setRaceComplete(false);
        }
    } catch (error) {
        // pi5 not answering: the page keeps what it shows
    }
}

// "BETTING OPEN" for {state_name: "BETTING_OPEN"}; '' when pi5 has not said
function raceStateLabel(info) {
    return info && info.state_name ? String(info.state_name).replace(/_/g, ' ') : '';
}

// What a mode button reports: the LED message, then the race state it set
// ("Animation: BETTING_60 · BETTING OPEN"), or why it could not
function withRaceState(message, race) {
    if (!race) return message;
    if (race.ok) return `${message} \u00b7 ${raceStateLabel(race)}`;
    return `${message} \u00b7 race state not set (${race.error})`;
}

// The mode a button stands for (data-mode), or null
function modeOf(buttonElement) {
    return (buttonElement && buttonElement.dataset && buttonElement.dataset.mode) || null;
}

// ----- Race Control (AUTO / MANUAL) -----

async function fetchRaceControlMode() {
    try {
        const response = await fetch('/api/racing/mode');
        const data = await response.json();
        if (data.success && data.mode) {
            raceControlMode = data.mode;
            return data.mode;
        }
    } catch (error) {
        console.error('[Race Control] Error fetching mode:', error);
    }
    return raceControlMode;
}

async function setRaceControlMode(mode) {
    try {
        const response = await fetch('/api/racing/mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode })
        });
        const data = await response.json();
        if (data.success) {
            raceControlMode = data.mode;
            updateRaceControlUI();
            showNotification(`Race control: ${data.mode.toUpperCase()}`, 'success');
            return true;
        } else {
            showNotification(data.error || 'Failed to set mode', 'error');
            return false;
        }
    } catch (error) {
        console.error('[Race Control] Error setting mode:', error);
        showNotification('Connection error', 'error');
        return false;
    }
}

function updateRaceControlUI() {
    const toggle = document.getElementById('race-control-toggle');
    const stateText = document.getElementById('race-control-state');
    const apiHint = document.getElementById('race-control-api-hint');
    const dot = document.getElementById('race-control-dot');
    const panels = document.querySelector('.panels');

    if (!toggle || !stateText) return;

    const isAuto = raceControlMode === 'auto';
    toggle.checked = isAuto;

    if (isAuto) {
        stateText.textContent = 'AUTO (API)';
        apiHint.style.display = 'inline';
        panels?.classList.add('race-control-auto');
        // Status dot: green while the LED wall answers, yellow when not
        dot.classList.remove('status-manual', 'status-auto-connected', 'status-auto-disconnected');
        dot.classList.add(ledsOnline() ? 'status-auto-connected' : 'status-auto-disconnected');
    } else {
        stateText.textContent = 'MANUAL';
        apiHint.style.display = 'none';
        panels?.classList.remove('race-control-auto');
        dot.classList.remove('status-auto-connected', 'status-auto-disconnected');
        dot.classList.add('status-manual');
    }
    updatePanelButtons();
}

// AUTO is the mock racing service's demo: a timer walks its own race states and drives the LEDs by itself
// (La Quiniela does not follow it). Race night is MANUAL. Switched on while a race is at the post, running or
// in WINNER, it asks first: the mantle would leave the race and SET WINNERS, STANDBY and RESET go off.
async function autoAllowed() {
    const info = await freshRaceMode();
    const state = info ? info.state : null;
    if (!(state >= AT_THE_POST && state <= WINNER)) return true;
    return askFirst('Switch to AUTO?',
        `The race state is ${raceStateLabel(info)}. AUTO runs the LED demo on its own timer, and SET WINNERS, `
        + 'STANDBY, RESET and the other race buttons stay off until you switch back to MANUAL. Race night is MANUAL.',
        'Switch to AUTO');
}

function initRaceControlToggle() {
    const toggle = document.getElementById('race-control-toggle');
    if (!toggle) return;

    toggle.addEventListener('change', async () => {
        const newMode = toggle.checked ? 'auto' : 'manual';
        if (newMode === 'auto') {
            toggle.checked = false;                  // MANUAL until the answer
            if (!(await autoAllowed())) return;
            toggle.checked = true;
        }
        const success = await setRaceControlMode(newMode);
        if (!success) {
            toggle.checked = raceControlMode === 'auto';
        }
    });

    fetchRaceControlMode().then(() => updateRaceControlUI());
}

async function pollRaceState() {
    try {
        const response = await fetch('/api/racing/state');
        const data = await response.json();
        if (data.success) {
            if (data.mode && data.mode !== raceControlMode) {
                raceControlMode = data.mode;
                updateRaceControlUI();
            }
        }
    } catch (error) {
        console.error('[Race Control] Poll error:', error);
    }
}

function startRaceStatePolling() {
    if (raceStatePollInterval) return;
    raceStatePollInterval = setInterval(pollRaceState, 5000);
}

function stopRaceStatePolling() {
    if (raceStatePollInterval) {
        clearInterval(raceStatePollInterval);
        raceStatePollInterval = null;
    }
}

// The AUTO card's dot follows the LED wall's health (AUTO mode)
function updateRaceControlDot() {
    if (raceControlMode !== 'auto') return;
    const dot = document.getElementById('race-control-dot');
    if (!dot) return;
    dot.classList.remove('status-auto-connected', 'status-auto-disconnected');
    dot.classList.add(ledsOnline() ? 'status-auto-connected' : 'status-auto-disconnected');
}

// =====================================================================
// One tap, one send (Joey, 2026-10-07)
// =====================================================================
// A mode button's requests, the race state and the LEDs, go once a tap.
// While a race state is on its way no mode button takes a tap (pi5 answers
// at once: it is its own work), so a second state cannot overtake it. The
// button tapped takes none until its own requests have ended and
// MODE_HOLD_MS have passed, so a quick second tap cannot turn its animation
// back off; it shows .is-sending meanwhile. (pi5 answers the LED request at
// once as well, since 2026-10-21: the wall's worker takes it. The full-screen
// loader once held every button for the whole LED call; the mode buttons no
// longer show it.)
const MODE_HOLD_MS = 2000;           // as long as the loader stayed up
let raceStateSending = null;         // the race state's request while it is on its way
const modesHeld = new Set();         // the mode buttons whose tap is still being sent

async function holdingMode(buttonElement, send) {
    if (raceStateSending || modesHeld.has(buttonElement)) return;    // nothing sent for this tap
    const tapped = Date.now();
    modesHeld.add(buttonElement);
    buttonElement.classList.add('is-sending');
    try {
        await send();
    } finally {
        setTimeout(() => {
            modesHeld.delete(buttonElement);
            buttonElement.classList.remove('is-sending');
        }, Math.max(0, MODE_HOLD_MS - (Date.now() - tapped)));
    }
}

// No mode button takes a tap until pi5 has answered this race state
// (setRaceMode never throws).
function sendingRaceState(raceSet) {
    raceStateSending = raceSet;
    raceSet.then(() => { if (raceStateSending === raceSet) raceStateSending = null; });
}

// =====================================================================
// Ask first (Joey, 2026-10-13)
// =====================================================================
// A tap that would lose something asks in the page before anything is sent: a button that reopens betting
// while pi5 holds the figures at the post (AT THE POST to AFTER PARTY: the figures and the hand count would be
// cleared), and AUTO switched on from AT THE POST to WINNER. askFirst() shows the question and resolves true for its first button, false for
// Cancel. An answer keeps the dialog up, its buttons off, for ASK_HOLD_MS, so the second tap of a double tap
// lands on the dialog, not on whatever is under it once it has gone; a question asked meanwhile is a Cancel.
// No browser confirm(): it is OK'd without reading, and a Home Screen page shows it oddly.
const AT_THE_POST = 3;               // race states: from AT THE POST to WINNER the figures at the post are held
const WINNER = 5;
const ASK_HOLD_MS = 600;
let askPending = null;               // the question on screen: its resolve
let askHold = null;                  // the hold after an answer

function askFirst(title, text, okLabel) {
    const modal = document.getElementById('ask-modal');
    if (!modal || askPending || askHold) return Promise.resolve(false);
    const ok = document.getElementById('ask-ok');
    const cancel = document.getElementById('ask-cancel');
    document.getElementById('ask-title').textContent = title;
    document.getElementById('ask-text').textContent = text;
    ok.textContent = okLabel;
    ok.disabled = cancel.disabled = false;
    modal.classList.add('active');
    return new Promise((resolve) => { askPending = resolve; });
}

function askAnswer(yes) {
    const resolve = askPending;
    if (!resolve) return;                         // already answered: the second tap of a double tap
    askPending = null;
    document.getElementById('ask-ok').disabled = true;
    document.getElementById('ask-cancel').disabled = true;
    askHold = setTimeout(() => {
        askHold = null;
        document.getElementById('ask-modal').classList.remove('active');
    }, ASK_HOLD_MS);
    resolve(yes);
}

// A mode that could reopen betting: its race state is before AT THE POST (pi5's table), or the table is not
// known yet. AT THE GATE, THEY'RE OFF!, CHAOS, FINISH and HEARTBEAT go at once, as ever.
function mayReopen(mode) {
    return !raceModes || typeof raceModes[mode] !== 'number' || raceModes[mode] < AT_THE_POST;
}

// "This reopens betting. The figures at the post (pot $86) and the hand count ($80) will be cleared."
function reopenText(atPost) {
    const dollars = (n) => '$' + Math.round(Number(n) || 0);
    if (!atPost || atPost.pot === null || atPost.pot === undefined) {
        return 'This reopens betting. The figures at the post will be cleared.';
    }
    const count = atPost.pot_counted;
    return `This reopens betting. The figures at the post (pot ${dollars(atPost.pot)})`
        + (count !== null && count !== undefined ? ` and the hand count (${dollars(count)}) will be cleared.` : ' will be cleared.');
}

// What a tap on `mode` sends along (OD1, OD2; Joey, 2026-10-28, review Q1): nothing, at once, unless it reopens
// betting while pi5 holds the figures at the post (any state from AT THE POST to AFTER PARTY, read as the tap is
// made); then only on Reopen betting, which sends {reopen: true}, or null for Cancel. pi5 clears the figures at the
// post and the hand count when the state arrives, and refuses it without that yes, so a page that did not ask
// cannot; the next AT THE GATE takes fresh figures.
async function reopenAllowed(mode) {
    if (!mayReopen(mode)) return {};
    const info = await freshRaceMode();
    const target = raceModes ? raceModes[mode] : undefined;
    if (typeof target !== 'number' || target >= AT_THE_POST || !(info && info.at_post)) return {};
    return (await askFirst('Reopen betting?', reopenText(info.at_post), 'Reopen betting')) ? { reopen: true } : null;
}

// A mode button's tap asks first when it must (a promise of what to send, null on Cancel); meanwhile no other
// mode button takes a tap.
function askedFirst(mode) {
    const asked = reopenAllowed(mode);
    sendingRaceState(asked);
    return asked;
}

// Every mode button is held while AUTO is on, each saying why on itself, and the race-phase buttons (the
// first three panels) once the results are set (setRaceComplete).
let raceComplete = false;
function updatePanelButtons() {
    const auto = raceControlMode === 'auto';
    document.querySelectorAll('.panels .panel').forEach((panel, i) => {
        panel.querySelectorAll('button.btn').forEach((btn) => {
            const held = auto || (raceComplete && i < 3);
            btn.disabled = held;
            btn.classList.toggle('disabled', held);
            btn.classList.toggle('auto-held', auto);
            let reason = btn.querySelector('.btn-reason');
            if (auto && !reason) {
                reason = document.createElement('span');
                reason.className = 'btn-reason';
                reason.textContent = 'switch to MANUAL';
                btn.appendChild(reason);
            } else if (!auto && reason) {
                reason.remove();
            }
        });
    });
}

// Send animation command with toggle functionality
async function sendAnimation(animName, buttonElement) {
    if (!buttonElement) return animate(animName, buttonElement);     // no button to hold (triggerFinish)
    return holdingMode(buttonElement, () => animate(animName, buttonElement));
}

async function animate(animName, buttonElement) {
    if (raceControlMode === 'auto') {
        showNotification('Switch to MANUAL to control animations', 'error');
        return;
    }
    // One-shot animations that don't stay active
    const oneShotAnimations = ['IDLE'];

    if (oneShotAnimations.includes(animName)) {
        // Clear all highlights and flash the button for one-shot
        clearAllActiveButtons();
        flashButton(buttonElement);
        showLoader();
        try {
            await fetch(`/api/animation/${animName}`, { method: 'POST' });
            showNotification(`Animation: ${animName}`, 'success');
            document.getElementById('current-mode').textContent = animName;
            filterTuningGroups(animName.toUpperCase());
            hideLoader();
        } catch (error) {
            hideLoader(true); // Hide immediately on error
            console.error('Error sending animation:', error);
            showNotification('Connection error', 'error');
        }
        return;
    }

    // Check toggle-off BEFORE clearing (so we can detect re-click on same button)
    const isToggleOff = (activeButton === buttonElement);

    // A mode that would reopen betting from AT THE POST or later asks first; Cancel sends nothing and leaves
    // the page as it was. (Toggling an animation off sends no race state: nothing to ask.)
    const mode = modeOf(buttonElement);
    const asked = mode && !isToggleOff ? await askedFirst(mode) : {};
    if (!asked) return;

    // Clear all highlights before sending new animation
    clearAllActiveButtons();

    // BUG FIX #2: Toggle animations - if same button was clicked, turn OFF the LEDs
    if (isToggleOff) {
        try {
            // Send LED:ALL_OFF command to stop the animation and turn off LEDs
            await fetch('/api/command', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ command: 'LED:ALL_OFF' })
            });
            showNotification('Animation stopped', 'success');
            document.getElementById('current-mode').textContent = 'IDLE';
            filterTuningGroups('IDLE');
            clearActiveButton();
        } catch (error) {
            console.error('Error stopping animation:', error);
            showNotification('Connection error', 'error');
        }
        return;
    }

    // Start new toggle animation. A mode button also names its mode to pi5 (one race state). The LEDs are not
    // waited for: pi5 hands the animation to the wall's worker and answers at once, and what the wall is doing
    // is the pill's, top left
    const raceSet = mode ? setRaceMode(mode, asked) : null;
    if (raceSet) sendingRaceState(raceSet);
    try {
        await fetch(`/api/animation/${animName}`, { method: 'POST' });
        const race = raceSet ? await raceSet : null;
        showNotification(withRaceState(`Animation: ${animName}`, race), 'success');
        document.getElementById('current-mode').textContent = animName;
        filterTuningGroups(animName.toUpperCase());
        setActiveButton(buttonElement);
    } catch (error) {
        console.error('Error sending animation:', error);
        showNotification(withRaceState('Connection error', raceSet ? await raceSet : null), 'error');
    }
}

// Standby - turns off LEDs without clearing results
async function sendStandby() {
    const button = document.querySelector("button[data-mode='STANDBY']");
    return button ? holdingMode(button, standby) : standby();
}

async function standby() {
    if (raceControlMode === 'auto') return;          // its button is held in AUTO and says so
    const asked = await askedFirst('STANDBY');       // with the figures at the post held: asks first
    if (!asked) return;
    const raceSet = setRaceMode('STANDBY', asked);
    sendingRaceState(raceSet);
    try {
        // Turn off all LEDs
        await fetch('/api/led/all_off', { method: 'POST' });
        const race = await raceSet;
        showNotification(withRaceState('Standby mode - LEDs off', race), 'success');
        document.getElementById('current-mode').textContent = 'STANDBY';
        filterTuningGroups('STANDBY');
        // Clear active button state
        clearActiveButton();
    } catch (error) {
        console.error('Error going to standby:', error);
        showNotification('Connection error', 'error');
    }
}

// Reset system - clears results and re-enables all buttons
async function sendReset() {
    if (raceControlMode === 'auto') return;
    if (confirm('Reset race? This will clear results, turn off LEDs, and re-enable all buttons.')) {
        clearAllActiveButtons();
        showLoader();
        try {
            // Clear results on server (deletes results.json and turns off LEDs)
            const response = await fetch('/api/results/clear', {
                method: 'POST'
            });
            
            const data = await response.json();
            
            if (data.success) {
                // Also send RESET to ESP32 to stop animations
                await fetch('/api/command', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ command: 'RESET' })
                });
                // Unlock all cups on ESP32
                await fetch('/api/cup/unlock', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cup: 'ALL' })
                });
                showNotification('Race reset - all systems cleared', 'success');
                document.getElementById('current-mode').textContent = 'IDLE';
                filterTuningGroups('IDLE');
                // Clear ALL results state
                localStorage.removeItem('raceResults');
                pendingResults = null;
                justSubmittedResults = false;
                resultsState = freshResultsState();
                resultsPosts = { win: null, place: null, show: null };
                // Clear finish timer if running
                if (finishTimer) {
                    clearTimeout(finishTimer);
                    finishTimer = null;
                }
                // Hide results banner
                hideResultsBanner();
                // Re-enable all race-phase buttons
                setRaceComplete(false);
                // /api/results/clear ended the race on pi5 (AFTER PARTY): show it
                await pollRaceMode();
                hideLoader();
            } else {
                hideLoader(true); // Hide immediately on error
                showNotification(`Error: ${data.response || data.error}`, 'error');
            }
        } catch (error) {
            hideLoader(true); // Hide immediately on error
            console.error('Error resetting:', error);
            showNotification('Connection error', 'error');
        }
    }
}

// Toggle emergency stop expansion
function toggleEmergencyStop() {
    const expanded = document.getElementById('emergency-expanded');
    const icon = document.getElementById('emergency-icon');
    
    if (expanded.classList.contains('active')) {
        expanded.classList.remove('active');
        icon.style.display = 'flex';
    } else {
        expanded.classList.add('active');
        icon.style.display = 'none';
    }
}

// Emergency stop
async function emergencyStop() {
    if (confirm('EMERGENCY STOP: Turn off all LEDs?')) {
        clearAllActiveButtons();
        showLoader();
        try {
            await fetch('/api/led/all_off', { method: 'POST' });
            showNotification('EMERGENCY STOP - All LEDs OFF', 'error');
            document.getElementById('current-mode').textContent = 'STOPPED';
            filterTuningGroups('STOPPED');
            // Collapse emergency stop after use
            const expanded = document.getElementById('emergency-expanded');
            const icon = document.getElementById('emergency-icon');
            expanded.classList.remove('active');
            icon.style.display = 'flex';
            hideLoader();
        } catch (error) {
            hideLoader(true); // Hide immediately on error
            console.error('Error with emergency stop:', error);
            showNotification('Connection error', 'error');
        }
    }
}

// RGB Test Modal - Color Wheel and Live Preview
let colorPicker = null;
let currentBrightness = 75;

// Open test modal with color wheel. An LED test only: it names no mode to pi5, so the race state, the
// figures at the post and the hand count stay as they are (Joey, 2026-10-13: opening it at the post used to
// set PRE-RACE and drop them). Nothing in the modal needs a race state; 'TEST' below is the LED mode's name.
function openTestModal() {
    clearAllActiveButtons();
    document.getElementById('current-mode').textContent = 'TEST';
    filterTuningGroups('TEST');
    const modal = document.getElementById('test-modal');
    modal.classList.add('active');

    // Initialize color picker if not already done
    if (!colorPicker) {
        colorPicker = new iro.ColorPicker('#color-picker', {
            width: 210,
            color: '#E195AB',
            borderWidth: 0,
            borderColor: '#1B998B',
            padding: 0,
            margin: 0,
            handleRadius: 4,
            layout: [
                {
                    component: iro.ui.Wheel,
                    options: { borderWidth: 0, handleRadius: 4 }
                }
            ]
        });

        colorPicker.on('color:change', function(color) {
            sendTestColor(color.rgb.r, color.rgb.g, color.rgb.b, currentBrightness);
        });

        const brightnessSlider = document.getElementById('brightness-slider');
        brightnessSlider.addEventListener('input', function() {
            currentBrightness = parseInt(this.value);
            document.getElementById('brightness-value').textContent = currentBrightness;
            const color = colorPicker.color.rgb;
            sendTestColor(color.r, color.g, color.b, currentBrightness);
        });

        document.querySelectorAll('.preset-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                const colorStr = this.dataset.color;
                const name = this.dataset.name;
                if (name === 'OFF') {
                    sendTestOff();
                } else {
                    const [r, g, b] = colorStr.split(',').map(v => parseInt(v));
                    colorPicker.color.rgb = { r, g, b };
                    sendTestColor(r, g, b, currentBrightness);
                }
            });
        });
    }
}

// Close test modal and turn off LEDs
async function closeTestModal() {
    const modal = document.getElementById('test-modal');
    modal.classList.remove('active');
    clearAllActiveButtons();
    document.getElementById('current-mode').textContent = 'IDLE';
    filterTuningGroups('IDLE');

    // Send LED:ALL_OFF command
    try {
        await fetch('/api/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ command: 'LED:ALL_OFF' })
        });
    } catch (error) {
        console.error('Error turning off LEDs:', error);
    }
}

// Send test color command
async function sendTestColor(r, g, b, brightness) {
    const command = `LED:TEST:${Math.round(r)},${Math.round(g)},${Math.round(b)},${brightness}`;
    
    try {
        const response = await fetch('/api/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: command })
        });
        const data = await response.json();
        if (!data.success) {
            console.error('Test color command failed:', data);
        }
    } catch (error) {
        console.error('Error sending test color:', error);
    }
}

// Send test off command
async function sendTestOff() {
    try {
        await fetch('/api/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: 'LED:ALL_OFF' })
        });
    } catch (error) {
        console.error('Error sending test off:', error);
    }
}

// Trigger finish animation with timed transition to fast heartbeat
async function triggerFinish() {
    // Clear any existing timer
    if (finishTimer) {
        clearTimeout(finishTimer);
        finishTimer = null;
    }
    
    // Send checkered/finish animation
    await sendAnimation('FINISH');
    
    // After 60 seconds, transition to heartbeat cooldown
    finishTimer = setTimeout(async () => {
        try {
            const response = await fetch('/api/animation/HEARTBEAT_COOLDOWN', {
                method: 'POST'
            });

            const data = await response.json();

            if (data.success) {
                showNotification('Transitioning to heartbeat cooldown', 'success');
                document.getElementById('current-mode').textContent = 'COOLDOWN';
                filterTuningGroups('COOLDOWN');
            }
        } catch (error) {
            console.error('Error transitioning to heartbeat cooldown:', error);
        }
        finishTimer = null;
    }, 60000);  // 60 seconds
}

// =====================================================================
// La Quiniela's field: the horses' names
// =====================================================================
// The names are La Quiniela's (its admin page, /quiniela/admin); the
// dashboard only reads them, for the results tote and the SET WINNERS
// pickers. GET /api/quiniela/field lists the field by post. A post is a
// place on the mantle, 1-20, and the LED cup there. A horse standing in for
// a scratched one runs from the scratched horse's post under its own number
// (22 Ocelli from post 9: the cup was renumbered, nothing moved), so a pick
// has two numbers: the post (which LED cup to light) and the horse (what
// the results say). A post whose horse was scratched with no replacement is
// not in the list and is not offered.
let quinielaField = null;   // {names_rev, posts: [{post, horse, name, label, replaces?}], names: {"1": "..."}}

// What is shown until La Quiniela answers (or if it cannot): posts 1-20, no names.
function defaultField() {
    const posts = [];
    const names = {};
    for (let n = 1; n <= 24; n++) names[String(n)] = '';
    for (let p = 1; p <= 20; p++) {
        posts.push({ post: p, horse: p, name: '', label: `${p} \u00b7 HORSE ${p}` });
    }
    return { names_rev: null, posts: posts, names: names };
}

async function loadQuinielaField() {
    const before = quinielaField ? quinielaField.names_rev : undefined;
    try {
        const response = await fetch('/api/quiniela/field', { cache: 'no-store' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        if (data && Array.isArray(data.posts) && data.names) quinielaField = data;
    } catch (error) {
        console.error('Error loading La Quiniela field:', error);
    }
    if (!quinielaField) quinielaField = defaultField();
    // The SET WINNERS picker, while it is open, follows the field it was built from (a scratch on the LQ admin page)
    if (resultsModalOpen() && quinielaField.names_rev !== before) pickerFollowsField();
    return quinielaField;
}

// What each horse's cup held at the post, for the SET WINNERS pickers' NO BETS
// marks: La Quiniela's figures at the post (its model's closing, GET
// /api/quiniela), taken when betting closed and frozen from then on. They are
// keyed by horse number, the number the results carry (22 running for 9 is
// horse 22), and a horse no cup claimed held 0. Returns {horse: tokens}, or
// null when there are no figures at the post (the race never reached the post,
// betting reopened, pi5 not answering): unknown is not zero, and nothing is
// marked then.
async function loadBetsAtPost() {
    try {
        const response = await fetch('/api/quiniela', { cache: 'no-store' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const model = await response.json();
        return betsAtPost(model && model.closing);
    } catch (error) {
        console.error('Error loading the figures at the post:', error);
        return null;
    }
}

function betsAtPost(closing) {
    const horses = closing && closing.horses;
    if (!horses || typeof horses !== 'object') return null;
    const bets = {};
    for (let n = 1; n <= 24; n++) {
        const tokens = Number(horses[String(n)] && horses[String(n)].tokens);
        bets[n] = Number.isFinite(tokens) && tokens > 0 ? tokens : 0;
    }
    return bets;
}

// The name the tote and the pickers print for a horse: La Quiniela's, or HORSE n.
function horseDisplayName(n) {
    const names = (quinielaField && quinielaField.names) || {};
    const name = String(names[String(n)] || '').trim();
    return name ? name.toUpperCase() : `HORSE ${n}`;
}

// Results modal state (win / place / show hold HORSE numbers; next the finishers after SHOW)
// ---------------------------------------------------------------------
// SET WINNERS is a small state machine that only the host's taps move. It
// holds the horse in each of the three slots (null: empty), `next`, the
// finishers after SHOW in order (4th first; a horse, or null while that card
// is empty), and `chosen`, the slot the host tapped on purpose to change
// (null: none). The slot the next horse tap fills is activeSlot(): the chosen
// one, else the first empty one in the order WIN, PLACE, SHOW, 4TH, 5TH...,
// else none (every place is set and none was chosen: a horse tap is refused,
// it never overwrites). A tap works that out from this state alone and writes
// the state in the same turn; the LED cups follow it afterwards (ledCall), so
// nothing that runs in the background, no answer from pi5 and no slow LED
// controller can change which slot a tap fills. (The picker used to move on
// to the next slot only once the LED lock for the pick had been answered, so
// a second tap inside that window filled the first slot again: the PLACE
// horse landed in WIN.) A horse is in one slot only: tapping one that is
// already placed is refused, with a message, and moves nothing.
//
// The real finish is recorded, never a substitute (Joey, 2026-10-20): the
// horses that crossed 1st, 2nd and 3rd. A finisher whose cup held no bets at
// the post is one La Quiniela will skip for the next finisher, so the record
// goes on: a 4TH card (then a 5TH...) asks for the next finisher until three
// horses with bets are named (finishersNeeded, pi5's own rule), and CONFIRM
// RESULTS waits for them. The LEDs light the three; a next finisher's cup is
// not lit. La Subasta pays the owners of the real 1st, 2nd and 3rd.
//
// While the picker is open it follows La Quiniela's field (a scratch on the
// LQ admin page): the field is read every RESULTS_FIELD_POLL_MS, and a horse
// that left it leaves its slot and the grid, with a note (pickerFollowsField).
// pi5 refuses a result that names a horse not in the field whatever the page
// shows; its reason stands beside CONFIRM RESULTS.
const RESULT_SLOTS = ['win', 'place', 'show'];
const RESULT_SLOT_NAMES = { win: 'WIN', place: 'PLACE', show: 'SHOW' };
const NEXT_SLOT = 'next:';                 // a next finisher's slot key: next:0 is 4TH, next:1 is 5TH...
const RESULTS_FIELD_POLL_MS = 2000;

// refusal: what pi5 said to the last CONFIRM RESULTS and the picks it said it to; it stays beside the button until the picks change
function freshResultsState() {
    return { chosen: null, win: null, place: null, show: null, next: [], tied: [], refusal: null };
}
let resultsState = freshResultsState();

// The posts (LED cups) of the three picks
let resultsPosts = { win: null, place: null, show: null };

// The bets at the post by horse (loadBetsAtPost), read once as the picker opens
// and left alone while it is open: they are frozen figures, so the NO BETS marks
// cannot move under the host. null: no figures at the post, no marks.
let resultsBets = null;

// A horse whose cup held no bets at the post. La Quiniela skips it for the next
// finisher (an empty cup has no token to draw), so the picker marks it and asks
// for the finisher after it; picking it is right, it is the real finish. False
// whenever the figures at the post are unknown.
function hadNoBets(horse) {
    return resultsBets !== null && !(resultsBets[horse] > 0);
}

// The slots: WIN, PLACE and SHOW, and the next finishers' cards (next:0 is 4TH)
function isNextSlot(key) {
    return typeof key === 'string' && key.startsWith(NEXT_SLOT);
}

function nextIndex(key) {
    return parseInt(key.slice(NEXT_SLOT.length), 10);
}

// 4 -> 4TH, 21 -> 21ST, 12 -> 12TH
function ordinalLabel(n) {
    const rest = n % 100;
    const suffix = (rest >= 11 && rest <= 13) ? 'TH' : ({ 1: 'ST', 2: 'ND', 3: 'RD' }[n % 10] || 'TH');
    return `${n}${suffix}`;
}

function slotName(key) {
    return isNextSlot(key) ? ordinalLabel(nextIndex(key) + 4) : RESULT_SLOT_NAMES[key];
}

function getSlot(key) {
    return isNextSlot(key) ? (resultsState.next[nextIndex(key)] || null) : resultsState[key];
}

function setSlot(key, horse) {
    if (!isNextSlot(key)) {
        resultsState[key] = horse;
        return;
    }
    const i = nextIndex(key);
    while (resultsState.next.length < i) resultsState.next.push(null);
    resultsState.next[i] = horse;
    while (resultsState.next.length && resultsState.next[resultsState.next.length - 1] === null) resultsState.next.pop();
}

// The finish as named so far, in order: WIN, PLACE, SHOW, then the next finishers
function namedFinish() {
    return RESULT_SLOTS.map((s) => resultsState[s]).concat(resultsState.next).filter((h) => h);
}

// How many more finishers the record needs: pi5's rule (la_quiniela/betting.py
// finishersNeeded), from the frozen figures at the post and the field as the
// grid has it. A horse with no bets is skipped by La Quiniela, so the finish
// goes on until three horses with bets are named, or every horse in the field
// that had bets when fewer did. 0 while the figures are unknown.
function finishersNeeded(named) {
    if (resultsBets === null) return 0;
    const field = ((quinielaField && quinielaField.posts) || []).map((p) => p.horse);
    const withBets = field.filter((h) => resultsBets[h] > 0);
    const have = named.filter((h) => withBets.includes(h)).length;
    return Math.max(0, Math.min(3, withBets.length) - have);
}

// The next finishers' slot keys: one card per finisher named after SHOW, one
// per finisher La Quiniela still needs beyond the empty WIN, PLACE and SHOW
// slots (those take the next horses first), and one for a dead heat marked on
// the last card (its partner's)
function nextSlotKeys() {
    const empty = RESULT_SLOTS.filter((key) => !resultsState[key]).length;
    let count = resultsState.next.length + Math.max(0, finishersNeeded(namedFinish()) - empty);
    resultsState.tied.forEach((key) => { if (isNextSlot(key)) count = Math.max(count, nextIndex(key) + 1); });
    const keys = [];
    for (let i = 0; i < count; i++) keys.push(NEXT_SLOT + i);
    return keys;
}

function allSlotKeys() {
    return RESULT_SLOTS.concat(nextSlotKeys());
}

// Winner colors for cup locking
const GOLD_RGB = { r: 255, g: 215, b: 0 };
const SILVER_RGB = { r: 192, g: 192, b: 192 };
const BRONZE_RGB = { r: 205, g: 127, b: 50 };
const RESULT_COLOURS = { win: GOLD_RGB, place: SILVER_RGB, show: BRONZE_RGB };

// The slot the next horse tap fills: the one the host chose, else the first
// empty one in order, else none.
function activeSlot() {
    const keys = allSlotKeys();
    if (resultsState.chosen && !keys.includes(resultsState.chosen)) resultsState.chosen = null;   // its card is gone
    if (resultsState.chosen) return resultsState.chosen;
    return keys.find((key) => !getSlot(key)) || null;
}

// The slot a horse is in, or null
function slotOfHorse(horse) {
    return allSlotKeys().find((key) => getSlot(key) === horse) || null;
}

function resultsPicksMade() {
    return namedFinish().length > 0;
}

function resultsModalOpen() {
    const modal = document.getElementById('results-modal');
    return !!modal && modal.classList.contains('active');
}

// The picker's LED calls (lock a cup as it is picked, unlock it when its pick
// is changed or cleared, all of them on RESET and CANCEL) go one after another
// in the order of the taps and never hold the picker up: the picks are the
// page's own and the LEDs follow them. A slow or failed call only ends itself.
let ledChain = Promise.resolve();
function ledCall(path, body) {
    const options = { method: 'POST' };
    if (body !== undefined) {
        options.headers = { 'Content-Type': 'application/json' };
        options.body = JSON.stringify(body);
    }
    const call = ledChain.then(() => fetch(path, options)).catch((error) => {
        console.error('LED call failed:', path, error);
    });
    ledChain = call;
    return call;
}

// The LED calls in flight, waited for at most `ms`: the results do not wait on
// the LEDs for longer than that.
function ledDrain(ms) {
    return Promise.race([ledChain, new Promise((resolve) => setTimeout(resolve, ms))]);
}

// A line under the heading for what a tap could not do, in plain words, until
// the next tap that works.
function resultsNote(text) {
    const el = document.getElementById('results-pick-note');
    if (el) el.textContent = text || '';
}

// The picks as they stand: a refusal holds only while they are these
function picksKey() {
    return JSON.stringify([namedFinish(), resultsState.tied]);
}

// "19 · GOLDEN TEMPO"
function horseLabel(horse) {
    return `${horse} · ${horseDisplayName(horse)}`;
}

// The field, read again every 2 s while the picker is open and only then
// (loadQuinielaField hands a change to pickerFollowsField)
let resultsFieldTimer = null;
function startResultsFieldPoll() {
    stopResultsFieldPoll();
    resultsFieldTimer = setInterval(() => {
        if (resultsModalOpen()) loadQuinielaField();
        else stopResultsFieldPoll();
    }, RESULTS_FIELD_POLL_MS);
}

function stopResultsFieldPoll() {
    if (resultsFieldTimer !== null) {
        clearInterval(resultsFieldTimer);
        resultsFieldTimer = null;
    }
}

// The field changed under an open picker (a scratch, an undo, a name typed on
// the LQ admin page): a horse that is no longer in it leaves its slot (its cup
// unlocked) and the grid, the grid is rebuilt (a replacement shows under its
// post), the slot cards show the names as they are now, and the note says who
// left. The picks that still run, the marks and the chosen slot stay.
function pickerFollowsField() {
    const inField = new Set(((quinielaField && quinielaField.posts) || []).map((p) => p.horse));
    const shown = [...document.querySelectorAll('.winner-pick-btn')].map((b) => parseInt(b.dataset.horse, 10));
    const notes = [];
    shown.filter((horse) => !inField.has(horse)).forEach((horse) => {
        const key = slotOfHorse(horse);
        if (key) {
            const post = isNextSlot(key) ? null : resultsPosts[key];
            setSlot(key, null);
            if (!isNextSlot(key)) resultsPosts[key] = null;
            if (post !== null) ledCall('/api/cup/unlock', { cup: post });
            notes.push(`#${horse} ${horseDisplayName(horse)} was scratched: ${slotName(key)} is empty, tap the horse that finished there.`);
        } else {
            notes.push(`#${horse} ${horseDisplayName(horse)} was scratched.`);
        }
    });
    generateSaddleClothGrid();
    updateResultsModalUI();
    if (notes.length) resultsNote(notes.join(' '));
}

// The result pi5 held when the picker opened ({win, place, show, next, ties} or null): the dialog opens on it,
// CHECK THE RESULTS with the picks, so a correction is one tap; CANCEL puts the mantle back on it
let savedOnOpen = null;

function preloadResults(saved) {
    resultsState = freshResultsState();
    RESULT_SLOTS.forEach((key) => { resultsState[key] = saved[key] || null; });
    resultsState.next = (saved.next || []).slice();
    const order = namedFinish();
    (saved.ties || []).forEach((group) => {
        group.slice(1).forEach((horse) => {
            const i = order.indexOf(horse);
            if (i >= 0) resultsState.tied.push(i < RESULT_SLOTS.length ? RESULT_SLOTS[i] : NEXT_SLOT + (i - RESULT_SLOTS.length));
        });
    });
    // the three cups lit as their picks were (a next finisher's is not: the mantle shows the three that crossed first)
    const postOf = {};
    ((quinielaField && quinielaField.posts) || []).forEach((p) => { postOf[p.horse] = p.post; });
    RESULT_SLOTS.forEach((key) => {
        const horse = resultsState[key];
        resultsPosts[key] = horse && postOf[horse] !== undefined ? postOf[horse] : null;
        if (resultsPosts[key] === null) return;
        const colour = RESULT_COLOURS[key];
        ledCall('/api/cup/lock', { cup: resultsPosts[key], r: colour.r, g: colour.g, b: colour.b });
    });
}

// Show results modal with the pickers
let resultsModalOpening = false;
async function showResultsModal() {
    if (raceControlMode === 'auto') return;
    if (resultsModalOpening || resultsModalOpen()) return;     // a second tap on SET WINNERS: the picker is coming up or is up
    resultsModalOpening = true;

    try {
        // Clear finish timer if running
        if (finishTimer) {
            clearTimeout(finishTimer);
            finishTimer = null;
        }

        // Stop any running animation first
        clearActiveButton();

        // Reset state
        resultsState = freshResultsState();
        resultsPosts = { win: null, place: null, show: null };
        resultsNote('');

        // Start RESULTS_ENTRY animation (clears cup locks internally)
        await ledCall('/api/animation/RESULTS_ENTRY');

        // The field as La Quiniela has it now (names, replacements, scratches), its
        // figures at the post (the NO BETS marks: read here only, so nothing changes
        // them while the picker is open) and the result pi5 holds, if one does
        const [, bets, saved] = await Promise.all([loadQuinielaField(), loadBetsAtPost(), savedResultsOnPi5()]);
        resultsBets = bets;
        document.getElementById('results-modal').classList.add('active');      // up before the cards are made: a name is fitted where it is shown
        generateSaddleClothGrid();
        savedOnOpen = saved;
        if (saved) preloadResults(saved);
        updateResultsModalUI();
        startResultsFieldPoll();
    } finally {
        resultsModalOpening = false;
    }
}

// Generate the pickers: one per horse in the field, in program-number order
// (4 across): a replacement stands where its number falls, at the end, and a
// scratched horse leaves no blank (Joey, 2026-10-23). Each shows the horse as
// "19 · GOLDEN TEMPO": the number on its saddle cloth, then La Quiniela's name
// (HORSE n without one). A tile carries two numbers, and a tap uses the tile's
// own: the horse, which the results record, and the post it runs from, the LED
// cup that lights (21 running for 19 lights cup 19). A horse whose cup held no
// bets at the post is dimmed and tagged NO BETS.
function generateSaddleClothGrid() {
    const grid = document.getElementById('saddle-cloth-grid');
    grid.innerHTML = '';

    const entries = ((quinielaField && quinielaField.posts) || defaultField().posts).slice().sort((a, b) => a.horse - b.horse);
    for (const entry of entries) {
        const post = entry.post;
        const horse = entry.horse;
        const name = horseDisplayName(horse);

        const btn = document.createElement('button');
        btn.className = 'winner-pick-btn';
        btn.dataset.post = post;
        btn.dataset.horse = horse;
        btn.title = `${horse} · ${name}`;

        // Saddle cloth colors of the horse's own number
        const colors = SADDLE_CLOTHS[horse] || { bg: '#808080', text: '#FFFFFF' };
        const num = document.createElement('span');
        num.className = 'winner-pick-num';
        num.style.backgroundColor = colors.bg;
        num.style.color = colors.text;
        num.textContent = String(horse);

        const sep = document.createElement('span');
        sep.className = 'winner-pick-sep';
        sep.textContent = '·';

        const label = document.createElement('span');
        label.className = 'winner-pick-name';
        label.textContent = name;

        // Which slot the horse is in, once it is in one
        const tag = document.createElement('span');
        tag.className = 'winner-pick-tag';

        // NO BETS, when nobody bet the horse (updateResultsModalUI)
        const none = document.createElement('span');
        none.className = 'winner-pick-nobets';

        btn.appendChild(num);
        btn.appendChild(sep);
        btn.appendChild(label);
        btn.appendChild(tag);
        btn.appendChild(none);
        btn.onclick = () => selectCup(post, horse);

        grid.appendChild(btn);
    }
}

// A card's name wraps only between words. A word wider than the room it has
// (none in a real field at a single card's size, but a name is free text, and a
// dead heat's horses share a card) takes a smaller size, a pixel at a time,
// until it fits: a name is never broken inside a word.
const SLOT_NAME_MIN_PX = 12;
function fitSlotName(el) {
    el.style.fontSize = '';
    const room = el.parentElement.clientWidth;      // the card's width for it: a long word can make the name wider
    let size = parseFloat(getComputedStyle(el).fontSize);
    while (el.scrollWidth > room + 0.5 && size > SLOT_NAME_MIN_PX) {
        size -= 1;
        el.style.fontSize = size + 'px';
    }
}

// Update a place's card with its horse: the saddle cloth, the name and the NO BETS badge (animated). A card that
// already shows what it should is left alone, so a pick made in one does not play the others again.
function updateSlot(slot, horseNum) {
    const slotEl = document.getElementById(`slot-${slot}`);
    if (!slotEl) return;                             // renderResultCards has not made the card yet
    const shown = horseNum ? `${horseNum}|${horseDisplayName(horseNum)}|${hadNoBets(horseNum)}` : '';
    if (slotEl.dataset.shown === shown) return;
    slotEl.dataset.shown = shown;
    if (horseNum) {
        const colors = SADDLE_CLOTHS[horseNum] || { bg: '#808080', text: '#FFFFFF' };
        slotEl.innerHTML = `<div class="slot-saddle" style="background:${colors.bg}; color:${colors.text}">${String(horseNum).padStart(2, '0')}</div>`
            + '<div class="slot-who"></div>';
        const who = slotEl.lastElementChild;
        who.appendChild(makeEl('div', 'slot-horse-name', horseDisplayName(horseNum)));
        if (hadNoBets(horseNum)) {
            const none = document.createElement('div');
            none.className = 'slot-nobets';
            none.textContent = 'NO BETS';
            who.appendChild(none);
        }
        slotEl.classList.add('filled', 'slot-named');
    } else {
        slotEl.innerHTML = '';
        slotEl.classList.remove('filled', 'slot-named');
    }
}

function makeEl(tag, className, text) {
    const el = document.createElement(tag);
    el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
}

// The card of one place: its name and the x that empties it over the horse's cloth, name and NO BETS badge
// (updateSlot), and what a tap on it does
function slotCard(key) {
    const name = slotName(key);
    const card = makeEl('div', 'result-slot');
    card.dataset.slot = key;
    card.setAttribute('role', 'button');
    card.tabIndex = 0;
    card.onclick = () => chooseSlot(key);
    card.onkeydown = (event) => {
        if (event.target === card && (event.key === 'Enter' || event.key === ' ')) {
            event.preventDefault();
            chooseSlot(key);
        }
    };
    const clear = makeEl('button', 'slot-clear');
    clear.type = 'button';
    clear.dataset.slot = key;
    clear.setAttribute('aria-label', `Clear ${name}`);
    clear.innerHTML = '&times;';
    clear.onclick = (event) => { event.stopPropagation(); clearSlot(key); };
    const head = makeEl('div', 'slot-head');
    head.append(makeEl('span', `slot-label ${isNextSlot(key) ? 'next' : key}`, name), clear);
    const value = makeEl('div', 'slot-value');
    value.id = `slot-${key}`;
    card.append(head, value, makeEl('span', 'slot-hint'));
    return card;
}

// The places as cards, in the order of the finish: WIN, PLACE, SHOW, then a card per next finisher as nextSlotKeys()
// says. A dead heat is one card, its title over the horses side by side, and one UNTIE. On the seam under a card sits
// the link that ties the card after it, wherever a dead heat of two or three can be made. The places' cards are kept
// from one call to the next and redrawn only when their horse, name or badge changed; the groups and the seams are made
// again.
function renderResultCards() {
    const holder = document.getElementById('results-cards');
    if (!holder) return;
    const kept = new Map([...holder.querySelectorAll('.result-slot')].map((card) => [card.dataset.slot, card]));
    const groups = placeGroups();
    const parts = [];
    groups.forEach((keys, i) => {
        const group = makeEl('div', 'result-group');
        const cards = keys.map((key) => kept.get(key) || slotCard(key));
        if (keys.length === 1) {
            group.append(cards[0]);
        } else {
            group.classList.add('is-dead-heat');
            const undo = makeEl('button', 'group-untie', 'UNTIE');
            undo.type = 'button';
            undo.onclick = () => untie(keys[0]);
            const head = makeEl('div', 'group-head');
            const title = makeEl('span', 'group-title');
            title.append(makeEl('span', '', keys.map(slotName).join(' · ')), ' ', makeEl('span', '', '— DEAD HEAT'));
            head.append(title, undo);
            const cells = makeEl('div', 'group-cells');
            cells.append(...cards);
            group.append(head, cells);
        }
        parts.push(group);
        const last = keys[keys.length - 1];
        const after = groups[i + 1] || [slotKeyAfter(last)];
        if (getSlot(last) && keys.length + after.length <= 3) {
            const seam = makeEl('div', 'seam');
            const link = makeEl('button', '', 'DEAD HEAT');
            link.type = 'button';
            link.dataset.after = last;
            link.setAttribute('aria-label', `${slotName(slotKeyAfter(last))} crossed together with ${slotName(last)}`);
            link.onclick = () => tieAfter(last);
            seam.appendChild(link);
            parts.push(seam);
        }
    });
    holder.replaceChildren(...parts);
    allSlotKeys().forEach((key) => updateSlot(key, getSlot(key)));
    holder.querySelectorAll('.slot-horse-name').forEach(fitSlotName);
}

// A tap on a horse: the horse number is what the results say; the post is the
// LED cup that lights. It fills the active slot, in this turn, from the state
// and nothing else.
function selectCup(post, horse) {
    const slot = activeSlot();
    const where = slotOfHorse(horse);

    if (where) {
        // One horse, one slot. The horse is in the slot being changed: nothing to do but
        // stop changing it; in another slot: refused.
        if (where === slot) {
            resultsState.chosen = null;
            resultsNote('');
        } else {
            resultsNote(`${horseLabel(horse)} is already ${slotName(where)}. `
                + `To use it elsewhere, clear ${slotName(where)} first.`);
        }
        updateResultsModalUI();
        return;
    }
    if (!slot) {
        resultsNote('Every place is set. Tap one of them to change it.');
        updateResultsModalUI();
        return;
    }

    const replaced = isNextSlot(slot) ? null : resultsPosts[slot];   // the post whose cup this pick replaces, on a change on purpose
    setSlot(slot, horse);
    if (!isNextSlot(slot)) resultsPosts[slot] = post;
    resultsState.chosen = null;
    resultsNote('');
    updateResultsModalUI();

    // The LEDs follow the three picks: the replaced horse's cup back to the animation, the new one locked to the
    // slot's color. A next finisher's cup is not lit: the mantle shows the three that crossed first.
    if (isNextSlot(slot)) return;
    if (replaced !== null && replaced !== post) {
        ledCall('/api/cup/unlock', { cup: replaced });
    }
    const colour = RESULT_COLOURS[slot];
    ledCall('/api/cup/lock', {
        cup: post,
        r: colour.r,
        g: colour.g,
        b: colour.b
    });
}

// A tap on a slot: the next horse tap fills that slot (and replaces what is
// in it); tapping it again goes back to the next empty one.
function chooseSlot(slot) {
    resultsState.chosen = (resultsState.chosen === slot) ? null : slot;
    resultsNote('');
    updateResultsModalUI();
}

// A dead heat: the horses that crossed together share one card. The link on the seam under a card (DEAD HEAT) ties
// the card after it to it (the horse there, or the next horse tapped); UNTIE on the merged card undoes the whole dead
// heat. Two or three horses share a position; the record carries them as ties, and La Quiniela's payout pools the
// positions they occupy. resultsState.tied holds the keys of the cards tied with the one before.
function slotKeyAfter(key) {
    const keys = allSlotKeys();
    const i = keys.indexOf(key);
    if (i >= 0 && i + 1 < keys.length) return keys[i + 1];
    return NEXT_SLOT + (isNextSlot(key) ? nextIndex(key) + 1 : 0);
}

// The places grouped by dead heat, in the order of the finish: [['win', 'place'], ['show'], ['next:0']]
function placeGroups() {
    const groups = [];
    allSlotKeys().forEach((key) => {
        if (groups.length && resultsState.tied.includes(key)) groups[groups.length - 1].push(key);
        else groups.push([key]);
    });
    return groups;
}

function tieAfter(key) {
    const after = slotKeyAfter(key);
    if (!resultsState.tied.includes(after)) resultsState.tied.push(after);      // a click that fires twice ties once
    if (!getSlot(after)) resultsState.chosen = after;       // the partner's card takes the next tap
    resultsNote('');
    updateResultsModalUI();
}

function untie(key) {
    const group = placeGroups().find((keys) => keys.includes(key));
    resultsState.tied = resultsState.tied.filter((k) => !group.includes(k));
    resultsNote('');
    updateResultsModalUI();
}

// The x on a filled slot: empty it, and the next horse tap fills it
function clearSlot(slot) {
    if (!getSlot(slot)) return;
    const post = isNextSlot(slot) ? null : resultsPosts[slot];
    setSlot(slot, null);
    if (!isNextSlot(slot)) resultsPosts[slot] = null;
    resultsState.chosen = allSlotKeys().includes(slot) ? slot : null;     // a next card no longer needed goes with its pick
    resultsNote('');
    updateResultsModalUI();
    if (post !== null) {
        ledCall('/api/cup/unlock', { cup: post });
    }
}

// Update modal UI from the state
function updateResultsModalUI() {
    // a tie mark whose leading card is empty is no tie any more
    const before = allSlotKeys();
    resultsState.tied = resultsState.tied.filter((key) => before.indexOf(key) > 0 && !!getSlot(before[before.indexOf(key) - 1]));
    renderResultCards();
    const header = document.getElementById('results-modal-header');
    const subtext = document.getElementById('results-modal-subtitle');
    const confirmSection = document.getElementById('results-confirm-section');
    const slot = activeSlot();
    const keys = allSlotKeys();
    const changing = !!resultsState.chosen && !!getSlot(resultsState.chosen);
    const complete = keys.every((key) => getSlot(key)) && !resultsState.chosen;

    // Heading and subtext: what the next horse tap does
    const heads = {
        win:   { text: 'CHOOSE WINNER', change: 'CHANGE WINNER', color: '#FFD700', sub: 'Tap the 1st place horse' },
        place: { text: 'CHOOSE PLACE',  change: 'CHANGE PLACE',  color: '#C0C0C0', sub: 'Tap the 2nd place horse' },
        show:  { text: 'CHOOSE SHOW',   change: 'CHANGE SHOW',   color: '#CD7F32', sub: 'Tap the 3rd place horse' }
    };
    const headFor = (key) => {
        if (resultsState.tied.includes(key)) {
            const prev = keys[keys.indexOf(key) - 1];
            return { text: 'DEAD HEAT', change: 'DEAD HEAT', color: '#E0E0E0',
                     sub: `Tap the horse that crossed together with ${horseLabel(getSlot(prev))} (${slotName(prev)}).` };
        }
        if (!isNextSlot(key)) return heads[key];
        const skipped = namedFinish().filter((h) => hadNoBets(h)).map((h) => `#${h}`);
        const name = slotName(key);
        return { text: `CHOOSE ${name}`, change: `CHANGE ${name}`, color: '#E0E0E0',
                 sub: `Tap the horse that finished ${name.toLowerCase()}: La Quiniela skips ${skipped.join(' and ')} (no bets at the post).` };
    };
    if (header) {
        if (slot) {
            header.textContent = changing ? headFor(slot).change : headFor(slot).text;
            header.style.color = headFor(slot).color;
        } else {
            header.textContent = 'CHECK THE RESULTS';
            header.style.color = '#FFFFFF';
        }
    }
    if (subtext) {
        if (slot) {
            subtext.textContent = changing
                ? `${slotName(slot)} is ${horseLabel(getSlot(slot))}. Tap the horse that replaces it, or tap ${slotName(slot)} again to leave it.`
                : headFor(slot).sub;
        } else {
            subtext.textContent = keys.length === RESULT_SLOTS.length
                ? 'Tap WIN, PLACE or SHOW to change one, or CONFIRM RESULTS.'
                : 'Tap a place to change it, or CONFIRM RESULTS.';
        }
    }

    // The slot cards: the one the next tap fills is lit
    keys.forEach((key) => {
        const card = document.querySelector(`.result-slot[data-slot="${key}"]`);
        if (!card) return;
        card.classList.toggle('is-active', key === slot);
        card.classList.toggle('is-filled', !!getSlot(key));
        const hint = card.querySelector('.slot-hint');
        if (hint) hint.textContent = (key === slot) ? (getSlot(key) ? 'TAP THE NEW HORSE' : 'TAP A HORSE') : '';
    });

    // Update button states in grid: a placed horse says which slot it is in, a horse
    // nobody bet says NO BETS (from the figures at the post, which do not change while
    // the picker is open)
    const buttons = document.querySelectorAll('.winner-pick-btn');
    buttons.forEach(btn => {
        const horse = parseInt(btn.dataset.horse);
        const where = slotOfHorse(horse);

        // Remove all selection classes
        btn.classList.remove('selected-win', 'selected-place', 'selected-show', 'selected-next', 'is-picked');
        const tag = btn.querySelector('.winner-pick-tag');
        if (tag) tag.textContent = '';

        if (where) {
            btn.classList.add(isNextSlot(where) ? 'selected-next' : `selected-${where}`, 'is-picked');
            if (tag) tag.textContent = slotName(where);
        }

        const noBets = hadNoBets(horse);
        btn.classList.toggle('no-bets', noBets);
        const none = btn.querySelector('.winner-pick-nobets');
        if (none) none.textContent = noBets ? 'NO BETS' : '';
    });

    // The confirm step. The cards are the check (the picks are not listed a second time); over CONFIRM RESULTS one quiet
    // note for the picks nobody bet, shown as soon as WIN, PLACE and SHOW are set (it says why a 4TH card is asked for);
    // the button itself once every place is set, the next finishers' cards too, and none is being changed. After a
    // refusal from pi5 its reason stands over the button, which says NOT SAVED, until the picks change.
    if (confirmSection) {
        const threeSet = RESULT_SLOTS.every((key) => getSlot(key)) && !resultsState.chosen;
        const confirmBtn = document.getElementById('results-confirm-btn');
        const wasShown = confirmSection.style.display === 'block' && !!confirmBtn && confirmBtn.style.display !== 'none';
        confirmSection.style.display = threeSet ? 'block' : 'none';
        // A pick whose cup held no bets at the post is the real finish all the same: La Quiniela skips it for the
        // finisher named after it, La Subasta pays its owner. Said once, the last look before CONFIRM RESULTS
        const skipped = keys.map(getSlot).filter((horse) => horse && hadNoBets(horse)).map((horse) => `#${horse}`);
        const many = skipped.length > 1;
        document.getElementById('results-confirm-notes').textContent = threeSet && skipped.length
            ? `${skipped.join(', ')}: no bets at the post. La Quiniela skips ${many ? 'them' : 'it'}; La Subasta still pays ${many ? 'their owners' : 'its owner'}.`
            : '';
        if (resultsState.refusal && resultsState.refusal.picks !== picksKey()) resultsState.refusal = null;      // the picks changed: it no longer holds
        const refusal = resultsState.refusal ? resultsState.refusal.text : '';
        document.getElementById('results-confirm-refusal').textContent = refusal;
        if (confirmBtn) {
            confirmBtn.style.display = complete ? '' : 'none';
            confirmBtn.classList.toggle('is-refused', !!refusal);
            confirmBtn.textContent = refusal ? 'NOT SAVED' : 'CONFIRM RESULTS';
        }
        // CONFIRM RESULTS has just appeared: on a short screen it can be below the fold (the modal
        // scrolls), so bring it and any warning over it into view
        if (complete && !wasShown && confirmBtn && confirmBtn.scrollIntoView) {
            confirmBtn.scrollIntoView({ block: 'nearest' });
        }
    }
}

// Reset selection
async function resultsReset() {
    // A result already saved (CONFIRM RESULTS pressed before): RESET clears it too, behind a question, so the host
    // can scratch (the field is frozen while a result stands) and enter the results again, here, with the picker
    // still open. pi5 is asked, not the page's memory: a result cleared elsewhere a moment ago asks nothing.
    const saved = await savedResultsOnPi5();
    if (saved) {
        const named = RESULT_SLOTS.map((s) => `${RESULT_SLOT_NAMES[s]} ${horseLabel(saved[s])}`).join(', ');
        const yes = await askFirst('Clear the saved result?',
            `${named}. Clearing ends the race (AFTER PARTY: the TV back to the slideshow, the LEDs off) until you `
            + 'confirm new results; scratch on the LQ admin page in between if you need to.',
            'Clear the result');
        if (!yes) return;
        await clearResults();
        savedOnOpen = null;
    }

    // The page's own state first, in this turn; the LEDs follow
    resultsState = freshResultsState();
    resultsPosts = { win: null, place: null, show: null };

    resultsNote('');
    updateResultsModalUI();

    // Unlock all selected cups, once a tap: a second RESET within MODE_HOLD_MS,
    // with no LED call since the first one's unlock, has nothing more to unlock.
    if (lastReset && ledChain === lastReset.unlock && Date.now() - lastReset.at < MODE_HOLD_MS) return;
    const unlock = ledCall('/api/cup/unlock', { cup: 'ALL' });
    lastReset = { unlock: unlock, at: Date.now() };
    await unlock;
}
let lastReset = null;      // RESET's unlock and when it was asked for

// Close results modal
async function closeResultsModal(keepAnimation = false) {
    stopResultsFieldPoll();
    const modal = document.getElementById('results-modal');
    modal.classList.remove('active');

    // Only unlock cups and stop animation if NOT confirmed
    if (!keepAnimation) {
        // Results another device set while the picker was up are shown now
        if (pendingResults) showResultsRevealModal();

        await ledCall('/api/cup/unlock', { cup: 'ALL' });
        // a result still stands: the mantle back on it; none: off
        if (savedOnOpen) await ledCall('/api/animation/RESULTS_ACTIVE', { win: savedOnOpen.win, place: savedOnOpen.place, show: savedOnOpen.show });
        else await ledCall('/api/command', { command: 'LED:ALL_OFF' });
    }
}

// Confirm and apply results (horse numbers: what La Quiniela's cups and the
// TV board are told; the LED cups were lit by post as they were picked), the
// next finishers with them. pi5 saves them first, whatever the LED wall does,
// and hands it RESULTS:FINALIZE itself (the pill, top left, says what the wall
// is doing). A refusal from pi5 (a horse scratched since the picker opened, a
// finisher it needs) stands beside the button and the field is read again.
async function resultsConfirm() {
    const winHorse = resultsState.win;
    const placeHorse = resultsState.place;
    const showHorse = resultsState.show;
    const after = resultsState.next.slice();
    const named = [winHorse, placeHorse, showHorse].concat(after);
    if (named.some((h) => !h) || new Set(named).size !== named.length) return;
    // the dead heats: each card of more than one horse, the horses in finishing order
    const ties = placeGroups().map((keys) => keys.map(getSlot)).filter((horses) => horses.length > 1);

    // Set flag to skip reveal popup on this device
    justSubmittedResults = true;

    showLoader();
    try {
        // The cups were locked as they were picked: let those calls finish first (not for long: the results
        // are facts about the race, not about the LEDs)
        await ledDrain(3000);

        const response = await fetch('/api/results', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ win: winHorse, place: placeHorse, show: showHorse, next: after, ties: ties })
        });

        const data = await response.json();

        if (data.success) {
            // The results stand either way. The line names the race state they set (WINNER); in red when La
            // Quiniela refused it, with its reason (what the LED wall does with them is the pill's, top left)
            const race = data.race ? { ok: true, ...data.race }
                : { ok: false, error: data.race_error || 'La Quiniela did not answer' };
            showNotification(withRaceState(`Results set: Win=${winHorse}, Place=${placeHorse}, Show=${showHorse}`, race),
                race.ok ? 'success' : 'error');
            document.getElementById('current-mode').textContent = 'RESULTS';
            filterTuningGroups('RESULTS');
            localStorage.setItem('raceResults', JSON.stringify({
                win: winHorse, place: placeHorse, show: showHorse
            }));
            pendingResults = null;           // these are the results now, whatever another device had said
            showResultsBanner(winHorse, placeHorse, showHorse);

            // /api/results set the race state (WINNER, with the results): the page knows it now
            if (data.race) {
                raceModeInfo = data.race;
            }

            // The winners' chase blending into the heartbeat (RESULTS:FINALIZE)
            // was sent by /api/results itself

            closeResultsModal(true);

            hideLoader();
        } else {
            hideLoader(true); // Hide immediately on error
            justSubmittedResults = false;
            // pi5 refused: its reason stands over CONFIRM RESULTS, which says NOT SAVED until the picks change, and
            // the field is read again at once; a horse scratched since the picker opened leaves its slot and the
            // grid, with its own line under the heading
            resultsState.refusal = { text: data.error || data.response || 'pi5 refused the results', picks: picksKey() };
            await loadQuinielaField();
            updateResultsModalUI();
            document.getElementById('results-confirm-btn').scrollIntoView({ block: 'nearest' });      // the reason pushed it down
        }
    } catch (error) {
        hideLoader(true); // Hide immediately on error
        justSubmittedResults = false;
        console.error('[CONFIRM] Error setting results:', error);
        showNotification('Connection error', 'error');
    }
}

// Saddle cloth colors and text colors: 1-20 the Official Racing Colors, 21-24
// the also-eligibles' placeholders (the same as the cups and the TV board show)
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
    20: { bg: '#FF00FF', text: '#FFCD00' },  // Fuchsia, yellow
    21: { bg: '#FFDAB9', text: '#000000' },  // Peach, black
    22: { bg: '#008080', text: '#FFFFFF' },  // Teal, white
    23: { bg: '#808000', text: '#FFFFFF' },  // Olive, white
    24: { bg: '#2F4F4F', text: '#FFFFFF' }   // Slate, white
};

// Create saddle cloth with flat color and bold number
function createSaddleCloth(number) {
    const cloth = document.createElement('div');
    cloth.className = 'saddle-cloth';
    
    // Get colors for this position
    const colors = SADDLE_CLOTHS[number] || { bg: '#808080', text: '#FFFFFF' };
    
    // Apply styling
    cloth.style.backgroundColor = colors.bg;
    cloth.style.color = colors.text;
    
    // Zero-pad to 2 digits and display
    const numStr = String(number).padStart(2, '0');
    cloth.textContent = numStr;
    
    return cloth;
}

// Create empty tile (all dots unlit)
function createEmptyTile() {
    const tile = document.createElement('div');
    tile.className = 'dot-digit';
    
    // Generate 7 rows × 5 columns = 35 unlit dots
    for (let row = 0; row < 7; row++) {
        for (let col = 0; col < 5; col++) {
            const dot = document.createElement('div');
            dot.className = 'dot';  // unlit
            tile.appendChild(dot);
        }
    }
    return tile;
}

// Create dot matrix text (letters and numbers) with optional padding and fixed length
function createDotMatrixText(text, padLeft = 0, padRight = 0, fixedLength = null) {
    const container = document.createElement('div');
    container.className = 'dot-matrix-text';
    
    // If fixedLength specified, pad or truncate text to exactly that many characters
    let processedText = text.toUpperCase();
    if (fixedLength !== null) {
        // Pad with spaces or truncate to fixed length
        if (processedText.length < fixedLength) {
            processedText = processedText.padEnd(fixedLength, ' ');
        } else if (processedText.length > fixedLength) {
            processedText = processedText.substring(0, fixedLength);
        }
        // Split into fixed number of tiles
        const chars = processedText.split('');
        chars.forEach(char => {
            container.appendChild(createDotDigit(char));
        });
    } else {
        // Add empty tiles on the left
        for (let i = 0; i < padLeft; i++) {
            container.appendChild(createEmptyTile());
        }
        
        // Convert text to uppercase and split into characters
        const chars = processedText.split('');
        
        // Create dot digit for each character
        chars.forEach(char => {
            container.appendChild(createDotDigit(char));
        });
        
        // Add empty tiles on the right
        for (let i = 0; i < padRight; i++) {
            container.appendChild(createEmptyTile());
        }
    }
    
    return container;
}

// Enable or disable race-phase panels when results are set (the first 3 panels: PRE-RACE, BETTING, DURING;
// the Results panel stays enabled). AUTO holds them all whatever this says (updatePanelButtons).
function setRaceComplete(isComplete) {
    raceComplete = !!isComplete;
    updatePanelButtons();
}

// The tote's name column holds 15 tiles. A longer name is drawn whole and
// the row of tiles scaled to the same width, so a name is never cut short.
const TOTE_NAME_TILES = 15;

function createToteName(name) {
    const text = String(name).toUpperCase();
    if (text.length <= TOTE_NAME_TILES) {
        return createDotMatrixText(text, 0, 0, TOTE_NAME_TILES);
    }
    const fit = document.createElement('div');
    fit.className = 'results-name-fit';
    const tiles = createDotMatrixText(text, 0, 0, text.length);
    tiles.style.transformOrigin = 'left center';
    tiles.style.transform = `scale(${(TOTE_NAME_TILES / text.length).toFixed(4)})`;
    fit.appendChild(tiles);
    return fit;
}

// What the tote shows: {win, place, show}, horse numbers, or null
let bannerResults = null;

// Paint the three rows: label, saddle cloth, the horse's name from La
// Quiniela's names store (HORSE n where it has none)
function renderResultsBanner(win, place, show) {
    const rows = [
        ['win',   '  WIN', win],     // 5 tiles total (2 spaces + WIN)
        ['place', 'PLACE', place],   // 5 tiles total (PLACE fits exactly)
        ['show',  ' SHOW', show]     // 5 tiles total (1 space + SHOW)
    ];
    rows.forEach(([key, label, horse]) => {
        const labelEl = document.getElementById(`banner-${key}-label`);
        labelEl.innerHTML = '';
        labelEl.appendChild(createDotMatrixText(label, 0, 0, 5));

        const numberEl = document.getElementById(`banner-${key}-number`);
        numberEl.innerHTML = '';
        numberEl.appendChild(createSaddleCloth(horse));

        const nameEl = document.getElementById(`banner-${key}-name`);
        nameEl.innerHTML = '';
        nameEl.appendChild(createToteName(horseDisplayName(horse)));
    });
}

// Show results banner with saddle cloths and dot matrix text
function showResultsBanner(win, place, show) {
    const banner = document.getElementById('results-banner');

    bannerResults = { win: win, place: place, show: show };
    renderResultsBanner(win, place, show);      // at once, with the names on hand
    banner.style.display = 'block';

    // Disable race-phase buttons once results are set
    setRaceComplete(true);

    // ...and again with La Quiniela's names as they are now
    loadQuinielaField().then(() => {
        const b = bannerResults;
        if (b && b.win === win && b.place === place && b.show === show) {
            renderResultsBanner(win, place, show);
        }
    });
}

// Hide results banner
function hideResultsBanner() {
    bannerResults = null;
    const banner = document.getElementById('results-banner');
    banner.style.display = 'none';
}

// Clear results (call backend API to delete results.json and turn off LEDs)
async function clearResults() {
    try {
        const response = await fetch('/api/results/clear', {
            method: 'POST'
        });
        
        const data = await response.json();
        
        if (data.success) {
            // Remove from localStorage
            localStorage.removeItem('raceResults');
            // Hide banner
            hideResultsBanner();
            // Re-enable race-phase buttons
            setRaceComplete(false);
            showNotification('Results cleared', 'success');
        } else {
            showNotification(`Error clearing results: ${data.error}`, 'error');
        }
    } catch (error) {
        console.error('Error clearing results:', error);
        showNotification('Connection error', 'error');
    }
}

// SSE connection for real-time results
let resultsEventSource = null;
let pendingResults = null;

// Connect to SSE stream for real-time results
function connectResultsStream() {
    if (resultsEventSource) {
        resultsEventSource.close();
    }
    
    resultsEventSource = new EventSource('/api/results/stream');
    
    resultsEventSource.addEventListener('results', function(e) {
        const data = JSON.parse(e.data);

        if (justSubmittedResults) {
            justSubmittedResults = false;
            return; // Skip reveal popup — this device submitted the results
        }
        
        // Show dramatic reveal popup for other devices
        pendingResults = data;
        if (resultsModalOpen()) {
            // The picker is up: the popup would take a tap meant for a horse, so it waits until the picker
            // closes (closeResultsModal), and the host is told now
            showNotification('Results were set on another device', 'error');
            return;
        }
        showResultsRevealModal();
    });
    
    resultsEventSource.onerror = function(e) {
        console.error('[SSE] Error:', e);
        // Reconnect after 5 seconds
        setTimeout(connectResultsStream, 5000);
    };
}

// The result pi5 holds now ({win, place, show, next}), or null: what RESET in the picker asks about before it
// clears it. pi5 not answering reads as nothing to clear.
async function savedResultsOnPi5() {
    try {
        const response = await fetch('/api/results', { cache: 'no-store' });
        const data = await response.json();
        return (data && data.success && data.results) ? data.results : null;
    } catch (error) {
        return null;
    }
}

// Load results from server
async function loadResultsFromServer() {
    try {
        const response = await fetch('/api/results');
        const data = await response.json();
        
        if (data.success && data.results) {
            showResultsBanner(data.results.win, data.results.place, data.results.show);
        }
    } catch (error) {
        console.error('Error loading results from server:', error);
    }
}

// Show results reveal modal
function showResultsRevealModal() {
    const modal = document.getElementById('results-reveal-modal');
    modal.classList.add('active');
}

// Reveal winners - close modal and show banner
function revealWinners() {
    const modal = document.getElementById('results-reveal-modal');
    modal.classList.remove('active');
    
    if (pendingResults) {
        showResultsBanner(pendingResults.win, pendingResults.place, pendingResults.show);
        pendingResults = null;
    }
}

// =====================================================================
// Slide-out Drawer
// =====================================================================

function toggleDrawer() {
    const drawer = document.getElementById('drawer');
    const overlay = document.getElementById('drawer-overlay');
    if (!drawer) return;

    const isOpen = drawer.classList.contains('open');
    if (isOpen) {
        closeDrawer();
    } else {
        openDrawer();
    }
}

function openDrawer() {
    const drawer = document.getElementById('drawer');
    const overlay = document.getElementById('drawer-overlay');
    if (drawer) drawer.classList.add('open');
    if (overlay) overlay.classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeDrawer() {
    const drawer = document.getElementById('drawer');
    const overlay = document.getElementById('drawer-overlay');
    if (drawer) drawer.classList.remove('open');
    if (overlay) overlay.classList.remove('active');
    document.body.style.overflow = '';
}

// Opened from the iPad's Home Screen, the page runs full screen: no tabs, no
// second window, so a link to the LQ admin page that opens a new tab would
// leave it. There a link marked data-in-place-standalone loses its target and
// opens in place (the admin page's link back does the same), replacing this
// page rather than stacking on it: a page kept for Back keeps its result
// streams open, and after a few trips between the two pages the browser has
// no connection to pi5 left. In a browser tab it keeps its target, as built.
function linksInPlaceWhenStandalone() {
    const standalone = navigator.standalone === true ||
        (typeof window.matchMedia === 'function' && window.matchMedia('(display-mode: standalone)').matches);
    if (!standalone) return;
    document.querySelectorAll('a[data-in-place-standalone]').forEach((a) => {
        a.removeAttribute('target');
        a.removeAttribute('rel');
        a.addEventListener('click', (event) => {
            event.preventDefault();
            location.replace(a.href);
        });
    });
}

// =====================================================================
// Animation Tuning Modal
// =====================================================================

let tuningParams = {};  // current param values, keyed by param name

// Open modal — fetch params from ESP32 and populate sliders
async function openTuningModal() {
    document.getElementById('tuning-modal').classList.add('active');

    // Get current running animation from mode display
    const currentMode = document.getElementById('current-mode')?.textContent.trim().toUpperCase() || '';

    // Filter groups based on current animation
    filterTuningGroups(currentMode);

    await loadTuningParams();
}

function filterTuningGroups(currentMode) {
    const groups = document.querySelectorAll('.tuning-group');
    const label = document.getElementById('tuning-context-label');

    // Animation mode → display name mapping
    const modeNames = {
        'GATES_BURST':        'Gates Burst',
        'AT_THE_GATE':        'At The Gate',
        'RESULTS_ENTRY':      'Results Entry',
        'HEARTBEAT_COOLDOWN': 'Heartbeat Cooldown',
        'COOLDOWN':           'Heartbeat Cooldown',
        'BETTING_60':         'Betting 60 Min',
        'BETTING_30':         'Betting 30 Min',
        'FINAL_CALL':         'Final Call',
        'CHAOS':              null,
        'FINISH':             null,
        'WELCOME':            null,
        'SILKS':              null,
        'IDLE':               null,
        'STANDBY':            null,
    };

    const isKnown = modeNames[currentMode] !== undefined;

    groups.forEach(group => {
        const anims = (group.dataset.anims || 'ALL').split(',').map(a => a.trim());
        const show = !isKnown || anims.includes('ALL') || anims.includes(currentMode);
        group.style.display = show ? '' : 'none';
    });

    // Count how many non-ALL groups are visible
    const tunableGroups = Array.from(groups).filter(g => {
        const anims = (g.dataset.anims || 'ALL').split(',').map(a => a.trim());
        return !anims.includes('ALL') && (anims.includes(currentMode));
    });

    const tuningBtn = document.querySelector('.btn-footer[onclick="openTuningModal()"]');
    if (tuningBtn) {
        if (isKnown && tunableGroups.length === 0) {
            tuningBtn.disabled = true;
            tuningBtn.style.opacity = '0.4';
            tuningBtn.style.cursor = 'not-allowed';
        } else {
            tuningBtn.disabled = false;
            tuningBtn.style.opacity = '';
            tuningBtn.style.cursor = '';
        }
    }

    if (label) {
        label.textContent = isKnown
            ? `Showing params for: ${modeNames[currentMode]}`
            : currentMode && currentMode !== 'STANDBY'
                ? `Showing all params (${currentMode} has no specific tuning)`
                : 'Showing all params';
    }
}

function closeTuningModal() {
    document.getElementById('tuning-modal').classList.remove('active');
}

// Fetch all params from Flask/ESP32 and populate sliders
async function loadTuningParams() {
    try {
        const response = await fetch('/api/params');
        const data = await response.json();

        if (data.success && data.params) {
            tuningParams = data.params;
            populateTuningSliders();
            if (data.cached) {
                showNotification('ESP32 offline — showing cached values', 'info');
            }
        } else {
            showNotification('Could not load params', 'error');
        }
    } catch (error) {
        console.error('Error loading tuning params:', error);
        showNotification('Error loading params', 'error');
    }
}

// Populate all sliders with current param values
function populateTuningSliders() {
    const sliders = document.querySelectorAll('.tuning-slider');
    sliders.forEach(slider => {
        const key = slider.dataset.key;
        if (key && tuningParams[key] !== undefined) {
            slider.value = tuningParams[key];
            updateTuningValueDisplay(slider);
        }
    });
}

// Update the value display span next to a slider
function updateTuningValueDisplay(slider) {
    const valueSpan = slider.nextElementSibling;
    if (valueSpan && valueSpan.classList.contains('tuning-value')) {
        valueSpan.textContent = slider.value;
    }
}

// Push a single param change live to ESP32
async function pushTuningParam(key, value) {
    try {
        const response = await fetch('/api/params', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key, value: parseInt(value) })
        });
        const data = await response.json();
        if (!data.success) {
            console.warn(`Failed to set ${key}=${value}:`, data);
        }
    } catch (error) {
        console.error(`Error setting param ${key}:`, error);
    }
}

// Save current params to ESP32 EEPROM
async function saveTuningParams() {
    try {
        const response = await fetch('/api/params/save', { method: 'POST' });
        const data = await response.json();
        if (data.success) {
            showNotification('Params saved to ESP32 EEPROM', 'success');
        } else {
            showNotification('Save failed', 'error');
        }
    } catch (error) {
        console.error('Error saving params:', error);
        showNotification('Save error', 'error');
    }
}

// Reset params to defaults on ESP32 and reload sliders
async function resetTuningParams() {
    try {
        const response = await fetch('/api/params/reset', { method: 'POST' });
        const data = await response.json();
        if (data.success) {
            showNotification('Params reset to defaults', 'success');
            await loadTuningParams();  // Reload sliders with default values
        } else {
            showNotification('Reset failed', 'error');
        }
    } catch (error) {
        console.error('Error resetting params:', error);
        showNotification('Reset error', 'error');
    }
}

// Wire up slider input events — runs once on DOMContentLoaded
function initTuningSliders() {
    const sliders = document.querySelectorAll('.tuning-slider');
    sliders.forEach(slider => {
        // Update display while dragging (no ESP32 call yet)
        slider.addEventListener('input', () => {
            updateTuningValueDisplay(slider);
        });

        // Push to ESP32 on release
        slider.addEventListener('change', () => {
            const key = slider.dataset.key;
            const value = slider.value;
            tuningParams[key] = parseInt(value);
            pushTuningParam(key, value);
        });
    });
}

// Animations Modal - Open
function openAnimationsModal() {
    const modal = document.getElementById('animations-modal');
    // Clear any previous active state on anim buttons
    document.querySelectorAll('.anim-btn').forEach(btn => btn.classList.remove('active'));
    modal.classList.add('active');
}

// Animations Modal - Close
function closeAnimationsModal() {
    const modal = document.getElementById('animations-modal');
    modal.classList.remove('active');
}

// =====================================================================
// Animation Library Modal
// =====================================================================

let animLibRegistry = {};       // full registry from Flask
let animLibAssignments = {};    // current slot → animation assignments
let animLibPreviewSlot = null;  // slot ID currently being previewed
let animLibPriorMode = null;    // animation running before modal opened

async function openAnimLibModal() {
    // Capture current mode BEFORE dimming — read from hidden span
    const modeEl = document.getElementById('current-mode');
    animLibPriorMode = modeEl ? modeEl.textContent.trim().toUpperCase() : 'STANDBY';

    // Sanitize — if mode is unknown or disconnected, restore to STANDBY
    const knownModes = [
        'WELCOME','STANDBY','TEST','BETTING_60','BETTING_30','FINAL_CALL',
        'AT_THE_GATE','GATES_BURST','CHAOS','CHAOS_CLASSIC','FINISH',
        'RESULTS_ENTRY','HEARTBEAT_COOLDOWN','SOLAR_FLARE','BREATHE_TOGETHER',
        'RIPPLE_OUT','STARFIELD','MONEY_RAIN','TENSION_BUILD',
        'HEARTBEAT_STACK','COUNTDOWN_SPIRAL'
    ];
    if (!knownModes.includes(animLibPriorMode)) {
        animLibPriorMode = 'STANDBY';
    }

    // Dim cups
    try {
        await fetch('/api/animation/STANDBY', { method: 'POST' });
    } catch(e) {}

    document.getElementById('anim-lib-modal').classList.add('active');
    await loadAnimLib();
}

function closeAnimLibModal() {
    document.getElementById('anim-lib-modal').classList.remove('active');

    // Clear all preview button states
    document.querySelectorAll('.anim-lib-preview-btn.active').forEach(btn => {
        btn.classList.remove('active');
        btn.textContent = '▶ Preview';
    });

    // Clear preview label
    const label = document.getElementById('anim-lib-preview-label');
    if (label) label.textContent = '';

    // Restore prior animation
    const restore = animLibPriorMode || 'STANDBY';
    fetch(`/api/animation/${restore}`, { method: 'POST' }).catch(() => {});

    animLibPreviewSlot = null;
    animLibPriorMode = null;
}

// Load registry + assignments from Flask
async function loadAnimLib() {
    try {
        const response = await fetch('/api/animations/registry');
        const data = await response.json();

        if (data.success) {
            animLibRegistry = data.registry;
            animLibAssignments = data.assignments;
            renderAnimLibSlots();
        } else {
            showNotification('Could not load animation library', 'error');
        }
    } catch (error) {
        console.error('Error loading animation library:', error);
        showNotification('Error loading animation library', 'error');
    }
}

// Render all slot rows from registry
function renderAnimLibSlots() {
    const states = (animLibRegistry.race_states || []);
    const animations = (animLibRegistry.animations || {});

    states.forEach(state => {
        const container = document.getElementById(`slots-${state.id}`);
        if (!container) return;

        container.innerHTML = '';

        state.slots.forEach(slot => {
            const currentAnim = animLibAssignments[slot.id] || slot.default;

            // Slot row
            const row = document.createElement('div');
            row.className = 'anim-lib-slot-row';

            // Label
            const label = document.createElement('span');
            label.className = 'anim-lib-slot-label';
            label.textContent = slot.label;

            // Dropdown
            const select = document.createElement('select');
            select.className = 'anim-lib-select';
            select.dataset.slotId = slot.id;

            // Populate options — filter to relevant category + ALL
            Object.entries(animations).forEach(([key, anim]) => {
                const option = document.createElement('option');
                option.value = key;
                option.textContent = anim.name;
                if (key === currentAnim) option.selected = true;
                select.appendChild(option);
            });

            // Update description on change
            select.addEventListener('change', () => {
                animLibAssignments[slot.id] = select.value;
                updateSlotDescription(slot.id, select.value);
            });

            // Preview button
            const previewBtn = document.createElement('button');
            previewBtn.className = 'anim-lib-preview-btn';
            previewBtn.textContent = '▶ Preview';
            previewBtn.dataset.slotId = slot.id;
            previewBtn.addEventListener('click', () => {
                previewAnimation(slot.id, select.value, previewBtn);
            });

            row.appendChild(label);
            row.appendChild(select);
            row.appendChild(previewBtn);

            // Description row
            const desc = document.createElement('div');
            desc.className = 'anim-lib-description';
            desc.id = `anim-desc-${slot.id}`;
            desc.textContent = animations[currentAnim]?.description || '';

            container.appendChild(row);
            container.appendChild(desc);
        });
    });
}

// Update description text when dropdown changes
function updateSlotDescription(slotId, animKey) {
    const desc = document.getElementById(`anim-desc-${slotId}`);
    if (desc) {
        const anim = (animLibRegistry.animations || {})[animKey];
        desc.textContent = anim?.description || '';
    }
}

async function previewAnimation(slotId, animKey, btnElement) {
    const isCurrentlyPreviewing = btnElement.classList.contains('active');

    // Clear all active preview buttons
    document.querySelectorAll('.anim-lib-preview-btn.active').forEach(btn => {
        btn.classList.remove('active');
        btn.textContent = '▶ Preview';
    });

    // Clear preview label
    const label = document.getElementById('anim-lib-preview-label');

    if (isCurrentlyPreviewing) {
        // User hit Stop — go dark, don't restore prior animation yet
        animLibPreviewSlot = null;
        if (label) label.textContent = '';
        try {
            await fetch('/api/animation/STANDBY', { method: 'POST' });
        } catch(e) {}
        return;
    }

    // Start new preview
    animLibPreviewSlot = slotId;
    btnElement.classList.add('active');
    btnElement.textContent = '■ Stop';

    const animName = (animLibRegistry.animations || {})[animKey]?.name || animKey;
    if (label) label.textContent = `Previewing: ${animName}`;

    try {
        await fetch(`/api/animation/${animKey}`, { method: 'POST' });
    } catch(e) {
        showNotification('Could not preview animation', 'error');
    }
}

// Save current assignments to Flask
async function animLibSave() {
    try {
        const response = await fetch('/api/animations/assignments', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(animLibAssignments)
        });
        const data = await response.json();

        if (data.success) {
            showNotification('Animation assignments saved!', 'success');
        } else {
            showNotification('Save failed', 'error');
        }
    } catch (error) {
        console.error('Error saving assignments:', error);
        showNotification('Save error', 'error');
    }
}

// Reset assignments to registry defaults
async function animLibReset() {
    const registry = animLibRegistry;
    const states = registry.race_states || [];

    // Rebuild defaults from registry
    states.forEach(state => {
        state.slots.forEach(slot => {
            animLibAssignments[slot.id] = slot.default;
        });
    });

    renderAnimLibSlots();
    showNotification('Reset to defaults', 'success');
}

// Animations Modal - Trigger animation and close
async function triggerAnimation(animName, buttonElement) {
    if (raceControlMode === 'auto') {
        showNotification('Switch to MANUAL to control animations', 'error');
        return;
    }
    // Clear all highlights across panels and modal
    clearAllActiveButtons();
    // Highlight selected button
    buttonElement.classList.add('active');

    // /api/animation/...: pi5 hands it to the wall's worker and answers at once
    try {
        showLoader();
        await fetch(`/api/animation/${animName}`, { method: 'POST' });
        showNotification(`Animation: ${animName}`, 'success');
        document.getElementById('current-mode').textContent = animName;
        filterTuningGroups(animName.toUpperCase());
        hideLoader();
    } catch (error) {
        hideLoader(true);
        console.error('Error triggering animation:', error);
        showNotification('Connection error', 'error');
    }

    // Close modal after selection
    closeAnimationsModal();
}

// Animations Modal - Trigger LED command (ALL_ON / ALL_OFF) and close
async function triggerAnimCommand(command, buttonElement) {
    if (raceControlMode === 'auto') {
        showNotification('Switch to MANUAL to control animations', 'error');
        return;
    }
    // Clear all highlights across panels and modal
    clearAllActiveButtons();
    // Highlight selected button
    buttonElement.classList.add('active');

    try {
        showLoader();
        await fetch('/api/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: command })
        });
        showNotification(`Command: ${command}`, 'success');
        document.getElementById('current-mode').textContent = command.replace('LED:', '');
        filterTuningGroups(command.replace('LED:', '').toUpperCase());
        hideLoader();
    } catch (error) {
        hideLoader(true);
        console.error('Error sending command:', error);
        showNotification('Connection error', 'error');
    }

    // Close modal after selection
    closeAnimationsModal();
}

// Close modal when clicking outside
window.onclick = function(event) {
    const resultsModal = document.getElementById('results-modal');
    const animationsModal = document.getElementById('animations-modal');

    // Close results modal if clicking outside, while nothing is picked: a stray tap on the dark edge of the
    // screen must not throw the picks away (CANCEL and the x are there to leave on purpose)
    if (event.target === resultsModal && !resultsPicksMade()) {
        closeResultsModal();
    }

    // Close animations modal if clicking outside
    if (event.target === animationsModal) {
        closeAnimationsModal();
    }
}

// Handle splash screen fade-out
function hideSplashScreen() {
    const splash = document.getElementById('splash-screen');
    if (splash) {
        // Add fade-out class to trigger CSS animation
        splash.classList.add('fade-out');
        
        // Remove from DOM after animation completes
        setTimeout(() => {
            splash.style.display = 'none';
        }, 500); // Match CSS animation duration
    }
}

// Toggle fullscreen mode
function toggleFullscreen() {
    const elem = document.documentElement;
    
    if (!document.fullscreenElement && !document.webkitFullscreenElement) {
        // Enter fullscreen
        if (elem.requestFullscreen) {
            elem.requestFullscreen();
        } else if (elem.webkitRequestFullscreen) { // Safari
            elem.webkitRequestFullscreen();
        }
    } else {
        // Exit fullscreen
        if (document.exitFullscreen) {
            document.exitFullscreen();
        } else if (document.webkitExitFullscreen) { // Safari
            document.webkitExitFullscreen();
        }
    }
}

// Update fullscreen icon when fullscreen state changes
function updateFullscreenIcon() {
    const icon = document.getElementById('fullscreen-icon');
    if (document.fullscreenElement || document.webkitFullscreenElement) {
        icon.textContent = '⛶'; // Exit fullscreen icon (same icon works for both)
    } else {
        icon.textContent = '⛶'; // Enter fullscreen icon
    }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    console.log('DDM Horse Dashboard initialized');
    
    // Race control toggle + state polling
    initRaceControlToggle();
    startRaceStatePolling();

    // The one race state (pi5's), now and every 5 s: La Quiniela's admin
    // page or another dashboard may have set it
    pollRaceMode();
    setInterval(pollRaceMode, 5000);
    
    // BUG FIX #1: Clear localStorage on page load to prevent persistence across server restarts
    localStorage.removeItem('raceResults');
    
    // Hide splash screen after 2 seconds
    setTimeout(hideSplashScreen, 2000);
    
    // Update clock immediately and every second
    updateClock();
    setInterval(updateClock, 1000);

    initTuningSliders();

    document.getElementById('footer-year').textContent = new Date().getFullYear();

    const copyrightYear = document.getElementById('copyright-year');
    if (copyrightYear) copyrightYear.textContent = new Date().getFullYear();

    // Close drawer on Escape key
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeDrawer();
        }
    });

    // Pre-populate ESP32 IP config field
    loadEsp32Config();
    
    // Connect to SSE stream for real-time results
    connectResultsStream();
    
    // La Quiniela's names, for the results tote and the pickers
    loadQuinielaField();

    // Full screen from the Home Screen: the link to the admin page opens in place
    linksInPlaceWhenStandalone();

    // Load initial results from server
    loadResultsFromServer();
    
    // The two devices' health, now and every 5 s
    pollDevices();
    setInterval(pollDevices, 5000);

    // Listen for fullscreen changes
    document.addEventListener('fullscreenchange', updateFullscreenIcon);
    document.addEventListener('webkitfullscreenchange', updateFullscreenIcon); // Safari

    // Keep spectator preview indicator in sync with current-mode text
    const modeEl = document.getElementById('current-mode');
    if (modeEl) {
        const modeObserver = new MutationObserver(function () {
            updateSpectatorIndicator(modeEl.textContent.trim());
        });
        modeObserver.observe(modeEl, { childList: true, characterData: true, subtree: true });
    }
});

// =====================================================================
// Spectator Preview — state indicator on admin dashboard
// =====================================================================

function updateSpectatorIndicator(modeText) {
    var el = document.getElementById('spectator-state-indicator');
    if (!el) return;

    // Map admin mode strings to spectator state display
    var MODE_TO_SPECTATOR = {
        'STANDBY':            { text: '\u25CF DORMANT',      cls: 'dormant' },
        'IDLE':               { text: '\u25CF DORMANT',      cls: 'dormant' },
        'WELCOME':            { text: '\u25CF ENTRIES',       cls: 'betting' },
        'BETTING_60':         { text: '\u25CF BETTING OPEN',  cls: 'betting' },
        'BETTING_30':         { text: '\u25CF BETTING OPEN',  cls: 'betting' },
        'FINAL_CALL':         { text: '\u25CF FINAL CALL',    cls: 'final-call' },
        'RACE_START':         { text: '\u25CF AT THE POST',   cls: 'racing' },
        'CHAOS':              { text: '\u25CF RUNNING',       cls: 'racing' },
        'FINISH':             { text: '\u25CF FINISHED',      cls: 'official' },
        'RESULTS':            { text: '\u25CF OFFICIAL',      cls: 'official' },
        'HEARTBEAT_COOLDOWN': { text: '\u25CF OFFICIAL',      cls: 'official' },
        'COOLDOWN':           { text: '\u25CF OFFICIAL',      cls: 'official' },
        'TEST':               { text: '\u25CF DORMANT',      cls: 'dormant' },
        'STOPPED':            { text: '\u25CF DORMANT',      cls: 'dormant' }
    };

    var info = MODE_TO_SPECTATOR[modeText] || { text: '\u25CF DORMANT', cls: 'dormant' };
    el.textContent = info.text;
    el.className = 'spectator-state-dot ' + info.cls;
}
