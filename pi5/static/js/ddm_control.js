// ddm_control.js - Dashboard control JavaScript

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
    }
}

// Send animation command
async function sendAnimation(animName) {
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
    }
}

// Reset system
async function sendReset() {
    if (confirm('Reset all LEDs and return to idle?')) {
        try {
            const response = await fetch('/api/reset', {
                method: 'POST'
            });
            
            const data = await response.json();
            
            if (data.success) {
                showNotification('System reset', 'success');
                document.getElementById('current-mode').textContent = 'IDLE';
            } else {
                showNotification(`Error: ${data.response}`, 'error');
            }
            
            await checkESP32Status();
        } catch (error) {
            console.error('Error resetting:', error);
            showNotification('Connection error', 'error');
        }
    }
}

// Emergency stop
async function emergencyStop() {
    if (confirm('EMERGENCY STOP: Turn off all LEDs?')) {
        try {
            const response = await fetch('/api/led/all_off', {
                method: 'POST'
            });
            
            const data = await response.json();
            
            if (data.success) {
                showNotification('EMERGENCY STOP - All LEDs OFF', 'error');
                document.getElementById('current-mode').textContent = 'STOPPED';
            } else {
                showNotification(`Error: ${data.response}`, 'error');
            }
            
            await checkESP32Status();
        } catch (error) {
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

// Apply results
async function applyResults() {
    const winCup = parseInt(document.getElementById('win-cup').value);
    const placeCup = parseInt(document.getElementById('place-cup').value);
    const showCup = parseInt(document.getElementById('show-cup').value);
    
    // Validate unique selections
    if (winCup === placeCup || winCup === showCup || placeCup === showCup) {
        showNotification('Win, Place, and Show must be different cups!', 'error');
        return;
    }
    
    try {
        const response = await fetch('/api/results', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                win: winCup,
                place: placeCup,
                show: showCup
            })
        });
        
        const data = await response.json();
        
        if (data.success) {
            showNotification(`Results set: Win=${winCup}, Place=${placeCup}, Show=${showCup}`, 'success');
            document.getElementById('current-mode').textContent = 'RESULTS';
            closeResultsModal();
        } else {
            showNotification(`Error: ${data.response || data.error}`, 'error');
        }
        
        await checkESP32Status();
    } catch (error) {
        console.error('Error setting results:', error);
        showNotification('Connection error', 'error');
    }
}

// Close modal when clicking outside
window.onclick = function(event) {
    const modal = document.getElementById('results-modal');
    if (event.target === modal) {
        closeResultsModal();
    }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    console.log('DDM Cup Dashboard initialized');
    
    // Update clock immediately and every second
    updateClock();
    setInterval(updateClock, 1000);
    
    // Check ESP32 status immediately and every 5 seconds
    checkESP32Status();
    setInterval(checkESP32Status, 5000);
    
    // Get system status
    getSystemStatus();
});
