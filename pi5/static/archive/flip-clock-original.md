# Flip Clock Archive

**Original 3D Split-Flap Flip Clock Implementation**  
Archived: December 2025  
Project: Derby de Mayo (DDM) Multimedia Dashboard

This is the original flip clock code featuring a true 3D split-flap animation effect. Saved for potential reuse in future projects.

---

## HTML Structure

From `pi5/templates/dashboard.html`:

```html
<div class="flip-clock-container">
    <div class="flip-unit">
        <div class="flip-card" id="flip-hour1">
            <div class="flip-card-upper">
                <div class="flip-card-upper-inner">0</div>
            </div>
            <div class="flip-card-lower">
                <div class="flip-card-lower-inner">0</div>
            </div>
            <div class="flip-card-flipping">
                <div class="flip-card-flipping-inner">0</div>
            </div>
        </div>
    </div>
    <div class="flip-unit">
        <div class="flip-card" id="flip-hour2">
            <div class="flip-card-upper">
                <div class="flip-card-upper-inner">0</div>
            </div>
            <div class="flip-card-lower">
                <div class="flip-card-lower-inner">0</div>
            </div>
            <div class="flip-card-flipping">
                <div class="flip-card-flipping-inner">0</div>
            </div>
        </div>
    </div>
    <div class="flip-separator">:</div>
    <div class="flip-unit">
        <div class="flip-card" id="flip-min1">
            <div class="flip-card-upper">
                <div class="flip-card-upper-inner">0</div>
            </div>
            <div class="flip-card-lower">
                <div class="flip-card-lower-inner">0</div>
            </div>
            <div class="flip-card-flipping">
                <div class="flip-card-flipping-inner">0</div>
            </div>
        </div>
    </div>
    <div class="flip-unit">
        <div class="flip-card" id="flip-min2">
            <div class="flip-card-upper">
                <div class="flip-card-upper-inner">0</div>
            </div>
            <div class="flip-card-lower">
                <div class="flip-card-lower-inner">0</div>
            </div>
            <div class="flip-card-flipping">
                <div class="flip-card-flipping-inner">0</div>
            </div>
        </div>
    </div>
    <div class="flip-separator">:</div>
    <div class="flip-unit flip-unit-ampm">
        <div class="flip-card" id="flip-ampm">
            <div class="flip-card-upper">
                <div class="flip-card-upper-inner">AM</div>
            </div>
            <div class="flip-card-lower">
                <div class="flip-card-lower-inner">AM</div>
            </div>
            <div class="flip-card-flipping">
                <div class="flip-card-flipping-inner">AM</div>
            </div>
        </div>
    </div>
</div>
```

---

## CSS Styles

From `pi5/static/css/ddm_style.css`:

```css
/* Flip Clock Container - Single Housing Unit */
.flip-clock-container {
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 12px 16px;
    background: #1a1a1a;
    border-radius: 8px;
    box-shadow: 
        0 4px 12px rgba(0, 0, 0, 0.5),
        inset 0 1px 0 rgba(255, 255, 255, 0.05);
}

.flip-unit {
    position: relative;
    width: 36px;
    height: 52px;
    perspective: 300px;
}

.flip-unit-ampm {
    width: 45px;
}

.flip-unit-ampm .flip-card-upper-inner,
.flip-unit-ampm .flip-card-lower-inner,
.flip-unit-ampm .flip-card-flipping-inner {
    font-size: 1.4em;
}

.flip-card {
    position: relative;
    width: 100%;
    height: 100%;
}

/* Upper Half - Static */
.flip-card-upper {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 50%;
    overflow: hidden;
    background: #2a2a2a;
    border-radius: 3px 3px 0 0;
}

.flip-card-upper-inner {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 200%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: 'Courier New', monospace;
    font-size: 2.2em;
    font-weight: bold;
    color: #f0f0f0;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8);
}

/* Lower Half - Static */
.flip-card-lower {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: 50%;
    overflow: hidden;
    background: #222;
    border-radius: 0 0 3px 3px;
    border-top: 1px solid #000;
}

.flip-card-lower-inner {
    position: absolute;
    top: -100%;
    left: 0;
    right: 0;
    height: 200%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: 'Courier New', monospace;
    font-size: 2.2em;
    font-weight: bold;
    color: #f0f0f0;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8);
}

/* Flipping Half - 3D Animation */
.flip-card-flipping {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 50%;
    overflow: hidden;
    transform-origin: bottom;
    transform-style: preserve-3d;
    backface-visibility: hidden;
    background: #2a2a2a;
    border-radius: 3px 3px 0 0;
    opacity: 0;
    pointer-events: none;
}

.flip-card-flipping-inner {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 200%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: 'Courier New', monospace;
    font-size: 2.2em;
    font-weight: bold;
    color: #f0f0f0;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8);
}

/* Flip Animation - Top Half Falls Forward */
.flip-card.flipping .flip-card-flipping {
    opacity: 1;
    animation: flipDown 0.5s cubic-bezier(0.455, 0.03, 0.515, 0.955);
}

@keyframes flipDown {
    0% {
        transform: rotateX(0deg);
    }
    100% {
        transform: rotateX(-180deg);
    }
}

/* Separator */
.flip-separator {
    font-size: 2em;
    font-weight: bold;
    color: #666;
    margin: 0 4px;
    opacity: 0.6;
}
```

