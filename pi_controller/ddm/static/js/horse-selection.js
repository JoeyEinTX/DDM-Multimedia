/**
 * DDM Racing System - Horse Selection Modal
 * Handles horse selection logic, modal interactions, and results finalization
 */

const HorseSelection = {
    // Selection state
    state: {
        step: 0, // 0=win, 1=place, 2=show
        selections: { win: null, place: null, show: null },
        stepLabels: ['SELECT WINNING HORSE', 'SELECT PLACE HORSE', 'SELECT SHOW HORSE']
    },
    
    // Initialize horse selection functionality
    init() {
        console.log('🐎 Initializing Horse Selection...');
        this.setupEventListeners();
    },
    
    // Set up event listeners
    setupEventListeners() {
        // Reset selections button
        const resetBtn = document.getElementById('reset-selections-btn');
        if (resetBtn) {
            resetBtn.addEventListener('click', this.resetSelections.bind(this));
        }
    },
    
    // Open horse selection modal
    openModal() {
        console.log('Opening horse selection modal...');
        
        // Check if there are existing results from the main dashboard
        const existingWin = document.getElementById('winner-win-number')?.textContent;
        const existingPlace = document.getElementById('winner-place-number')?.textContent;
        const existingShow = document.getElementById('winner-show-number')?.textContent;
        
        // Initialize selection state based on existing results
        if (existingWin !== '--' || existingPlace !== '--' || existingShow !== '--') {
            // Load existing results into selection state
            this.state = {
                step: 2, // Set to final step since we have results
                selections: { 
                    win: existingWin !== '--' ? parseInt(existingWin) : null,
                    place: existingPlace !== '--' ? parseInt(existingPlace) : null,
                    show: existingShow !== '--' ? parseInt(existingShow) : null
                },
                stepLabels: ['SELECT WINNING HORSE', 'SELECT PLACE HORSE', 'SELECT SHOW HORSE']
            };
        } else {
            // Reset state for new selection
            this.state = {
                step: 0,
                selections: { win: null, place: null, show: null },
                stepLabels: ['SELECT WINNING HORSE', 'SELECT PLACE HORSE', 'SELECT SHOW HORSE']
            };
        }
        
        // Create horse grid
        this.createHorseGrid();
        this.updateSelectionStep();
        this.updateResultsDisplay();
        
        // Ensure finalize button state is correct on modal open
        const finalizeBtn = document.getElementById('finalize-results-btn');
        if (finalizeBtn) {
            const hasAllSelections = this.state.selections.win !== null && 
                                   this.state.selections.place !== null && 
                                   this.state.selections.show !== null;
            finalizeBtn.disabled = !hasAllSelections;
            console.log('Modal opened - button state set:', {
                selections: this.state.selections,
                hasAllSelections,
                buttonDisabled: finalizeBtn.disabled
            });
        }
        
        // Show modal
        const modal = new bootstrap.Modal(document.getElementById('horseSelectionModal'));
        modal.show();
    },
    
    // Create the grid of horse number buttons
    createHorseGrid() {
        const gridContainer = document.querySelector('.horse-grid');
        if (!gridContainer) return;
        
        gridContainer.innerHTML = '';
        
        // Create 20 horse number buttons
        for (let i = 1; i <= 20; i++) {
            const button = document.createElement('button');
            button.className = 'horse-number';
            button.textContent = i;
            button.onclick = () => this.selectHorse(i);
            gridContainer.appendChild(button);
        }
        
        this.updateHorseGrid();
    },
    
    // Select a horse for the current step
    selectHorse(number) {
        console.log(`Selecting horse #${number} for step ${this.state.step}`);
        
        const stepKeys = ['win', 'place', 'show'];
        const currentStep = stepKeys[this.state.step];
        
        // Check if horse already selected for a different position
        if (Object.values(this.state.selections).includes(number)) {
            DDMDashboard?.showNotification(`Horse #${number} already selected!`, 'warning');
            return;
        }
        
        // Record selection
        this.state.selections[currentStep] = number;
        
        // Update display
        this.updateResultsDisplay();
        this.updateHorseGrid();
        
        // Move to next step or finish
        if (this.state.step < 2) {
            this.state.step++;
            this.updateSelectionStep();
        } else {
            // All selections made - enable finalize button
            const finalizeBtn = document.getElementById('finalize-results-btn');
            if (finalizeBtn) {
                finalizeBtn.disabled = false;
                console.log('All selections complete - finalize button enabled');
            }
            document.getElementById('selection-step').textContent = 'RESULTS COMPLETE - READY TO FINALIZE';
        }
    },
    
    // Update the selection step display
    updateSelectionStep() {
        const stepLabel = document.getElementById('selection-step');
        if (stepLabel) {
            stepLabel.textContent = this.state.stepLabels[this.state.step];
        }
        
        // Update finalize button state
        const finalizeBtn = document.getElementById('finalize-results-btn');
        if (finalizeBtn) {
            const hasAllSelections = this.state.selections.win !== null && 
                                   this.state.selections.place !== null && 
                                   this.state.selections.show !== null;
            finalizeBtn.disabled = !hasAllSelections;
            
            // Debug log
            console.log('Button state update:', {
                win: this.state.selections.win,
                place: this.state.selections.place,
                show: this.state.selections.show,
                hasAllSelections,
                buttonDisabled: finalizeBtn.disabled
            });
        }
    },
    
    // Update the results display in the modal
    updateResultsDisplay() {
        const winElement = document.getElementById('win-number');
        const placeElement = document.getElementById('place-number');
        const showElement = document.getElementById('show-number');
        
        if (!winElement || !placeElement || !showElement) return;
        
        // Get current step info - step hasn't been incremented yet when this is called
        const stepKeys = ['win', 'place', 'show'];
        const currentStep = stepKeys[this.state.step];
        
        // Update all numbers without animation first
        winElement.textContent = this.state.selections.win || '?';
        placeElement.textContent = this.state.selections.place || '?';
        showElement.textContent = this.state.selections.show || '?';
        
        // Only animate the current selection
        if (currentStep === 'win' && this.state.selections.win) {
            winElement.classList.add('reveal');
            setTimeout(() => winElement.classList.remove('reveal'), 1500);
        } else if (currentStep === 'place' && this.state.selections.place) {
            placeElement.classList.add('reveal');
            setTimeout(() => placeElement.classList.remove('reveal'), 1500);
        } else if (currentStep === 'show' && this.state.selections.show) {
            showElement.classList.add('reveal');
            setTimeout(() => showElement.classList.remove('reveal'), 1500);
        }
        
        // Update box states with pixelate animation
        const winBox = document.querySelector('.win-box');
        const placeBox = document.querySelector('.place-box');
        const showBox = document.querySelector('.show-box');
        
        // Update selected states and trigger pixelate animation for new selections
        if (this.state.selections.win && !winBox?.classList.contains('selected')) {
            winBox?.classList.add('selected');
            winBox?.classList.add('pixelate-in');
            setTimeout(() => winBox?.classList.remove('pixelate-in'), 600);
        }
        if (this.state.selections.place && !placeBox?.classList.contains('selected')) {
            placeBox?.classList.add('selected');
            placeBox?.classList.add('pixelate-in');
            setTimeout(() => placeBox?.classList.remove('pixelate-in'), 600);
        }
        if (this.state.selections.show && !showBox?.classList.contains('selected')) {
            showBox?.classList.add('selected');
            showBox?.classList.add('pixelate-in');
            setTimeout(() => showBox?.classList.remove('pixelate-in'), 600);
        }
    },
    
    // Update the horse grid button states
    updateHorseGrid() {
        const buttons = document.querySelectorAll('.horse-number');
        
        buttons.forEach(button => {
            const number = parseInt(button.textContent);
            
            // Reset all classes
            button.classList.remove('selected', 'win', 'place', 'show', 'disabled');
            
            // Add appropriate selection class
            if (this.state.selections.win === number) {
                button.classList.add('selected', 'win');
            } else if (this.state.selections.place === number) {
                button.classList.add('selected', 'place');
            } else if (this.state.selections.show === number) {
                button.classList.add('selected', 'show');
            }
            
            // Disable already selected horses for future selections
            if (this.state.step < 2 && Object.values(this.state.selections).includes(number)) {
                button.classList.add('disabled');
            }
        });
    },
    
    // Finalize the race results
    async finalizeResults() {
        console.log('=== FINALIZE RESULTS CLICKED ===');
        console.log('Current selection state:', this.state);
        
        // Validate we have all selections
        if (!this.state.selections.win || !this.state.selections.place || !this.state.selections.show) {
            console.error('Missing selections:', this.state.selections);
            DDMDashboard?.showNotification('Please select Win, Place, and Show horses first!', 'error');
            return;
        }
        
        // Update Winners Panel immediately (offline mode)
        this.updateWinnersPanel(this.state.selections);
        DDMDashboard?.showNotification(
            `Results Finalized! Win: #${this.state.selections.win}, Place: #${this.state.selections.place}, Show: #${this.state.selections.show}`, 
            'success'
        );
        
        // Close modal immediately
        try {
            const modal = bootstrap.Modal.getInstance(document.getElementById('horseSelectionModal'));
            if (modal) {
                console.log('Closing modal...');
                modal.hide();
            } else {
                console.log('Modal instance not found, trying alternative close...');
                const modalElement = document.getElementById('horseSelectionModal');
                if (modalElement) {
                    modalElement.style.display = 'none';
                    document.body.classList.remove('modal-open');
                    const backdrop = document.querySelector('.modal-backdrop');
                    if (backdrop) backdrop.remove();
                }
            }
        } catch (modalError) {
            console.error('Error closing modal:', modalError);
        }
        
        // Update reset button state
        DDMDashboard?.updateResetButtonState();
        
        // Try to send to API in background (don't block modal close)
        try {
            console.log('Sending API request with:', {
                animation: 'race_results',
                mode: 'results',
                results: this.state.selections
            });
            
            const response = await fetch('/api/display/command', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    animation: 'race_results',
                    mode: 'results',
                    results: this.state.selections
                })
            });
            
            console.log('API Response status:', response.status);
            const data = await response.json();
            console.log('API Response data:', data);
            
            if (!data.success) {
                console.error('API returned error:', data.error);
                // Don't show error notification since results are already saved locally
            }
        } catch (error) {
            console.error('API Error (background):', error);
            // Don't show error notification since results are already saved locally
        }
    },
    
    // Update the winners panel in the main dashboard
    updateWinnersPanel(results) {
        // Update numbers
        const winNumber = document.getElementById('winner-win-number');
        const placeNumber = document.getElementById('winner-place-number');
        const showNumber = document.getElementById('winner-show-number');
        
        if (winNumber) winNumber.textContent = results.win || '--';
        if (placeNumber) placeNumber.textContent = results.place || '--';
        if (showNumber) showNumber.textContent = results.show || '--';
        
        // Add active animations
        const winItem = document.getElementById('winner-win');
        const placeItem = document.getElementById('winner-place');
        const showItem = document.getElementById('winner-show');
        
        if (results.win && winItem) {
            winItem.classList.add('active');
        }
        if (results.place && placeItem) {
            placeItem.classList.add('active');
        }
        if (results.show && showItem) {
            showItem.classList.add('active');
        }
        
        // Update timestamp
        const timestamp = document.getElementById('results-timestamp');
        if (timestamp) {
            timestamp.textContent = `Last updated: ${new Date().toLocaleTimeString()}`;
        }
    },
    
    // Reset all selections
    resetSelections() {
        console.log('Resetting selections...');
        
        // Reset selection state
        this.state.step = 0;
        this.state.selections = { win: null, place: null, show: null };
        
        // Reset display
        const winNumber = document.getElementById('win-number');
        const placeNumber = document.getElementById('place-number');
        const showNumber = document.getElementById('show-number');
        
        if (winNumber) winNumber.textContent = '?';
        if (placeNumber) placeNumber.textContent = '?';
        if (showNumber) showNumber.textContent = '?';
        
        // Remove selected classes
        document.querySelector('.win-box')?.classList.remove('selected');
        document.querySelector('.place-box')?.classList.remove('selected');
        document.querySelector('.show-box')?.classList.remove('selected');
        
        // Update step label
        this.updateSelectionStep();
        
        // Update horse grid
        this.updateHorseGrid();
        
        // Disable finalize button
        const finalizeBtn = document.getElementById('finalize-results-btn');
        if (finalizeBtn) {
            finalizeBtn.disabled = true;
        }
        
        DDMDashboard?.showNotification('Selections reset', 'info');
    }
};

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    HorseSelection.init();
});

// Make globally available
window.HorseSelection = HorseSelection;

// Global functions for backward compatibility
window.openHorseSelectionModal = () => HorseSelection.openModal();
window.finalizeResults = () => HorseSelection.finalizeResults();
window.resetSelections = () => HorseSelection.resetSelections();
