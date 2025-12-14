// ddm_control.js - Dashboard control JavaScript

// Track active button
let activeButton = null;

// Track loading start time
let loaderStartTime = null;
const MIN_LOADER_DISPLAY_TIME = 2000; // 2 seconds minimum

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

// Dot matrix patterns (5×7 grid: 5 columns × 7 rows) - 1 = lit, 0 = unlit
// Slashed zero distinguishes 0 from O (authentic tote board style)
const dotPatterns = {
    '0': [0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E],  // Slashed zero with diagonal
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
    'C': [0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E],
    'E': [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F],
    'H': [0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
    'I': [0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E],
    'L': [0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F],
    'M': [0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11],
    'N': [0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11],
    'O': [0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
    'P': [0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10],
    'R': [0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11],
    'S': [0x0E, 0x11, 0x10, 0x0E, 0x01, 0x11, 0x0E],
    'W': [0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A],
    ' ': [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]  // Space
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

// Update dot matrix tote board clock
function updateClock() {
    const now = new Date();
    let hours = now.getHours();
    const minutes = now.getMinutes();
    const ampm = hours >= 12 ? 'PM' : 'AM';
    
    hours = hours % 12;
    hours = hours ? hours : 12;
    
    const timeStr = String(hours).padStart(2, '0') + String(minutes).padStart(2, '0');
    const display = document.getElementById('tote-display');
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

// Track ESP32 connection state
let esp32WasOnline = false;

// Track ESP32 connection info
let esp32Info = {
    online: false,
    ip: null
};

// Check ESP32 status
async function checkESP32Status() {
    try {
        const response = await fetch('/api/ping');
        const data = await response.json();
        
        const isOnline = data.success;
        esp32Info.online = isOnline;
        
        if (isOnline) {
            esp32WasOnline = true;
        } else {
            // If ESP32 just went offline, clear active buttons
            if (esp32WasOnline) {
                clearActiveButton();
                document.getElementById('current-mode').textContent = 'DISCONNECTED';
                showNotification('ESP32 disconnected - animation stopped', 'error');
                esp32WasOnline = false;
            }
        }
        
        // Update device count (ESP32 devices only - 0 or 1)
        const deviceCount = isOnline ? 1 : 0;
        document.getElementById('device-count').textContent = deviceCount;
        document.getElementById('device-plural').textContent = deviceCount === 1 ? '' : 's';
        
        // Update device modal list
        updateDeviceList();
        
    } catch (error) {
        console.error('Error checking ESP32 status:', error);
        esp32Info.online = false;
        
        // If ESP32 just went offline, clear active buttons
        if (esp32WasOnline) {
            clearActiveButton();
            document.getElementById('current-mode').textContent = 'DISCONNECTED';
            showNotification('ESP32 disconnected - animation stopped', 'error');
            esp32WasOnline = false;
        }
        
        // Update device count
        document.getElementById('device-count').textContent = '0';
        document.getElementById('device-plural').textContent = 's';
        
        // Update device modal list
        updateDeviceList();
    }
}

// Get system status (ESP32 IP)
async function getSystemStatus() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();
        
        if (data.esp32_ip) {
            esp32Info.ip = data.esp32_ip;
            updateDeviceList();
        }
    } catch (error) {
        console.error('Error getting system status:', error);
    }
}

// Update device list in modal
function updateDeviceList() {
    const deviceList = document.getElementById('device-list');
    const noDevices = document.getElementById('no-devices');
    
    if (esp32Info.online) {
        // Show device, hide "no devices" message
        deviceList.style.display = 'flex';
        noDevices.style.display = 'none';
        
        // Build device item
        deviceList.innerHTML = `
            <div class="device-item">
                <span class="device-icon">🔌</span>
                <span class="device-name">ESP32 LED Controller</span>
                <span class="device-status-value connected">ONLINE</span>
            </div>
            <div class="device-item">
                <span class="device-icon">💡</span>
                <span class="device-name">IP Address</span>
                <span class="device-status-value">${esp32Info.ip || '---'}</span>
            </div>
        `;
    } else {
        // Hide device list, show "no devices" message
        deviceList.style.display = 'none';
        noDevices.style.display = 'block';
    }
}

// Toggle device status modal
function toggleDeviceModal() {
    updateDeviceList(); // Refresh before showing
    const modal = document.getElementById('device-modal');
    modal.classList.add('active');
}

// Close device status modal
function closeDeviceModal() {
    const modal = document.getElementById('device-modal');
    modal.classList.remove('active');
}

// Send command to ESP32
async function sendCommand(command, buttonElement, toggle = false) {
    // If toggle mode and button is already active, turn it off
    if (toggle && activeButton === buttonElement) {
        showLoader();
        try {
            // Send command to stop (turn off LEDs)
            const response = await fetch('/api/command', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ command: 'LED:ALL_OFF' })
            });
            
            const data = await response.json();
            
            if (data.success) {
                showNotification('Test stopped', 'success');
                document.getElementById('current-mode').textContent = 'IDLE';
                clearActiveButton();
                await checkESP32Status();
                hideLoader();
            } else {
                hideLoader(true); // Hide immediately on error
                showNotification(`Error: ${data.response}`, 'error');
            }
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
        const response = await fetch('/api/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ command: command })
        });
        
        const data = await response.json();
        
        if (data.success) {
            showNotification(`Command sent: ${command}`, 'success');
            document.getElementById('current-mode').textContent = command.split(':')[0];
            
            // If toggle mode, set button as active
            if (toggle) {
                setActiveButton(buttonElement);
            }
            await checkESP32Status();
            hideLoader();
        } else {
            hideLoader(true); // Hide immediately on error
            showNotification(`Error: ${data.response}`, 'error');
        }
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

// Send animation command with toggle functionality
async function sendAnimation(animName, buttonElement) {
    // One-shot animations that don't stay active
    const oneShotAnimations = ['IDLE'];
    
    if (oneShotAnimations.includes(animName)) {
        // Just flash the button and execute without toggle
        flashButton(buttonElement);
        showLoader();
        try {
            const response = await fetch(`/api/animation/${animName}`, {
                method: 'POST'
            });
            
            const data = await response.json();
            
            if (data.success) {
                showNotification(`Animation: ${animName}`, 'success');
                document.getElementById('current-mode').textContent = animName;
                // Clear any active button
                clearActiveButton();
                await checkESP32Status();
                hideLoader();
            } else {
                hideLoader(true); // Hide immediately on error
                showNotification(`Error: ${data.response}`, 'error');
            }
        } catch (error) {
            hideLoader(true); // Hide immediately on error
            console.error('Error sending animation:', error);
            showNotification('Connection error', 'error');
        }
        return;
    }
    
    // Toggle animations (check if this button is already active)
    if (activeButton === buttonElement) {
        showLoader();
        try {
            // Send IDLE command to stop the animation
            const response = await fetch('/api/animation/IDLE', {
                method: 'POST'
            });
            
            const data = await response.json();
            
            if (data.success) {
                showNotification('Animation stopped', 'success');
                document.getElementById('current-mode').textContent = 'IDLE';
                clearActiveButton();
                await checkESP32Status();
                hideLoader();
            } else {
                hideLoader(true); // Hide immediately on error
                showNotification(`Error: ${data.response}`, 'error');
            }
        } catch (error) {
            hideLoader(true); // Hide immediately on error
            console.error('Error stopping animation:', error);
            showNotification('Connection error', 'error');
        }
        return;
    }
    
    // Start new toggle animation
    showLoader();
    try {
        const response = await fetch(`/api/animation/${animName}`, {
            method: 'POST'
        });
        
        const data = await response.json();
        
        if (data.success) {
            showNotification(`Animation: ${animName}`, 'success');
            document.getElementById('current-mode').textContent = animName;
            setActiveButton(buttonElement);
            await checkESP32Status();
            hideLoader();
        } else {
            hideLoader(true); // Hide immediately on error
            showNotification(`Error: ${data.response}`, 'error');
        }
    } catch (error) {
        hideLoader(true); // Hide immediately on error
        console.error('Error sending animation:', error);
        showNotification('Connection error', 'error');
    }
}

// Reset system
async function sendReset() {
    if (confirm('Reset all LEDs and return to idle?')) {
        showLoader();
        try {
            const response = await fetch('/api/reset', {
                method: 'POST'
            });
            
            const data = await response.json();
            
            if (data.success) {
                showNotification('System reset', 'success');
                document.getElementById('current-mode').textContent = 'IDLE';
                // Clear results from localStorage and hide banner
                localStorage.removeItem('raceResults');
                hideResultsBanner();
                // Clear active button state
                clearActiveButton();
                await checkESP32Status();
                hideLoader();
            } else {
                hideLoader(true); // Hide immediately on error
                showNotification(`Error: ${data.response}`, 'error');
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
        showLoader();
        try {
            const response = await fetch('/api/led/all_off', {
                method: 'POST'
            });
            
            const data = await response.json();
            
            if (data.success) {
                showNotification('EMERGENCY STOP - All LEDs OFF', 'error');
                document.getElementById('current-mode').textContent = 'STOPPED';
                // Clear all active button states
                clearActiveButton();
                // Collapse emergency stop after use
                const expanded = document.getElementById('emergency-expanded');
                const icon = document.getElementById('emergency-icon');
                expanded.classList.remove('active');
                icon.style.display = 'flex';
                await checkESP32Status();
                hideLoader();
            } else {
                hideLoader(true); // Hide immediately on error
                showNotification(`Error: ${data.response}`, 'error');
            }
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

// Open test modal with color wheel
function openTestModal() {
    console.log('[TEST MODAL] Opening modal...');
    const modal = document.getElementById('test-modal');
    modal.classList.add('active');
    
    // Initialize color picker if not already done
    if (!colorPicker) {
        console.log('[TEST MODAL] Initializing color picker and event handlers...');
        colorPicker = new iro.ColorPicker('#color-picker', {
            width: 200,
            color: '#E195AB', // Default to ROSE
            borderWidth: 2,
            borderColor: '#1B998B',
            layout: [
                {
                    component: iro.ui.Wheel,
                    options: {}
                }
            ]
        });
        
        // Listen to color changes
        colorPicker.on('color:change', function(color) {
            console.log('[COLOR WHEEL] Color changed:', color.rgb);
            sendTestColor(color.rgb.r, color.rgb.g, color.rgb.b, currentBrightness);
        });
        
        // Set up brightness slider (only once)
        const brightnessSlider = document.getElementById('brightness-slider');
        brightnessSlider.addEventListener('input', function() {
            currentBrightness = parseInt(this.value);
            document.getElementById('brightness-value').textContent = currentBrightness;
            const color = colorPicker.color.rgb;
            console.log('[BRIGHTNESS] Changed to:', currentBrightness);
            sendTestColor(color.r, color.g, color.b, currentBrightness);
        });
        
        // Set up preset buttons (only once)
        const presetButtons = document.querySelectorAll('.preset-btn');
        console.log('[PRESET BUTTONS] Found', presetButtons.length, 'buttons');
        presetButtons.forEach(btn => {
            btn.addEventListener('click', function() {
                const colorStr = this.dataset.color;
                const name = this.dataset.name;
                console.log('[PRESET] Button clicked:', name);
                
                if (name === 'OFF') {
                    sendTestOff();
                } else {
                    const [r, g, b] = colorStr.split(',').map(v => parseInt(v));
                    colorPicker.color.rgb = { r, g, b };
                    sendTestColor(r, g, b, currentBrightness);
                }
            });
        });
        
        console.log('[TEST MODAL] Initialization complete');
    } else {
        console.log('[TEST MODAL] Color picker already initialized');
    }
}

// Close test modal and turn off LEDs
async function closeTestModal() {
    const modal = document.getElementById('test-modal');
    modal.classList.remove('active');
    
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
    
    console.log('[SEND TEST COLOR] Function called with:', { r, g, b, brightness });
    console.log('[SEND TEST COLOR] Command:', command);
    
    try {
        console.log('[SEND TEST COLOR] Sending fetch request to /api/command...');
        
        const response = await fetch('/api/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ command: command })
        });
        
        console.log('[SEND TEST COLOR] Fetch completed, status:', response.status);
        
        const data = await response.json();
        console.log('[SEND TEST COLOR] Response data:', data);
        
        if (!data.success) {
            console.error('[SEND TEST COLOR] Command failed:', data);
        }
    } catch (error) {
        console.error('[SEND TEST COLOR] ERROR:', error);
    }
}

// Send test off command
async function sendTestOff() {
    console.log('[SEND TEST OFF] Turning off LEDs...');
    
    try {
        const response = await fetch('/api/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ command: 'LED:ALL_OFF' })
        });
        
        console.log('[SEND TEST OFF] Response status:', response.status);
        const data = await response.json();
        console.log('[SEND TEST OFF] Response data:', data);
    } catch (error) {
        console.error('[SEND TEST OFF] ERROR:', error);
    }
}

// Show results modal
function showResultsModal() {
    const modal = document.getElementById('results-modal');
    modal.classList.add('active');
}

// Close results modal and clear previews
async function closeResultsModal() {
    const modal = document.getElementById('results-modal');
    modal.classList.remove('active');
    
    // Turn off preview LEDs
    try {
        await fetch('/api/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ command: 'LED:ALL_OFF' })
        });
    } catch (error) {
        console.error('Error turning off preview LEDs:', error);
    }
}

// Preview winner selection (live preview)
let previousPreview = {
    win: null,
    place: null,
    show: null
};

async function previewWinner(position, cupNumber) {
    const colors = {
        win: '255,215,0',    // Gold
        place: '192,192,192', // Silver
        show: '205,127,50'    // Bronze
    };
    
    // Turn off previous cup for this position if it exists
    if (previousPreview[position]) {
        try {
            await fetch('/api/command', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ command: `LED:CUP:${previousPreview[position]}:0,0,0` })
            });
        } catch (error) {
            console.error('Error turning off previous preview:', error);
        }
    }
    
    // Light up new cup
    try {
        await fetch('/api/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ command: `LED:CUP:${cupNumber}:${colors[position]}` })
        });
        
        // Update tracking
        previousPreview[position] = cupNumber;
    } catch (error) {
        console.error('Error sending preview command:', error);
    }
}

