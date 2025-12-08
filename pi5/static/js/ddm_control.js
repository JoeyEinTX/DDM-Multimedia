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

// Dot matrix patterns (5x7 grid) - 1 = lit, 0 = unlit
const dotPatterns = {
    '0': [0x1F, 0x11, 0x11, 0x11, 0x1F],
    '1': [0x00, 0x12, 0x1F, 0x10, 0x00],
    '2': [0x1D, 0x15, 0x15, 0x15, 0x17],
    '3': [0x11, 0x15, 0x15, 0x15, 0x0E],
    '4': [0x07, 0x04, 0x04, 0x1F, 0x04],
    '5': [0x17, 0x15, 0x15, 0x15, 0x1D],
    '6': [0x1E, 0x15, 0x15, 0x15, 0x1C],
    '7': [0x01, 0x01, 0x01, 0x1D, 0x03],
    '8': [0x1F, 0x15, 0x15, 0x15, 0x1F],
    '9': [0x07, 0x15, 0x15, 0x15, 0x0F],
    'A': [0x1E, 0x05, 0x05, 0x05, 0x1E],
    'P': [0x1F, 0x05, 0x05, 0x05, 0x02],
    'M': [0x1F, 0x02, 0x04, 0x02, 0x1F]
};

// Create a digit element with dot matrix
function createDotDigit(char) {
    const digit = document.createElement('div');
    digit.className = 'dot-digit';
    const pattern = dotPatterns[char] || [0,0,0,0,0];
    
    for (let row = 0; row < 7; row++) {
        for (let col = 0; col < 5; col++) {
            const dot = document.createElement('div');
            dot.className = 'dot';
            // Use bit 0 for top row (row 0), bit 4 for bottom data row (row 4)
            if (row < 5 && (pattern[col] & (1 << row))) {
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

// Check ESP32 status
async function checkESP32Status() {
    try {
        const response = await fetch('/api/ping');
        const data = await response.json();
        
        const statusElement = document.getElementById('esp32-status');
        if (data.success) {
            statusElement.textContent = 'ONLINE';
            statusElement.className = 'status-value status-online';
        } else {
            statusElement.textContent = 'OFFLINE';
            statusElement.className = 'status-value status-offline';
        }
    } catch (error) {
        console.error('Error checking ESP32 status:', error);
        const statusElement = document.getElementById('esp32-status');
        statusElement.textContent = 'ERROR';
        statusElement.className = 'status-value status-offline';
    }
}

// Get system status
async function getSystemStatus() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();
        
        if (data.esp32_ip) {
            document.getElementById('esp32-ip').textContent = data.esp32_ip;
        }
    } catch (error) {
        console.error('Error getting system status:', error);
    }
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

// Show results modal
function showResultsModal() {
    const modal = document.getElementById('results-modal');
    modal.classList.add('active');
}

// Close results modal
function closeResultsModal() {
    const modal = document.getElementById('results-modal');
    modal.classList.remove('active');
}

// Show results banner
function showResultsBanner(win, place, show) {
    const banner = document.getElementById('results-banner');
    document.getElementById('banner-win').textContent = win;
    document.getElementById('banner-place').textContent = place;
    document.getElementById('banner-show').textContent = show;
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

// Load results from localStorage
function loadResultsFromStorage() {
    const stored = localStorage.getItem('raceResults');
    if (stored) {
        try {
            const results = JSON.parse(stored);
            showResultsBanner(results.win, results.place, results.show);
        } catch (error) {
            console.error('Error loading results from storage:', error);
        }
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

// Fetch and display weather forecast
async function getWeather() {
    try {
        const response = await fetch('/api/weather');
        const data = await response.json();
        
        if (data.success && data.hourly) {
            const weatherScroll = document.getElementById('weather-scroll');
            weatherScroll.innerHTML = '';
            
            // Get current hour for highlighting
            const now = new Date();
            const currentHour = now.getHours();
            
            // Simply take the first 12 hours from the API (already sorted chronologically)
            const hourlyForecasts = data.hourly.slice(0, 12);
            
            hourlyForecasts.forEach((item, index) => {
                // Parse hour directly from time string (already in local time)
                // Format: "YYYY-MM-DD HH:MM" e.g., "2025-12-07 19:00"
                const timeStr = item.time.split(' ')[1]; // Get "HH:MM"
                const hours = parseInt(timeStr.split(':')[0]); // Get hour as integer
                const ampm = hours >= 12 ? 'PM' : 'AM';
                const displayHour = hours % 12 || 12;
                const temp = Math.round(item.temp_f);
                
                const weatherItem = document.createElement('div');
                weatherItem.className = 'weather-item';
                
                // Add NOW class to first card
                if (index === 0) {
                    weatherItem.classList.add('now');
                }
                
                // Get temperature color class
                const tempColorClass = getTempColorClass(temp);
                
                // Build HTML with NOW label for first card
                // Replace 64x64 with 128x128 in icon URL for larger icons
                const iconUrl = item.condition.icon.replace('64x64', '128x128');
                
                weatherItem.innerHTML = `
                    ${index === 0 ? '<div class="weather-now-label">NOW</div>' : ''}
                    <div class="weather-time">${displayHour} ${ampm}</div>
                    <img src="https:${iconUrl}" alt="${item.condition.text}" class="weather-icon-img">
                    <div class="weather-temp ${tempColorClass}">${temp}°F</div>
                `;
                
                weatherScroll.appendChild(weatherItem);
            });
        }
    } catch (error) {
        console.error('Error fetching weather:', error);
        // Hide weather forecast on error
        document.getElementById('weather-forecast').style.display = 'none';
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
    
    // Load saved results from localStorage
    loadResultsFromStorage();
    
    // Get weather forecast immediately and every 30 minutes
    getWeather();
    setInterval(getWeather, 30 * 60 * 1000);
    
    // Listen for fullscreen changes
    document.addEventListener('fullscreenchange', updateFullscreenIcon);
    document.addEventListener('webkitfullscreenchange', updateFullscreenIcon); // Safari
});
