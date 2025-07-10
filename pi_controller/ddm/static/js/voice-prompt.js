/**
 * Voice-Activated Animation Prompt System
 * For DDM Racing System - Voice to LED Animation
 */

class VoiceAnimationPrompt {
    constructor() {
        this.recognition = null;
        this.isListening = false;
        this.maxWords = 10;
        this.currentModal = null;
        
        // Initialize speech recognition
        this.initSpeechRecognition();
        
        // Bind methods
        this.startListening = this.startListening.bind(this);
        this.stopListening = this.stopListening.bind(this);
        this.processResult = this.processResult.bind(this);
    }
    
    initSpeechRecognition() {
        // Check for Web Speech API support
        if ('webkitSpeechRecognition' in window) {
            this.recognition = new webkitSpeechRecognition();
        } else if ('SpeechRecognition' in window) {
            this.recognition = new SpeechRecognition();
        } else {
            console.warn('Web Speech API not supported in this browser');
            return;
        }
        
        // Configure recognition
        this.recognition.continuous = false;
        this.recognition.interimResults = false;
        this.recognition.lang = 'en-US';
        this.recognition.maxAlternatives = 1;
        
        // Event handlers
        this.recognition.onstart = () => {
            this.isListening = true;
            this.showListeningModal();
        };
        
        this.recognition.onresult = (event) => {
            const transcript = event.results[0][0].transcript;
            this.processResult(transcript);
        };
        
        this.recognition.onerror = (event) => {
            console.error('Speech recognition error:', event.error);
            this.showErrorModal(`Speech recognition error: ${event.error}`);
            this.isListening = false;
        };
        
        this.recognition.onend = () => {
            this.isListening = false;
        };
    }
    
    isSupported() {
        return this.recognition !== null;
    }
    
    startListening() {
        if (!this.isSupported()) {
            this.showErrorModal('Voice recognition not supported in this browser');
            return;
        }
        
        if (this.isListening) {
            return;
        }
        
        try {
            this.recognition.start();
        } catch (error) {
            console.error('Error starting speech recognition:', error);
            this.showErrorModal('Error starting voice recognition');
        }
    }
    
    stopListening() {
        if (this.recognition && this.isListening) {
            this.recognition.stop();
        }
    }
    
    processResult(transcript) {
        const words = transcript.trim().split(/\s+/);
        const wordCount = words.length;
        
        this.showResultModal(transcript, wordCount);
    }
    
    showListeningModal() {
        const modal = this.createModal(`
            <div class="voice-modal listening">
                <div class="voice-icon">🎤</div>
                <h3>Listening...</h3>
                <p>Speak your animation idea (max 10 words)</p>
                <div class="voice-animation">
                    <div class="pulse"></div>
                </div>
                <button onclick="voicePrompt.stopListening()" class="btn btn-secondary">Cancel</button>
            </div>
        `);
        
        this.currentModal = modal;
    }
    
    showResultModal(transcript, wordCount) {
        const isValidLength = wordCount <= this.maxWords;
        const statusClass = isValidLength ? 'success' : 'error';
        const statusMessage = isValidLength 
            ? `Got it! (${wordCount}/${this.maxWords} words)`
            : `Too long! (${wordCount}/${this.maxWords} words)`;
        
        const modal = this.createModal(`
            <div class="voice-modal result ${statusClass}">
                <div class="voice-icon">${isValidLength ? '✅' : '❌'}</div>
                <h3>${statusMessage}</h3>
                <div class="transcript">"${transcript}"</div>
                <div class="modal-actions">
                    ${isValidLength ? `
                        <button onclick="voicePrompt.sendPrompt('${transcript.replace(/'/g, "\\'")}')" class="btn btn-primary">
                            ✨ Generate Animation
                        </button>
                    ` : ''}
                    <button onclick="voicePrompt.startListening()" class="btn btn-secondary">
                        🎤 Try Again
                    </button>
                    <button onclick="voicePrompt.closeModal()" class="btn btn-outline">
                        Cancel
                    </button>
                </div>
            </div>
        `);
        
