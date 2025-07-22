/**
 * DDM Racing System - Core Dashboard Functionality
 * Main dashboard initialization and core features
 */

// Global dashboard state
const DDMDashboard = {
    initialized: false,
    voiceSupported: false,
    // Open the mode modal (pre_race, betting_open, during_race, results)
    openModeModal(mode) {
        // Set modal title and icon based on mode
        const modalTitle = document.getElementById('modal-mode-title');
        const modalIcon = document.getElementById('modal-mode-icon');
        const modalBody = document.getElementById('animationModalBody');
        let title = '';
        let icon = '';
        let bodyHtml = '';
        switch (mode) {
            case 'pre_race':
                // Open the dedicated pre-race modal
                const preRaceModalEl = document.getElementById('preRaceModal');
                if (preRaceModalEl) {
                    const modal = new bootstrap.Modal(preRaceModalEl);
                    modal.show();
                } else {
                    alert('Pre-Race modal element not found!');
                }
                break;
            case 'betting_open':
                // Open the dedicated betting open modal
                const bettingOpenModalEl = document.getElementById('bettingOpenModal');
                if (bettingOpenModalEl) {
                    const modal = new bootstrap.Modal(bettingOpenModalEl);
                    modal.show();
                } else {
                    alert('Betting Open modal element not found!');
                }
                break;
            case 'during_race':
                // Open the dedicated during race modal
                const duringRaceModalEl = document.getElementById('duringRaceModal');
                if (duringRaceModalEl) {
                    const modal = new bootstrap.Modal(duringRaceModalEl);
                    modal.show();
                } else {
                    alert('During Race modal element not found!');
                }
                break;
            case 'results':
                // Instead of showing the animation modal, open the horse selection modal
                const horseModal = document.getElementById('horseSelectionModal');
                if (horseModal) {
                    const modal = new bootstrap.Modal(horseModal);
                    modal.show();
                } else {
                    alert('Horse selection modal not found!');
                }
                break;
            default:
                title = 'Mode';
                icon = '🎬';
                bodyHtml = `<p>No content available for this mode.</p>`;
                if (modalTitle) modalTitle.textContent = title;
                if (modalIcon) modalIcon.textContent = icon;
                if (modalBody) modalBody.innerHTML = bodyHtml;
                {
                    const modalEl = document.getElementById('animationModal');
                    if (modalEl) {
                        const modal = new bootstrap.Modal(modalEl);
                        modal.show();
                    } else {
                        alert('Modal element not found!');
                    }
                }
        }
    },

    // Test animation button logic
    testAnimation() {
        fetch('/api/test-animation', { method: 'POST' })
            .then(response => response.json())
            .then(data => {
                this.showNotification('Test animation triggered!', 'success');
            })
            .catch(error => {
                this.showNotification('Test animation failed', 'error');
                console.error('Test animation error:', error);
            });
    },

    // Refresh status button logic
    refreshStatus() {
        // You can reload data here, or just reload the page
        location.reload();
    },

    // Clear logs button logic
    clearLogs() {
        fetch('/api/clear-logs', { method: 'POST' })
            .then(response => response.json())
            .then(data => {
                this.showNotification('Logs cleared!', 'success');
            })
            .catch(error => {
                this.showNotification('Failed to clear logs', 'error');
                console.error('Clear logs error:', error);
            });
    },
    
    // Initialize the dashboard
    async init() {
        if (this.initialized) return;
        
        console.log('🏇 Initializing DDM Dashboard...');
        
        try {
            // Core initialization
            this.checkVoiceSupport();
            await this.loadSystemStatus();
            await this.loadOpenAIUsage();
            
            // Set up event listeners
            this.setupEventListeners();
            
            // Initialize components
            this.updateResetButtonState();
            
            this.initialized = true;
            console.log('✅ DDM Dashboard initialized successfully');
        } catch (error) {
            console.error('❌ Dashboard initialization failed:', error);
        }
    },
    
    // Set up all event listeners
    setupEventListeners() {
        // OpenAI toggle
        const openaiToggle = document.getElementById('openaiToggle');
        if (openaiToggle) {
            openaiToggle.addEventListener('change', this.toggleOpenAI.bind(this));
        }
        
        // Manual prompt enter key
        const manualPrompt = document.getElementById('manualPrompt');
        if (manualPrompt) {
            manualPrompt.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    this.sendManualPrompt();
                }
            });
        }
        
        // Add finalize button event listener as backup
        setTimeout(() => {
            const finalizeBtn = document.getElementById('finalize-results-btn');
            if (finalizeBtn) {
                finalizeBtn.addEventListener('click', (e) => {
                    console.log('Finalize button clicked via event listener');
                    e.preventDefault();
                    window.HorseSelection?.finalizeResults();
                });
                console.log('Finalize button event listener added');
            } else {
                console.warn('Finalize button not found during initialization');
            }
        }, 100);
    },
    
    // Check voice support
    checkVoiceSupport() {
        const statusElement = document.getElementById('voice-support-status');
        if (!statusElement) return;
        
        try {
            if (typeof voicePrompt !== 'undefined' && voicePrompt.isSupported()) {
                statusElement.textContent = '✅ Voice recognition supported';
                statusElement.className = 'small text-success mt-2';
                this.voiceSupported = true;
            } else {
                statusElement.textContent = '❌ Voice recognition not supported in this browser';
                statusElement.className = 'small text-warning mt-2';
                this.voiceSupported = false;
            }
        } catch (error) {
            console.warn('Voice support check failed:', error);
            statusElement.textContent = '❌ Voice support unavailable';
            statusElement.className = 'small text-warning mt-2';
            this.voiceSupported = false;
        }
    },
    
    // Load system status
    async loadSystemStatus() {
        try {
            const response = await fetch('/api/openai/status');
            const data = await response.json();
            
            const statusElement = document.getElementById('openai-status');
            const textElement = document.getElementById('openai-text');
            
            if (statusElement && textElement) {
                if (data.configured) {
                    statusElement.className = 'status-indicator status-online';
                    textElement.textContent = 'Ready';
                } else {
                    statusElement.className = 'status-indicator status-offline';
                    textElement.textContent = 'Not configured';
                }
            }
        } catch (error) {
            console.error('Error loading system status:', error);
        }
    },
    
    // Load OpenAI usage statistics
    async loadOpenAIUsage() {
        try {
            const response = await fetch('/api/openai/usage');
            const data = await response.json();
            
            const usageElement = document.getElementById('openai-usage');
            if (usageElement) {
                if (data.success) {
                    usageElement.innerHTML = `
                        <small class="text-muted">
                            Usage: ${data.requests || 0} requests, 
                            ${data.tokens || 0} tokens
                        </small>
                    `;
                } else {
                    usageElement.innerHTML = '<small class="text-muted">Usage data unavailable</small>';
                }
            }
        } catch (error) {
            console.error('Error loading OpenAI usage:', error);
            const usageElement = document.getElementById('openai-usage');
            if (usageElement) {
                usageElement.innerHTML = '<small class="text-muted">Usage data unavailable</small>';
            }
        }
    },
    
    // Toggle OpenAI functionality
    async toggleOpenAI() {
        const toggle = document.getElementById('openaiToggle');
        if (!toggle) return;
        
        try {
            const response = await fetch('/api/openai/toggle', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: toggle.checked})
            });
            
            const data = await response.json();
            
            if (data.success) {
                this.showNotification(
                    `OpenAI ${toggle.checked ? 'enabled' : 'disabled'}`, 
                    'success'
                );
            } else {
                this.showNotification(data.error || 'Failed to toggle OpenAI', 'error');
                toggle.checked = !toggle.checked; // Revert on error
            }
        } catch (error) {
            console.error('Error toggling OpenAI:', error);
            this.showNotification('Error toggling OpenAI', 'error');
            toggle.checked = !toggle.checked; // Revert on error
        }
    },
    
    // Send manual prompt
    async sendManualPrompt() {
        const promptInput = document.getElementById('manualPrompt');
        if (!promptInput) return;
        
        const prompt = promptInput.value.trim();
        if (!prompt) {
            this.showNotification('Please enter a prompt', 'warning');
            return;
        }
        
        try {
            const response = await fetch('/api/openai/prompt', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({prompt})
            });
            
            const data = await response.json();
            
            if (data.success) {
                this.showNotification('Prompt sent successfully', 'success');
                promptInput.value = '';
            } else {
                this.showNotification(data.error || 'Failed to send prompt', 'error');
            }
        } catch (error) {
            console.error('Error sending prompt:', error);
            this.showNotification('Error sending prompt', 'error');
        }
    },
    
    // Update reset button state
    updateResetButtonState() {
        const resetBtn = document.getElementById('reset-results-btn');
        if (!resetBtn) return;
        
        // Check if there are any results to reset
        const winNumber = document.getElementById('winner-win-number');
        const placeNumber = document.getElementById('winner-place-number');
        const showNumber = document.getElementById('winner-show-number');
        
        const hasResults = (winNumber && winNumber.textContent !== '--') ||
                          (placeNumber && placeNumber.textContent !== '--') ||
                          (showNumber && showNumber.textContent !== '--');
        
        resetBtn.disabled = !hasResults;
    },
    
    // Show notification
    showNotification(message, type = 'info') {
        // Create notification element
        const notification = document.createElement('div');
        notification.className = `alert alert-${type === 'error' ? 'danger' : type} alert-dismissible fade show position-fixed`;
        notification.style.cssText = 'top: 20px; right: 20px; z-index: 9999; min-width: 300px;';
        notification.innerHTML = `
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        `;
        
        document.body.appendChild(notification);
        
        // Auto-remove after 5 seconds
        setTimeout(() => {
            if (notification.parentNode) {
                notification.remove();
            }
        }, 5000);
    }
};

