// animations.cpp - Animation implementations for DDM Cup Project

#include "animations.h"

// Animation state variables
AnimationState currentAnimation = ANIM_NONE;
unsigned long animationStartTime = 0;
ResultsData results = {0, 0, 0, false};

/**
 * Start an animation
 */
void startAnimation(AnimationState anim) {
    currentAnimation = anim;
    animationStartTime = millis();
    Serial.println("[ANIM] Starting: " + getAnimationName(anim));
}

/**
 * Stop current animation
 */
void stopAnimation() {
    currentAnimation = ANIM_NONE;
    clearAllLEDs();
    Serial.println("[ANIM] Stopped");
}

/**
 * Update current animation (call in loop)
 */
void updateAnimation() {
    switch (currentAnimation) {
        case ANIM_IDLE:
            animIdle();
            break;
        case ANIM_WELCOME:
            animWelcome();
            break;
        case ANIM_BETTING_60:
            animBetting60();
            break;
        case ANIM_BETTING_30:
            animBetting30();
            break;
        case ANIM_FINAL_CALL:
            animFinalCall();
            break;
        case ANIM_RACE_START:
            animRaceStart();
            break;
        case ANIM_CHAOS:
            animChaos();
            break;
        case ANIM_FINISH:
            animFinish();
            break;
        case ANIM_HEARTBEAT:
            animHeartbeat();
            break;
        case ANIM_RESULTS:
            animResults();
            break;
        default:
            break;
    }
}

/**
 * Get animation name as string
 */
String getAnimationName(AnimationState anim) {
    switch (anim) {
        case ANIM_IDLE: return "Idle";
        case ANIM_WELCOME: return "Welcome";
        case ANIM_BETTING_60: return "Betting 60";
        case ANIM_BETTING_30: return "Betting 30";
        case ANIM_FINAL_CALL: return "Final Call";
        case ANIM_RACE_START: return "Race Start";
        case ANIM_CHAOS: return "Chaos";
        case ANIM_FINISH: return "Finish";
        case ANIM_HEARTBEAT: return "Heartbeat";
        case ANIM_RESULTS: return "Results";
        default: return "None";
    }
}

/**
 * IDLE Animation - Slow breathing in DDM green
 */
void animIdle() {
    breathingEffect(CRGB(DDM_GREEN.r, DDM_GREEN.g, DDM_GREEN.b), 0.02);
}

/**
 * WELCOME Animation - Cascading DDM colors across all cups
 */
void animWelcome() {
    static unsigned long lastUpdate = 0;
    static int currentCup = 0;
    
    if (millis() - lastUpdate > 150) {
        lastUpdate = millis();
        
        // DDM color sequence
        CRGB colors[] = {
            CRGB(DDM_GREEN.r, DDM_GREEN.g, DDM_GREEN.b),
            CRGB(DDM_GOLD.r, DDM_GOLD.g, DDM_GOLD.b),
            CRGB(DDM_ROSE.r, DDM_ROSE.g, DDM_ROSE.b)
        };
        
        int colorIndex = currentCup % 3;
        setCup(currentCup + 1, colors[colorIndex]);
        
        currentCup++;
        if (currentCup >= NUM_CUPS) {
            currentCup = 0;
        }
    }
}

/**
 * BETTING_60 Animation - Gentle gold pulse (60 min warning)
 */
void animBetting60() {
    pulseEffect(CRGB(DDM_GOLD.r, DDM_GOLD.g, DDM_GOLD.b), 2000);
}

/**
 * BETTING_30 Animation - Faster amber pulse (30 min warning)
 */
void animBetting30() {
    // Amber color (between gold and rose)
    CRGB amber = CRGB(255, 150, 0);
    pulseEffect(amber, 1000);
}

/**
 * FINAL_CALL Animation - Red strobe with increasing urgency
 */
void animFinalCall() {
    unsigned long elapsed = millis() - animationStartTime;
    int interval = max(50, 500 - (int)(elapsed / 100)); // Speed up over time
    strobeEffect(CRGB(DDM_ROSE.r, DDM_ROSE.g, DDM_ROSE.b), interval);
}

/**
 * RACE_START Animation - Green flash
 */
void animRaceStart() {
    static bool flashed = false;
    
    if (!flashed) {
        setAllLEDs(DDM_GREEN.r, DDM_GREEN.g, DDM_GREEN.b);
        delay(500);
        clearAllLEDs();
        flashed = true;
    }
}

/**
 * CHAOS Animation - Maximum intensity strobing
 */
