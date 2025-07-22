console.log('horse-selection.js loaded');
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
    // Reset all selections
    resetSelections() {
        console.log('Resetting selections...');

        // Reset selection state
        this.state.step = 0;
        this.state.selections = { win: null, place: null, show: null };

        // UI will be rendered on modal show; if modal is open, force render now
        const modalEl = document.getElementById('horseSelectionModal');
        if (modalEl && modalEl.classList.contains('show')) {
            this.renderModalUIFromState();
            // Also update the horse grid to clear button states
            this.updateHorseGrid();
        }

        DDMDashboard?.showNotification('Selections reset', 'info');
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

        // Show modal (Bootstrap 5)
        const modalEl = document.getElementById('horseSelectionModal');
        if (modalEl) {
            const modal = new bootstrap.Modal(modalEl);
            modal.show();
        } else {
            console.error('horseSelectionModal element not found in DOM!');
        }
        // No direct UI update here; UI will be rendered on modal show
    },
    
    // Render all modal UI from current state
    renderModalUIFromState() {
        // Only update UI state here; grid is built in shown.bs.modal

        // Update results display
        this.updateResultsDisplay();
        // Update selection step
        this.updateSelectionStep();

        // Remove selected and animation classes from modal result boxes if not selected
        ['win-box','place-box','show-box'].forEach(cls => {
            const box = document.querySelector('.' + cls);
            if (box && !this.state.selections[cls.replace('-box','')]) {
                box.classList.remove('selected','pixelate-in');
            }
        });

        // Remove reveal classes if not selected
        const winNumber = document.getElementById('win-number');
        const placeNumber = document.getElementById('place-number');
        const showNumber = document.getElementById('show-number');
        if (!this.state.selections.win) winNumber?.classList.remove('reveal');
        if (!this.state.selections.place) placeNumber?.classList.remove('reveal');
        if (!this.state.selections.show) showNumber?.classList.remove('reveal');

        // Disable finalize button if not all selections
        const finalizeBtn = document.getElementById('finalize-results-btn');
        if (finalizeBtn) {
            const hasAllSelections = this.state.selections.win !== null && 
                                   this.state.selections.place !== null && 
                                   this.state.selections.show !== null;
            finalizeBtn.disabled = !hasAllSelections;
        }
    },
    
    // Create the grid of horse number buttons
    createHorseGrid(modalEl) {
        // Scope the search to the modal element
        console.log('[DEBUG] createHorseGrid called. modalEl:', modalEl);
        const gridContainer = modalEl.querySelector('.horse-grid');
        if (!gridContainer) {
            console.error('No .horse-grid found in DOM! Modal HTML:', modalEl.innerHTML);
            // Try to show a visible error in the modal if possible
            const modalBody = modalEl.querySelector('.modal-body');
            if (modalBody) {
                const errorDiv = document.createElement('div');
                errorDiv.textContent = 'ERROR: .horse-grid container NOT FOUND! Please check the modal HTML.';
                errorDiv.style.background = 'red';
                errorDiv.style.color = 'white';
                errorDiv.style.padding = '2em';
                errorDiv.style.fontWeight = 'bold';
                errorDiv.style.fontSize = '1.5em';
                errorDiv.style.textAlign = 'center';
                errorDiv.style.margin = '2em 0';
                modalBody.appendChild(errorDiv);
            }
            return;
        }

        gridContainer.innerHTML = '';
        gridContainer.style.minHeight = '200px';
        for (let i = 1; i <= 20; i++) {
            const button = document.createElement('button');
            button.className = 'horse-number';
            button.textContent = i;
            button.onclick = () => this.selectHorse(i);
            gridContainer.appendChild(button);
        }
        console.log('Horse grid HTML:', gridContainer.innerHTML);
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
            const modalElement = document.getElementById('horseSelectionModal');
            let modal = null;
            if (window.bootstrap && window.bootstrap.Modal && modalElement) {
                modal = window.bootstrap.Modal.getInstance(modalElement) || new window.bootstrap.Modal(modalElement);
            }
            if (modal) {
                console.log('Closing modal with Bootstrap instance...');
                modal.hide();
            } else if (modalElement) {
                console.warn('Bootstrap modal instance not found, forcibly hiding modal.');
                modalElement.classList.remove('show');
                modalElement.style.display = 'none';
                document.body.classList.remove('modal-open');
                const backdrop = document.querySelector('.modal-backdrop');
                if (backdrop) backdrop.remove();
            } else {
                console.error('Modal element not found, cannot close modal.');
            }
        } catch (modalError) {
            console.error('Error closing modal:', modalError);
            DDMDashboard?.showNotification('Error closing modal', 'error');
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
    }
};

// Make resetSelections globally available
window.resetSelections = () => HorseSelection.resetSelections();

document.addEventListener('DOMContentLoaded', function () {
    HorseSelection.init();

    // Attach show.bs.modal event to always render UI from state before modal is shown
    const modalEl = document.getElementById('horseSelectionModal');
    if (modalEl) {
        modalEl.addEventListener('show.bs.modal', function (event) {
            console.log('[Bootstrap Event] show.bs.modal fired, rendering modal UI from state...');
            HorseSelection.renderModalUIFromState();
        });
        modalEl.addEventListener('shown.bs.modal', function (event) {
            console.log('[Bootstrap Event] shown.bs.modal fired, building horse grid...');
            HorseSelection.createHorseGrid(modalEl);
            // After grid is built, update UI state
            HorseSelection.renderModalUIFromState();
        });
    } else {
        console.error('horseSelectionModal element not found in DOM when attaching modal events!');
    }

    // Make globally available
    window.HorseSelection = HorseSelection;
    window.openHorseSelectionModal = () => HorseSelection.openModal();
    window.finalizeResults = () => HorseSelection.finalizeResults();
    // window.resetSelections removed
});
