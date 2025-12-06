// ddm_control.js - Dashboard control JavaScript

// Show loading indicator
function showLoader() {
    const loader = document.getElementById('loader');
    loader.classList.add('show');
}

// Hide loading indicator
function hideLoader() {
    const loader = document.getElementById('loader');
    loader.classList.remove('show');
}

// Update clock
function updateClock() {
    const now = new Date();
    const timeString = now.toLocaleTimeString('en-US', { hour12: false });
    document.getElementById('current-time').textContent = timeString;
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
async function sendCommand(command) {
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
        } else {
            showNotification(`Error: ${data.response}`, 'error');
        }
        
        await checkESP32Status();
    } catch (error) {
        console.error('Error sending command:', error);
        showNotification('Connection error', 'error');
    } finally {
        hideLoader();
    }
}

// Send animation command
async function sendAnimation(animName) {
    showLoader();
    try {
        const response = await fetch(`/api/animation/${animName}`, {
            method: 'POST'
        });
        
        const data = await response.json();
        
        if (data.success) {
            showNotification(`Animation: ${animName}`, 'success');
            document.getElementById('current-mode').textContent = animName;
        } else {
            showNotification(`Error: ${data.response}`, 'error');
        }
        
        await checkESP32Status();
    } catch (error) {
        console.error('Error sending animation:', error);
        showNotification('Connection error', 'error');
    } finally {
        hideLoader();
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
                // Hide results banner on reset
                hideResultsBanner();
            } else {
                showNotification(`Error: ${data.response}`, 'error');
            }
            
            await checkESP32Status();
        } catch (error) {
            console.error('Error resetting:', error);
            showNotification('Connection error', 'error');
        } finally {
            hideLoader();
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
                // Collapse emergency stop after use
                const expanded = document.getElementById('emergency-expanded');
                const icon = document.getElementById('emergency-icon');
                expanded.classList.remove('active');
                icon.style.display = 'flex';
            } else {
                showNotification(`Error: ${data.response}`, 'error');
            }
            
            await checkESP32Status();
        } catch (error) {
            console.error('Error with emergency stop:', error);
            showNotification('Connection error', 'error');
        } finally {
            hideLoader();
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
            // Show results banner
            showResultsBanner(winHorse, placeHorse, showHorse);
            closeResultsModal();
        } else {
            showNotification(`Error: ${data.response || data.error}`, 'error');
        }
        
        await checkESP32Status();
    } catch (error) {
        console.error('Error setting results:', error);
        showNotification('Connection error', 'error');
    } finally {
        hideLoader();
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

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    console.log('DDM Horse Dashboard initialized');
    
    // Update clock immediately and every second
    updateClock();
    setInterval(updateClock, 1000);
    
    // Check ESP32 status immediately and every 5 seconds
    checkESP32Status();
    setInterval(checkESP32Status, 5000);
    
    // Get system status
    getSystemStatus();
});