void animChaos() {
    static unsigned long lastUpdate = 0;
    
    if (millis() - lastUpdate > 50) {
        lastUpdate = millis();
        
        // Random intense colors on each cup
        for (int cup = 1; cup <= NUM_CUPS; cup++) {
            int colorChoice = random(0, 3);
            if (colorChoice == 0) {
                setCup(cup, DDM_GOLD.r, DDM_GOLD.g, DDM_GOLD.b);
            } else if (colorChoice == 1) {
                setCup(cup, DDM_ROSE.r, DDM_ROSE.g, DDM_ROSE.b);
            } else {
                setCup(cup, DDM_WHITE.r, DDM_WHITE.g, DDM_WHITE.b);
            }
        }
    }
}

/**
 * FINISH Animation - Checkered flag pattern
 */
void animFinish() {
    static bool initialized = false;
    
    if (!initialized) {
        checkeredPattern();
        initialized = true;
    }
}

/**
 * HEARTBEAT Animation - Synchronized pulse that slows over time
 */
void animHeartbeat() {
    unsigned long elapsed = millis() - animationStartTime;
    int beatInterval = 1000 + (elapsed / 1000) * 100; // Slow down over time
    beatInterval = min(beatInterval, 3000); // Max 3 second intervals
    
    pulseEffect(CRGB(DDM_ROSE.r, DDM_ROSE.g, DDM_ROSE.b), beatInterval);
}

/**
 * RESULTS Animation - Show winners with synchronized heartbeat
 */
void animResults() {
    if (!results.isActive) {
        return;
    }
    
    static unsigned long lastBeat = 0;
    unsigned long now = millis();
    
    // 2-second heartbeat cycle
    float phase = (now % 2000) / 2000.0;
    uint8_t brightness = (sin(phase * 2 * PI) + 1) * 127.5;
    
    // Clear all first
    clearAllLEDs();
    
    // Set winner cups with breathing effect
    for (int cup = 1; cup <= NUM_CUPS; cup++) {
        CRGB color;
        int dimBrightness = 25; // 10% brightness for non-winners
        
        if (cup == results.winCup) {
            color = CRGB(DDM_GOLD.r, DDM_GOLD.g, DDM_GOLD.b);
            color.nscale8(brightness);
        } else if (cup == results.placeCup) {
            color = CRGB(DDM_SILVER.r, DDM_SILVER.g, DDM_SILVER.b);
            color.nscale8(brightness);
        } else if (cup == results.showCup) {
            color = CRGB(DDM_BRONZE.r, DDM_BRONZE.g, DDM_BRONZE.b);
            color.nscale8(brightness);
        } else {
            // Dim dark red for other cups
            color = CRGB(DDM_ROSE.r, DDM_ROSE.g, DDM_ROSE.b);
            color.nscale8(dimBrightness);
        }
        
        setCup(cup, color);
    }
}

/**
 * Breathing effect utility
 */
void breathingEffect(CRGB color, float speed) {
    float breath = (sin(millis() * speed) + 1.0) / 2.0;
    CRGB scaledColor = color;
    scaledColor.nscale8(breath * 255);
    setAllLEDs(scaledColor);
}

/**
 * Pulse effect utility
 */
void pulseEffect(CRGB color, int pulseSpeed) {
    unsigned long phase = millis() % pulseSpeed;
    float intensity = (sin((phase / (float)pulseSpeed) * 2 * PI) + 1) / 2.0;
    CRGB scaledColor = color;
    scaledColor.nscale8(intensity * 255);
    setAllLEDs(scaledColor);
}

/**
 * Strobe effect utility
 */
void strobeEffect(CRGB color, int interval) {
    static unsigned long lastToggle = 0;
    static bool isOn = false;
    
    if (millis() - lastToggle > interval) {
        lastToggle = millis();
        isOn = !isOn;
        
        if (isOn) {
            setAllLEDs(color);
        } else {
            clearAllLEDs();
        }
    }
}

/**
 * Cascade effect utility
 */
void cascadeEffect(const CRGB colors[], int numColors, int delayMs) {
    static unsigned long lastUpdate = 0;
    static int currentCup = 0;
    
    if (millis() - lastUpdate > delayMs) {
        lastUpdate = millis();
        
        int colorIndex = currentCup % numColors;
        setCup(currentCup + 1, colors[colorIndex]);
        
        currentCup++;
        if (currentCup >= NUM_CUPS) {
            currentCup = 0;
        }
    }
}

/**
 * Checkered pattern utility
 */
void checkeredPattern() {
    for (int cup = 1; cup <= NUM_CUPS; cup++) {
        if (cup % 2 == 1) {
            setCup(cup, DDM_WHITE.r, DDM_WHITE.g, DDM_WHITE.b);
        } else {
            setCup(cup, DDM_BLACK.r, DDM_BLACK.g, DDM_BLACK.b);
        }
    }
}