// Saddle cloth colors and text colors for positions 1-20 (Official Racing Colors)
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

// Show results banner with saddle cloths and dot matrix text
function showResultsBanner(win, place, show) {
    const banner = document.getElementById('results-banner');
    
    // Update WIN row - fixed 5 tiles for label, saddle cloth, fixed 15 tiles for horse name
    const winLabel = document.getElementById('banner-win-label');
    winLabel.innerHTML = '';
    winLabel.appendChild(createDotMatrixText('  WIN', 0, 0, 5));  // 5 tiles total (2 spaces + WIN)
    
    const winNumber = document.getElementById('banner-win-number');
    winNumber.innerHTML = '';
    winNumber.appendChild(createSaddleCloth(win));
    
    const winName = document.getElementById('banner-win-name');
    winName.innerHTML = '';
    winName.appendChild(createDotMatrixText(`HORSE ${win}`, 0, 0, 15));  // 15 tiles total
    
    // Update PLACE row - fixed 5 tiles for label, saddle cloth, fixed 15 tiles for horse name
    const placeLabel = document.getElementById('banner-place-label');
    placeLabel.innerHTML = '';
    placeLabel.appendChild(createDotMatrixText('PLACE', 0, 0, 5));  // 5 tiles total (PLACE fits exactly)
    
    const placeNumber = document.getElementById('banner-place-number');
    placeNumber.innerHTML = '';
    placeNumber.appendChild(createSaddleCloth(place));
    
    const placeName = document.getElementById('banner-place-name');
    placeName.innerHTML = '';
    placeName.appendChild(createDotMatrixText(`HORSE ${place}`, 0, 0, 15));  // 15 tiles total
    
    // Update SHOW row - fixed 5 tiles for label, saddle cloth, fixed 15 tiles for horse name
    const showLabel = document.getElementById('banner-show-label');
    showLabel.innerHTML = '';
    showLabel.appendChild(createDotMatrixText(' SHOW', 0, 0, 5));  // 5 tiles total (1 space + SHOW)
    
    const showNumber = document.getElementById('banner-show-number');
    showNumber.innerHTML = '';
    showNumber.appendChild(createSaddleCloth(show));
    
    const showName = document.getElementById('banner-show-name');
    showName.innerHTML = '';
    showName.appendChild(createDotMatrixText(`HORSE ${show}`, 0, 0, 15));  // 15 tiles total
    
    banner.style.display = 'block';
}

