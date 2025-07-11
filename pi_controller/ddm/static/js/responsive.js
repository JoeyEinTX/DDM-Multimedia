/**
 * DDM Racing System - Responsive Layout Manager
 * Handles screen orientation detection and dynamic layout adjustments
 */

class DDMResponsiveManager {
    constructor() {
        this.currentOrientation = null;
        this.currentBreakpoint = null;
        this.orientationLocked = false;
        this.theme = 'light';
        this.highContrast = false;
        
        this.init();
    }
    
    init() {
        this.detectInitialState();
        this.setupEventListeners();
        this.loadUserPreferences();
        this.applyInitialStyles();
    }
    
    detectInitialState() {
        this.currentOrientation = this.getOrientation();
        this.currentBreakpoint = this.getBreakpoint();
        
        // Set CSS custom properties for JavaScript access
        document.documentElement.style.setProperty('--js-orientation', this.currentOrientation);
        document.documentElement.style.setProperty('--js-breakpoint', this.currentBreakpoint);
    }
    
    setupEventListeners() {
        // Orientation change
        window.addEventListener('orientationchange', this.handleOrientationChange.bind(this));
        window.addEventListener('resize', this.debounce(this.handleResize.bind(this), 250));
        
        // Theme toggle
        document.addEventListener('DOMContentLoaded', () => {
            this.setupThemeToggle();
            this.setupAccessibilityControls();
        });
        
        // Keyboard shortcuts
        document.addEventListener('keydown', this.handleKeyboardShortcuts.bind(this));
    }
    
    getOrientation() {
        if (window.matchMedia('(orientation: portrait)').matches) {
            return 'portrait';
        } else if (window.matchMedia('(orientation: landscape)').matches) {
            return 'landscape';
        }
        return 'unknown';
    }
    
    getBreakpoint() {
        const width = window.innerWidth;
        
        if (width < 576) return 'xs';
        if (width < 768) return 'sm';
        if (width < 992) return 'md';
        if (width < 1200) return 'lg';
        return 'xl';
    }
    
    handleOrientationChange() {
        if (this.orientationLocked) return;
        
        // Small delay to allow for viewport changes
        setTimeout(() => {
            const newOrientation = this.getOrientation();
            const newBreakpoint = this.getBreakpoint();
            
            if (newOrientation !== this.currentOrientation || newBreakpoint !== this.currentBreakpoint) {
                this.currentOrientation = newOrientation;
                this.currentBreakpoint = newBreakpoint;
                
                this.applyOrientationStyles();
                this.adjustModalLayouts();
                this.triggerOrientationChangeEvent();
            }
        }, 100);
    }
    
    handleResize() {
        const newBreakpoint = this.getBreakpoint();
        
        if (newBreakpoint !== this.currentBreakpoint) {
            this.currentBreakpoint = newBreakpoint;
            this.applyBreakpointStyles();
            this.adjustModalLayouts();
        }
    }
    
    applyInitialStyles() {
        document.body.classList.add(`orientation-${this.currentOrientation}`);
        document.body.classList.add(`breakpoint-${this.currentBreakpoint}`);
        document.body.classList.add(`theme-${this.theme}`);
        
        if (this.highContrast) {
            document.body.classList.add('high-contrast');
        }
    }
    
    applyOrientationStyles() {
        // Remove old orientation classes
        document.body.classList.remove('orientation-portrait', 'orientation-landscape');
        
        // Add new orientation class
        document.body.classList.add(`orientation-${this.currentOrientation}`);
        
        // Update CSS custom property
        document.documentElement.style.setProperty('--js-orientation', this.currentOrientation);
        
        // Log for debugging
        console.log(`Orientation changed to: ${this.currentOrientation}`);
    }
    
    applyBreakpointStyles() {
        // Remove old breakpoint classes
        document.body.classList.remove('breakpoint-xs', 'breakpoint-sm', 'breakpoint-md', 'breakpoint-lg', 'breakpoint-xl');
        
        // Add new breakpoint class
        document.body.classList.add(`breakpoint-${this.currentBreakpoint}`);
        
        // Update CSS custom property
        document.documentElement.style.setProperty('--js-breakpoint', this.currentBreakpoint);
        
        // Log for debugging
        console.log(`Breakpoint changed to: ${this.currentBreakpoint}`);
    }
    
