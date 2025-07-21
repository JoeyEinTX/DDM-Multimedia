/**
 * DDM Racing System - Race Results Management
 * Handles race results display, reset functionality, and animations
 */

const RaceResults = {
    // Initialize race results functionality
    init() {
        console.log('🏁 Initializing Race Results...');
        this.setupEventListeners();
    },
    
    // Set up event listeners
    setupEventListeners() {
        // Reset results button
        const resetBtn = document.getElementById('reset-results-btn');
        if (resetBtn) {
            resetBtn.addEventListener('click', this.resetRaceResults.bind(this));
        }
    },
    
    // Reset all race results
    resetRaceResults() {
        console.log('Resetting race results...');
        
        try {
            // Reset winner numbers
            const winNumber = document.getElementById('winner-win-number');
            const placeNumber = document.getElementById('winner-place-number');
            const showNumber = document.getElementById('winner-show-number');
            
            if (winNumber) winNumber.textContent = '--';
            if (placeNumber) placeNumber.textContent = '--';
            if (showNumber) showNumber.textContent = '--';
            
            // Remove active states
            const winItem = document.getElementById('winner-win');
            const placeItem = document.getElementById('winner-place');
            const showItem = document.getElementById('winner-show');
            
            if (winItem) winItem.classList.remove('active');
            if (placeItem) placeItem.classList.remove('active');
            if (showItem) showItem.classList.remove('active');
            
            // Reset timestamp
            const timestamp = document.getElementById('results-timestamp');
            if (timestamp) {
                timestamp.textContent = 'No race results yet';
            }
            
            // Update button states
            DDMDashboard?.updateResetButtonState();
            
            // Show success message
            DDMDashboard?.showNotification('Race results cleared', 'success');
            
        } catch (error) {
            console.error('Error resetting race results:', error);
            DDMDashboard?.showNotification('Error clearing results', 'error');
        }
    },
    
    // Set race results programmatically
    setResults(results) {
        try {
            const { win, place, show } = results;
            
            // Update numbers with animation
            this.updateResultWithAnimation('winner-win-number', win);
            this.updateResultWithAnimation('winner-place-number', place);
            this.updateResultWithAnimation('winner-show-number', show);
            
            // Add active states
            if (win) {
                const winItem = document.getElementById('winner-win');
                if (winItem) {
                    winItem.classList.add('active');
                    this.triggerWinAnimation(winItem);
                }
            }
            
            if (place) {
                const placeItem = document.getElementById('winner-place');
                if (placeItem) {
                    placeItem.classList.add('active');
                    this.triggerPlaceAnimation(placeItem);
                }
            }
            
            if (show) {
                const showItem = document.getElementById('winner-show');
                if (showItem) {
                    showItem.classList.add('active');
                    this.triggerShowAnimation(showItem);
                }
            }
            
            // Update timestamp
            const timestamp = document.getElementById('results-timestamp');
            if (timestamp) {
                timestamp.textContent = `Results set: ${new Date().toLocaleTimeString()}`;
            }
            
            // Update button states
            DDMDashboard?.updateResetButtonState();
            
            console.log('Race results set:', results);
            
        } catch (error) {
            console.error('Error setting race results:', error);
        }
    },
    
    // Update a result number with animation
    updateResultWithAnimation(elementId, value) {
        const element = document.getElementById(elementId);
        if (!element) return;
        
        // Add reveal animation
        element.classList.add('reveal');
        
        // Update value after short delay for better effect
        setTimeout(() => {
            element.textContent = value || '--';
        }, 100);
        
        // Remove animation class
        setTimeout(() => {
            element.classList.remove('reveal');
        }, 1500);
    },
    
    // Trigger win animation (gold)
    triggerWinAnimation(element) {
        element.style.animation = 'gold-pulse 2s ease-in-out 3';
        setTimeout(() => {
            element.style.animation = '';
        }, 6000);
    },
    
    // Trigger place animation (silver)
    triggerPlaceAnimation(element) {
        element.style.animation = 'silver-pulse 2s ease-in-out 3';
        setTimeout(() => {
            element.style.animation = '';
        }, 6000);
    },
    
    // Trigger show animation (bronze)
    triggerShowAnimation(element) {
        element.style.animation = 'bronze-pulse 2s ease-in-out 3';
        setTimeout(() => {
            element.style.animation = '';
        }, 6000);
    },
    
    // Get current results
    getCurrentResults() {
        const winNumber = document.getElementById('winner-win-number');
        const placeNumber = document.getElementById('winner-place-number');
        const showNumber = document.getElementById('winner-show-number');
        
        return {
            win: winNumber?.textContent !== '--' ? parseInt(winNumber.textContent) : null,
            place: placeNumber?.textContent !== '--' ? parseInt(placeNumber.textContent) : null,
            show: showNumber?.textContent !== '--' ? parseInt(showNumber.textContent) : null
        };
    },
    
    // Check if results are set
    hasResults() {
        const results = this.getCurrentResults();
        return results.win !== null || results.place !== null || results.show !== null;
    },
    
    // Export results for sharing/saving
    exportResults() {
        const results = this.getCurrentResults();
        const timestamp = new Date().toISOString();
        
        return {
            results,
            timestamp,
            raceId: `race_${Date.now()}`,
            version: '1.0'
        };
    },
    
    // Import results from saved data
    importResults(data) {
        if (!data || !data.results) {
            DDMDashboard?.showNotification('Invalid results data', 'error');
            return false;
        }
        
        try {
            this.setResults(data.results);
            DDMDashboard?.showNotification('Results imported successfully', 'success');
            return true;
        } catch (error) {
            console.error('Error importing results:', error);
            DDMDashboard?.showNotification('Error importing results', 'error');
            return false;
        }
    }
};

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    RaceResults.init();
});

// Make globally available
window.RaceResults = RaceResults;

// Global functions for backward compatibility
window.resetRaceResults = () => RaceResults.resetRaceResults();