// Hide results banner
function hideResultsBanner() {
    const banner = document.getElementById('results-banner');
    banner.style.display = 'none';
}

// Clear results (remove from localStorage and hide banner)
function clearResults() {
    localStorage.removeItem('raceResults');
    hideResultsBanner();
    showNotification('Results cleared', 'success');
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
    
    resultsEventSource.onopen = function() {
        console.log('[SSE] Connected to results stream');
    };
    
    resultsEventSource.addEventListener('results', function(e) {
        console.log('[SSE] Results received:', e.data);
        const data = JSON.parse(e.data);
        pendingResults = data;
        showResultsRevealModal();
    });
    
    resultsEventSource.onerror = function(e) {
        console.error('[SSE] Error:', e);
        // Reconnect after 5 seconds
        setTimeout(connectResultsStream, 5000);
    };
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

// Apply results
async function applyResults() {
    const winHorse = parseInt(document.getElementById('win-horse').value);
    const placeHorse = parseInt(document.getElementById('place-horse').value);
    const showHorse = parseInt(document.getElementById('show-horse').value);
    
    // Validate unique selections
    if (winHorse === placeHorse || winHorse === showHorse || placeHorse === showHorse) {
        showNotification('Win, Place, and Show must be different horses!', 'error');
        return;
    }
    
    showLoader();
    try {
        const response = await fetch('/api/results', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                win: winHorse,
                place: placeHorse,
                show: showHorse
            })
        });
        
        const data = await response.json();
        
        if (data.success) {
            showNotification(`Results set: Win=${winHorse}, Place=${placeHorse}, Show=${showHorse}`, 'success');
            document.getElementById('current-mode').textContent = 'RESULTS';
            // Save results to localStorage
            localStorage.setItem('raceResults', JSON.stringify({
                win: winHorse,
                place: placeHorse,
                show: showHorse
            }));
            // Show results banner
            showResultsBanner(winHorse, placeHorse, showHorse);
            closeResultsModal();
            await checkESP32Status();
            hideLoader();
        } else {
            hideLoader(true); // Hide immediately on error
            showNotification(`Error: ${data.response || data.error}`, 'error');
        }
    } catch (error) {
        hideLoader(true); // Hide immediately on error
        console.error('Error setting results:', error);
        showNotification('Connection error', 'error');
    }
}