    adjustModalLayouts() {
        const modals = document.querySelectorAll('.modal');
        modals.forEach(modal => {
            this.adjustModalLayout(modal);
        });
    }
    
    adjustModalLayout(modal) {
        const modalDialog = modal.querySelector('.modal-dialog');
        if (!modalDialog) return;
        
        // Remove existing responsive classes
        modalDialog.classList.remove('modal-xs', 'modal-sm', 'modal-md', 'modal-lg', 'modal-xl');
        modalDialog.classList.remove('modal-portrait', 'modal-landscape');
        
        // Add current responsive classes
        modalDialog.classList.add(`modal-${this.currentBreakpoint}`);
        modalDialog.classList.add(`modal-${this.currentOrientation}`);
        
        // Special handling for horse selection modal
        if (modal.id === 'horseSelectionModal') {
            this.adjustHorseSelectionModal(modal);
        }
    }
    
    adjustHorseSelectionModal(modal) {
        const horseGrid = modal.querySelector('.horse-grid');
        const resultsDisplay = modal.querySelector('.results-display');
        
        if (!horseGrid || !resultsDisplay) return;
        
        // Adjust horse grid based on orientation and breakpoint
        if (this.currentOrientation === 'portrait') {
            if (this.currentBreakpoint === 'xs') {
                this.setHorseGridLayout(horseGrid, 2, 10);
            } else if (this.currentBreakpoint === 'sm') {
                this.setHorseGridLayout(horseGrid, 3, 7);
            } else {
                this.setHorseGridLayout(horseGrid, 4, 5);
            }
        } else {
            if (this.currentBreakpoint === 'xs' || this.currentBreakpoint === 'sm') {
                this.setHorseGridLayout(horseGrid, 4, 5);
            } else {
                this.setHorseGridLayout(horseGrid, 5, 4);
            }
        }
        
        // Adjust results display layout
        const modalBody = modal.querySelector('.modal-body .row');
        if (modalBody) {
            if (this.currentOrientation === 'portrait' && this.currentBreakpoint === 'xs') {
                // Stack vertically on small portrait screens
                modalBody.style.flexDirection = 'column';
                modalBody.querySelector('.col-12.col-lg-9').style.order = '2';
                modalBody.querySelector('.col-12.col-lg-3').style.order = '1';
            } else {
                // Normal horizontal layout
                modalBody.style.flexDirection = 'row';
                modalBody.querySelector('.col-12.col-lg-9').style.order = '1';
                modalBody.querySelector('.col-12.col-lg-3').style.order = '2';
            }
        }
    }
    
    setHorseGridLayout(grid, cols, rows) {
        grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
        grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;
    }
    
    triggerOrientationChangeEvent() {
        const event = new CustomEvent('ddm:orientationchange', {
            detail: {
                orientation: this.currentOrientation,
                breakpoint: this.currentBreakpoint
            }
        });
        document.dispatchEvent(event);
    }
    
    setupThemeToggle() {
        const themeToggle = document.querySelector('.theme-toggle');
        if (themeToggle) {
            themeToggle.addEventListener('click', this.toggleTheme.bind(this));
        }
    }
    