        this.currentModal = modal;
    }
    
    showErrorModal(message) {
        const modal = this.createModal(`
            <div class="voice-modal error">
                <div class="voice-icon">❌</div>
                <h3>Error</h3>
                <p>${message}</p>
                <button onclick="voicePrompt.closeModal()" class="btn btn-secondary">Close</button>
            </div>
        `);
        
        this.currentModal = modal;
    }
    
    showLoadingModal() {
        const modal = this.createModal(`
            <div class="voice-modal loading">
                <div class="voice-icon">🎨</div>
                <h3>Creating Animation...</h3>
                <div class="loading-spinner"></div>
                <p>AI is generating your LED sequence</p>
            </div>
        `);
        
        this.currentModal = modal;
    }
    
    createModal(content) {
        // Remove existing modal
        this.closeModal();
        
        const modalOverlay = document.createElement('div');
        modalOverlay.className = 'voice-modal-overlay';
        modalOverlay.innerHTML = `
            <div class="voice-modal-container">
                ${content}
            </div>
        `;
        
        // Close on overlay click
        modalOverlay.onclick = (e) => {
            if (e.target === modalOverlay) {
                this.closeModal();
            }
        };
        
        document.body.appendChild(modalOverlay);
        return modalOverlay;
    }
    
    closeModal() {
        if (this.currentModal) {
            this.currentModal.remove();
            this.currentModal = null;
        }
    }
    
    async sendPrompt(prompt) {
        this.showLoadingModal();
        
        try {
            const response = await fetch('/api/openai/voice-prompt', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ prompt: prompt })
            });
            
            const data = await response.json();
            
            if (data.success) {
                this.showSuccessModal(data);
                
                // Trigger animation if callback is available
                if (window.onVoiceAnimationGenerated) {
                    window.onVoiceAnimationGenerated(data);
                }
            } else {
                this.showErrorModal(data.error || 'Failed to generate animation');
            }
        } catch (error) {
            console.error('Error sending voice prompt:', error);
            this.showErrorModal('Network error. Please try again.');
        }
    }
    
    showSuccessModal(data) {
        const modal = this.createModal(`
            <div class="voice-modal success">
                <div class="voice-icon">🎉</div>
                <h3>Animation Created!</h3>
                <div class="prompt-info">
                    <strong>Prompt:</strong> "${data.prompt}"
                </div>
                <div class="sequence-preview">
                    <strong>Generated:</strong>
                    <pre>${JSON.stringify(data.sequence, null, 2)}</pre>
                </div>
                <div class="modal-actions">
                    <button onclick="voicePrompt.applyAnimation()" class="btn btn-primary">
                        🚀 Apply to LEDs
                    </button>
                    <button onclick="voicePrompt.startListening()" class="btn btn-secondary">
                        🎤 Create Another
                    </button>
                    <button onclick="voicePrompt.closeModal()" class="btn btn-outline">
                        Close
                    </button>
                </div>
            </div>
        `);
        
        this.currentModal = modal;
        this.lastGeneratedSequence = data.sequence;
    }
    
    applyAnimation() {
        if (this.lastGeneratedSequence) {
            // Trigger animation application
            if (window.onApplyVoiceAnimation) {
                window.onApplyVoiceAnimation(this.lastGeneratedSequence);
            }
            
            // Show feedback
            this.showFeedbackModal('Animation applied to LEDs!');
        }
    }
    
    showFeedbackModal(message) {
        const modal = this.createModal(`
            <div class="voice-modal feedback">
                <div class="voice-icon">✨</div>
                <h3>${message}</h3>
                <button onclick="voicePrompt.closeModal()" class="btn btn-primary">Awesome!</button>
            </div>
        `);
        
        this.currentModal = modal;
        
        // Auto-close after 2 seconds
        setTimeout(() => {
            this.closeModal();
        }, 2000);
    }
}

// Initialize global instance
const voicePrompt = new VoiceAnimationPrompt();

// Export for use in other modules
window.voicePrompt = voicePrompt;