// Close modal when clicking outside
window.onclick = function(event) {
    const modal = document.getElementById('results-modal');
    const emergencyExpanded = document.getElementById('emergency-expanded');
    const emergencyIcon = document.getElementById('emergency-icon');
    
    // Close results modal if clicking outside
    if (event.target === modal) {
        closeResultsModal();
    }
    
    // Collapse emergency stop if clicking outside
    if (emergencyExpanded.classList.contains('active')) {
        if (!emergencyExpanded.contains(event.target) && event.target !== emergencyIcon) {
            emergencyExpanded.classList.remove('active');
            emergencyIcon.style.display = 'flex';
        }
    }
}

// Get temperature color class based on temperature
function getTempColorClass(temp) {
    if (temp < 40) return 'temp-cold';
    if (temp < 60) return 'temp-cool';
    if (temp < 75) return 'temp-comfortable';
    if (temp < 90) return 'temp-warm';
    return 'temp-hot';
}

// Store weather data globally for modal
let weatherData = null;

// Fetch and display weather summary/modal
async function getWeather() {
    try {
        const response = await fetch('/api/weather');
        const data = await response.json();
        
        if (data.success && data.current && data.hourly) {
            weatherData = data;
            
            // Update graphical header summary
            const summary = document.getElementById('weather-summary');
            const current = data.current;
            const temp = Math.round(current.temp_f);
            const tempColorClass = getTempColorClass(temp);
            const condition = current.condition.text;
            const location = data.location || 'Dallas, TX';
            const iconUrl = current.condition.icon.replace('64x64', '128x128');
            
            // Build graphical weather widget with forced icon size
            summary.innerHTML = `
                <img src="https:${iconUrl}" alt="${condition}" class="weather-icon" 
                     width="36" height="36"
                     style="width: 36px !important; height: 36px !important; min-width: 36px; min-height: 36px; max-width: 36px; max-height: 36px; flex-shrink: 0;">
                <div class="weather-info">
                    <div class="weather-temp-large ${tempColorClass}">${temp}°F</div>
                    <div class="weather-condition">${condition}</div>
                    <div class="weather-location">${location}</div>
                </div>
            `;
            summary.style.display = 'flex';
        }
    } catch (error) {
        console.error('Error fetching weather:', error);
        // Hide weather summary on error
        document.getElementById('weather-summary').style.display = 'none';
    }
}

