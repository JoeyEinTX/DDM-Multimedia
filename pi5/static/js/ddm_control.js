// ddm_control.js - Dashboard control JavaScript

// Track active button
let activeButton = null;

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
        activeButton = null;
    }
}

// Flip clock animation
function flipCard(cardId, newValue) {
    const card = document.getElementById(cardId);
    const currentValue = card.querySelector('.flip-card-top').textContent;
    
    // Only flip if value changed
    if (currentValue !== newValue) {
        const top = card.querySelector('.flip-card-top');
        const bottom = card.querySelector('.flip-card-bottom');
        const face = card.querySelector('.flip-card-face');
        
        // Set new value on bottom half
        bottom.textContent = newValue;
        
        // Trigger flip animation
        card.classList.add('flipping');
        
        // After animation completes, update top half and reset
        setTimeout(() => {
            top.textContent = newValue;
            face.textContent = newValue;
            card.classList.remove('flipping');
        }, 600);
    }
}

// Update flip clock
function updateClock() {
    const now = new Date();
    let hours = now.getHours();
    const minutes = now.getMinutes();
    const seconds = now.getSeconds();
    
    // Determine AM/PM
    const ampm = hours >= 12 ? 'PM' : 'AM';
    
    // Convert to 12-hour format
    hours = hours % 12;
    hours = hours ? hours : 12; // 0 should be 12
    
    // Pad with zeros
    const hoursStr = String(hours).padStart(2, '0');
    const minutesStr = String(minutes).padStart(2, '0');
    const secondsStr = String(seconds).padStart(2, '0');
    
    // Update each digit
    flipCard('flip-hour1', hoursStr[0]);
    flipCard('flip-hour2', hoursStr[1]);
    flipCard('flip-min1', minutesStr[0]);
    flipCard('flip-min2', minutesStr[1]);
    flipCard('flip-sec1', secondsStr[0]);
    flipCard('flip-sec2', secondsStr[1]);
    
    // Update AM/PM
    document.getElementById('flip-ampm').textContent = ampm;
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
            } else {
                showNotification(`Error: ${data.response}`, 'error');
            }
            
            await checkESP32Status();
        } catch (error) {
            console.error('Error stopping command:', error);
            showNotification('Connection error', 'error');
        } finally {
            hideLoader();
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
            } else {
                showNotification(`Error: ${data.response}`, 'error');
            }
            
            await checkESP32Status();
        } catch (error) {
            console.error('Error stopping animation:', error);
            showNotification('Connection error', 'error');
        } finally {
            hideLoader();
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
                // Clear results from localStorage and hide banner
                localStorage.removeItem('raceResults');
                hideResultsBanner();
                // Clear active button state
                clearActiveButton();
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
            
            // Get current hour
            const now = new Date();
            const currentHour = now.getHours();
            
            // Filter to get next 12 hours starting from current hour
            const hourlyForecasts = data.hourly.filter(item => {
                const forecastTime = new Date(item.time);
                const forecastHour = forecastTime.getHours();
                // Get forecasts for the next 12 hours
                return forecastHour >= currentHour && forecastHour < currentHour + 12;
            }).slice(0, 12);
            
            hourlyForecasts.forEach((item, index) => {
                const time = new Date(item.time);
                const hours = time.getHours();
                const ampm = hours >= 12 ? 'PM' : 'AM';
                const displayHour = hours % 12 || 12;
                const temp = Math.round(item.temp_f);
                
                const weatherItem = document.createElement('div');
                weatherItem.className = 'weather-item';
                
                // Add NOW class to first card
                if (index === 0) {
                    weatherItem.classList.add('now');
                }
                
                // Add current-hour class to current hour
                if (hours === currentHour) {
                    weatherItem.classList.add('current-hour');
                }
                
                // Get temperature color class
                const tempColorClass = getTempColorClass(temp);
                
                // Build HTML with NOW label for first card
                weatherItem.innerHTML = `
                    ${index === 0 ? '<div class="weather-now-label">NOW</div>' : ''}
                    <div class="weather-time">${displayHour} ${ampm}</div>
                    <img src="https:${item.condition.icon}" alt="${item.condition.text}" class="weather-icon-img">
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
});