// Initialize when DOM is loaded

document.addEventListener('DOMContentLoaded', () => {
    DDMDashboard.init();

    // Attach shown.bs.modal event to animationModal for dynamic content setup
    const animationModal = document.getElementById('animationModal');
    if (animationModal) {
        animationModal.addEventListener('shown.bs.modal', function () {
            console.log('[Bootstrap Event] shown.bs.modal fired for animationModal');
            // Place any dynamic setup logic here if needed, e.g.:
            // - Re-initialize animation buttons
            // - Focus a default button
            // - Run layout adjustments
            // Example: focus the first button if present
            const firstBtn = animationModal.querySelector('button');
            if (firstBtn) firstBtn.focus();
        });
    } else {
        console.error('animationModal element not found in DOM when attaching shown.bs.modal event!');
    }
});

// Make DDMDashboard globally available
window.DDMDashboard = DDMDashboard;

// =============================================================================
// GLOBAL FUNCTION WRAPPERS for HTML onclick handlers
// =============================================================================

// Modal functions
function openModeModal(mode) {
    if (DDMDashboard.openModeModal) {
        DDMDashboard.openModeModal(mode);
    } else {
        console.warn('openModeModal not implemented yet:', mode);
        // Fallback: show a simple alert for now
        alert(`Opening ${mode} modal - feature coming soon!`);
    }
}