---

## JavaScript Functions

From `pi5/static/js/ddm_control.js`:

```javascript
// Flip clock animation - true 3D split-flap effect
function flipCard(cardId, newValue) {
    const flipCard = document.getElementById(cardId);
    const upperInner = flipCard.querySelector('.flip-card-upper-inner');
    const lowerInner = flipCard.querySelector('.flip-card-lower-inner');
    const flippingInner = flipCard.querySelector('.flip-card-flipping-inner');
    
    const currentValue = upperInner.textContent;
    
    // Only flip if value changed
    if (currentValue !== newValue) {
        // Set flipping section to show current value
        flippingInner.textContent = currentValue;
        
        // Set lower section to show new value (will be revealed)
        lowerInner.textContent = newValue;
        
        // Start flip animation
        flipCard.classList.add('flipping');
        
        // After flip completes, update upper section and reset
        setTimeout(() => {
            upperInner.textContent = newValue;
            flipCard.classList.remove('flipping');
        }, 500);
    }
}

// Update flip clock (hours, minutes, and AM/PM)
function updateClock() {
    const now = new Date();
    let hours = now.getHours();
    const minutes = now.getMinutes();
    
    // Determine AM/PM
    const ampm = hours >= 12 ? 'PM' : 'AM';
    
    // Convert to 12-hour format
    hours = hours % 12;
    hours = hours ? hours : 12; // 0 should be 12
    
    // Pad with zeros
    const hoursStr = String(hours).padStart(2, '0');
    const minutesStr = String(minutes).padStart(2, '0');
    
    // Update each digit
    flipCard('flip-hour1', hoursStr[0]);
    flipCard('flip-hour2', hoursStr[1]);
    flipCard('flip-min1', minutesStr[0]);
    flipCard('flip-min2', minutesStr[1]);
    
    // Update AM/PM
    flipCard('flip-ampm', ampm);
}

// Initialize clock (called in DOMContentLoaded)
updateClock();
setInterval(updateClock, 1000);
```

---

## Technical Notes

### Animation Technique
- Uses CSS 3D transforms (`rotateX`) to create realistic split-flap effect
- Three-layer system: upper (static), lower (static), flipping (animated)
- The flipping section rotates from 0deg to -180deg over 0.5 seconds
- Perspective of 300px creates depth perception

### Structure
- Each digit/section is a separate `.flip-unit` 
- Each unit contains a `.flip-card` with three display layers
- Separators (`:`) are simple text elements between units
- AM/PM unit is slightly wider to accommodate text

### Update Logic
- Only animates when value changes (prevents unnecessary flips)
- 500ms animation duration matches CSS keyframe timing
- Updates every second via `setInterval`
- Uses 12-hour format with AM/PM indicator

### Styling Details
- Dark theme with subtle highlights and shadows
- Monospace 'Courier New' font for authentic flip-board look
- Housing container with inset light reflection
- Smooth cubic-bezier easing for natural motion

---

**End of Archive**