// Open weather modal
function openWeatherModal() {
    if (!weatherData) return;
    
    const modal = document.getElementById('weather-modal');
    const grid = document.getElementById('weather-modal-grid');
    const title = document.getElementById('weather-modal-title');
    
    // Set title
    const location = weatherData.location || 'Dallas, TX';
    title.textContent = `Weather Forecast - ${location}`;
    
    // Clear grid
    grid.innerHTML = '';
    
    // Populate with hourly forecasts
    const hourlyForecasts = weatherData.hourly.slice(0, 12);
    hourlyForecasts.forEach((item, index) => {
        const timeStr = item.time.split(' ')[1];
        const hours = parseInt(timeStr.split(':')[0]);
        const ampm = hours >= 12 ? 'PM' : 'AM';
        const displayHour = hours % 12 || 12;
        const temp = Math.round(item.temp_f);
        const tempColorClass = getTempColorClass(temp);
        const iconUrl = item.condition.icon.replace('64x64', '128x128');
        
        const weatherItem = document.createElement('div');
        weatherItem.className = 'weather-modal-item';
        if (index === 0) weatherItem.classList.add('current');
        
        weatherItem.innerHTML = `
            <div class="weather-modal-time">${index === 0 ? 'NOW' : `${displayHour} ${ampm}`}</div>
            <img src="https:${iconUrl}" alt="${item.condition.text}" class="weather-modal-icon">
            <div class="weather-modal-temp ${tempColorClass}">${temp}°F</div>
        `;
        
        grid.appendChild(weatherItem);
    });
    
    modal.classList.add('active');
}

// Close weather modal
function closeWeatherModal() {
    const modal = document.getElementById('weather-modal');
    modal.classList.remove('active');
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
    
    // Hide splash screen after 2 seconds
    setTimeout(hideSplashScreen, 2000);
    
    // Update clock immediately and every second
    updateClock();
    setInterval(updateClock, 1000);
    
    // Check ESP32 status immediately and every 5 seconds
    checkESP32Status();
    setInterval(checkESP32Status, 5000);
    
    // Get system status
    getSystemStatus();
    
    // Connect to SSE stream for real-time results
    connectResultsStream();
    
    // Load initial results from server
    loadResultsFromServer();
    
    // Get weather forecast immediately and every 30 minutes
    getWeather();
    setInterval(getWeather, 30 * 60 * 1000);
    
    // Listen for fullscreen changes
    document.addEventListener('fullscreenchange', updateFullscreenIcon);
    document.addEventListener('webkitfullscreenchange', updateFullscreenIcon); // Safari
});