// Animation functions  
function testAnimation() {
    if (DDMDashboard.testAnimation) {
        DDMDashboard.testAnimation();
    } else {
        console.log('Test animation called');
        // Simple API call for now
        fetch('/api/test-animation', { method: 'POST' })
            .then(response => response.json())
            .then(data => console.log('Test animation:', data))
            .catch(error => console.error('Test animation error:', error));
    }
}

function refreshStatus() {
    if (DDMDashboard.refreshStatus) {
        DDMDashboard.refreshStatus();
    } else {
        location.reload();
    }
}

function clearLogs() {
    if (DDMDashboard.clearLogs) {
        DDMDashboard.clearLogs();
    } else {
        console.log('Clear logs called');
        fetch('/api/clear-logs', { method: 'POST' })
            .then(response => response.json())
            .then(data => console.log('Logs cleared:', data))
            .catch(error => console.error('Clear logs error:', error));
    }
}

// Race functions
function resetRaceResults() {
    if (DDMDashboard.resetRaceResults) {
        DDMDashboard.resetRaceResults();
    } else {
        console.log('Reset race results called');
        fetch('/api/reset-race-results', { method: 'POST' })
            .then(response => response.json())
            .then(data => {
                console.log('Race results reset:', data);
                location.reload();
            })
            .catch(error => console.error('Reset race results error:', error));
    }
}

function endRace() {
    if (DDMDashboard.endRace) {
        DDMDashboard.endRace();
    } else {
        console.log('End race called');
        // Add your end race logic here
    }
}

function sendManualAnimation() {
    if (DDMDashboard.sendManualAnimation) {
        DDMDashboard.sendManualAnimation();
    } else {
        const input = document.getElementById('manualPrompt');
        if (input && input.value.trim()) {
            console.log('Manual animation:', input.value);
            // Add your manual animation logic here
        }
    }
}

// Horse selection functions
// resetSelections removed

function finalizeResults() {
    if (HorseSelection && HorseSelection.finalizeResults) {
        HorseSelection.finalizeResults();
    } else {
        console.log('Finalize results called');
        // Add finalize logic here
    }
}