    setupAccessibilityControls() {
        // High contrast toggle
        const contrastToggle = document.querySelector('.contrast-toggle');
        if (contrastToggle) {
            contrastToggle.addEventListener('click', this.toggleHighContrast.bind(this));
        }
        
        // Reduced motion detection
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
            document.body.classList.add('reduced-motion');
        }
    }
    
    toggleTheme() {
        this.theme = this.theme === 'light' ? 'dark' : 'light';
        document.body.classList.remove('theme-light', 'theme-dark');
        document.body.classList.add(`theme-${this.theme}`);
        document.documentElement.setAttribute('data-theme', this.theme);
        
        this.saveUserPreferences();
        
        // Update theme toggle button
        const themeToggle = document.querySelector('.theme-toggle');
        if (themeToggle) {
            themeToggle.textContent = this.theme === 'light' ? '🌙' : '☀️';
            themeToggle.title = `Switch to ${this.theme === 'light' ? 'dark' : 'light'} theme`;
        }
        
        // Trigger theme change event
        const event = new CustomEvent('ddm:themechange', {
            detail: { theme: this.theme }
        });
        document.dispatchEvent(event);
    }
    
    toggleHighContrast() {
        this.highContrast = !this.highContrast;
        document.body.classList.toggle('high-contrast', this.highContrast);
        this.saveUserPreferences();
    }
    
    lockOrientation(orientation = null) {
        this.orientationLocked = orientation ? true : false;
        
        if (orientation) {
            document.body.classList.add('orientation-locked');
            document.body.classList.add(`orientation-locked-${orientation}`);
        } else {
            document.body.classList.remove('orientation-locked');
            document.body.classList.remove('orientation-locked-portrait', 'orientation-locked-landscape');
        }
        
        this.saveUserPreferences();
    }
    
    handleKeyboardShortcuts(event) {
        // Alt + T: Toggle theme
        if (event.altKey && event.key === 't') {
            event.preventDefault();
            this.toggleTheme();
        }
        
        // Alt + C: Toggle high contrast
        if (event.altKey && event.key === 'c') {
            event.preventDefault();
            this.toggleHighContrast();
        }
        
        // Alt + O: Lock/unlock orientation
        if (event.altKey && event.key === 'o') {
            event.preventDefault();
            this.lockOrientation(this.orientationLocked ? null : this.currentOrientation);
        }
    }
    
    saveUserPreferences() {
        const preferences = {
            theme: this.theme,
            highContrast: this.highContrast,
            orientationLocked: this.orientationLocked
        };
        
        localStorage.setItem('ddm-preferences', JSON.stringify(preferences));
    }
    
    loadUserPreferences() {
        const stored = localStorage.getItem('ddm-preferences');
        if (stored) {
            try {
                const preferences = JSON.parse(stored);
                this.theme = preferences.theme || 'light';
                this.highContrast = preferences.highContrast || false;
                this.orientationLocked = preferences.orientationLocked || false;
            } catch (e) {
                console.warn('Failed to load user preferences:', e);
            }
        }
    }
    
    // Utility function for debouncing
    debounce(func, wait) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    }
    
    // Public API methods
    getCurrentState() {
        return {
            orientation: this.currentOrientation,
            breakpoint: this.currentBreakpoint,
            theme: this.theme,
            highContrast: this.highContrast,
            orientationLocked: this.orientationLocked
        };
    }
    
    setTheme(theme) {
        if (['light', 'dark'].includes(theme)) {
            this.theme = theme;
            document.body.classList.remove('theme-light', 'theme-dark');
            document.body.classList.add(`theme-${this.theme}`);
            document.documentElement.setAttribute('data-theme', this.theme);
            this.saveUserPreferences();
        }
    }
    
    // Force a layout recalculation
    recalculateLayout() {
        this.handleOrientationChange();
        this.handleResize();
    }
}

// Initialize the responsive manager
const ddmResponsive = new DDMResponsiveManager();

// Export for use in other scripts
window.DDMResponsive = ddmResponsive;

// Event listeners for other scripts to use
document.addEventListener('ddm:orientationchange', (event) => {
    console.log('DDM Orientation changed:', event.detail);
});

document.addEventListener('ddm:themechange', (event) => {
    console.log('DDM Theme changed:', event.detail);
});

// Create theme toggle button if it doesn't exist
document.addEventListener('DOMContentLoaded', () => {
    if (!document.querySelector('.theme-toggle')) {
        const themeToggle = document.createElement('button');
        themeToggle.className = 'theme-toggle';
        themeToggle.textContent = ddmResponsive.theme === 'light' ? '🌙' : '☀️';
        themeToggle.title = `Switch to ${ddmResponsive.theme === 'light' ? 'dark' : 'light'} theme`;
        themeToggle.addEventListener('click', () => ddmResponsive.toggleTheme());
        document.body.appendChild(themeToggle);
    }
});
