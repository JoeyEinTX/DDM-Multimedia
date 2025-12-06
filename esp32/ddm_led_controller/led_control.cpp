// led_control.cpp - LED control implementation for DDM Cup Project

#include "led_control.h"

// LED strip array
CRGB leds[LED_COUNT];

// Current brightness level
static uint8_t currentBrightness = DEFAULT_BRIGHTNESS;

/**
 * Initialize the LED strip
 */
void initLEDs() {
    FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, LED_COUNT);
    FastLED.setBrightness(DEFAULT_BRIGHTNESS);
    FastLED.clear();
    FastLED.show();
    Serial.println("[LED] Initialized " + String(LED_COUNT) + " LEDs on GPIO " + String(LED_PIN));
}

/**
 * Set all LEDs to a specific color (CRGB)
 */
void setAllLEDs(CRGB color) {
    fill_solid(leds, LED_COUNT, color);
    FastLED.show();
}

/**
 * Set all LEDs to a specific color (RGB values)
 */
void setAllLEDs(uint8_t r, uint8_t g, uint8_t b) {
    setAllLEDs(CRGB(r, g, b));
}

/**
 * Set a specific cup to a color (cups numbered 1-20)
 */
void setCup(uint8_t cupNumber, CRGB color) {
    if (cupNumber < 1 || cupNumber > NUM_CUPS) {
        Serial.println("[LED] Error: Cup number " + String(cupNumber) + " out of range (1-" + String(NUM_CUPS) + ")");
        return;
    }
    
    uint16_t startIdx = (cupNumber - 1) * LEDS_PER_CUP;
    uint16_t endIdx = startIdx + LEDS_PER_CUP;
    
    for (uint16_t i = startIdx; i < endIdx; i++) {
        leds[i] = color;
    }
    
    FastLED.show();
}

/**
 * Set a specific cup to a color (RGB values)
 */
void setCup(uint8_t cupNumber, uint8_t r, uint8_t g, uint8_t b) {
    setCup(cupNumber, CRGB(r, g, b));
}

/**
 * Clear all LEDs (turn off)
 */
void clearAllLEDs() {
    FastLED.clear();
    FastLED.show();
}

/**
 * Set global brightness (0-255)
 */
void setBrightness(uint8_t brightness) {
    currentBrightness = constrain(brightness, 0, MAX_BRIGHTNESS);
    FastLED.setBrightness(currentBrightness);
    FastLED.show();
    Serial.println("[LED] Brightness set to " + String(currentBrightness));
}

/**
 * Get current brightness level
 */
uint8_t getBrightness() {
    return currentBrightness;
}

/**
 * Update LED strip (call after making changes)
 */
void updateLEDs() {
    FastLED.show();
}

/**
 * Convert hex color string to CRGB
 * Accepts formats: "RRGGBB" or "#RRGGBB"
 */
CRGB hexToRGB(const String& hexColor) {
    String hex = hexColor;
    
    // Remove # if present
    if (hex.startsWith("#")) {
        hex = hex.substring(1);
    }
    
    // Validate length
    if (hex.length() != 6) {
        Serial.println("[LED] Error: Invalid hex color '" + hexColor + "'");
        return CRGB::Black;
    }
    
    // Parse RGB components
    long number = strtol(hex.c_str(), NULL, 16);
    uint8_t r = (number >> 16) & 0xFF;
    uint8_t g = (number >> 8) & 0xFF;
    uint8_t b = number & 0xFF;
    
    return CRGB(r, g, b);
}

/**
 * Get LED index range for a specific cup
 */
void getCupRange(uint8_t cupNumber, uint16_t& startIdx, uint16_t& endIdx) {
    if (cupNumber < 1 || cupNumber > NUM_CUPS) {
        startIdx = 0;
        endIdx = 0;
        return;
    }
    
    startIdx = (cupNumber - 1) * LEDS_PER_CUP;
    endIdx = startIdx + LEDS_PER_CUP;
}

/**
 * Test sequence - light each cup sequentially in DDM colors
 */
void testSequence() {
    Serial.println("[LED] Starting test sequence...");
    
    clearAllLEDs();
    delay(500);
    
    // Test colors
    const CRGB testColors[] = {
        CRGB(DDM_GREEN.r, DDM_GREEN.g, DDM_GREEN.b),
        CRGB(DDM_GOLD.r, DDM_GOLD.g, DDM_GOLD.b),
        CRGB(DDM_ROSE.r, DDM_ROSE.g, DDM_ROSE.b),
        CRGB(DDM_WHITE.r, DDM_WHITE.g, DDM_WHITE.b)
    };
    const int numTestColors = 4;
    
    for (uint8_t cup = 1; cup <= NUM_CUPS; cup++) {
        CRGB color = testColors[(cup - 1) % numTestColors];
        setCup(cup, color);
        delay(200);
    }
    
    delay(1000);
    clearAllLEDs();
    Serial.println("[LED] Test sequence complete");
}

/**
 * Rainbow test - cycle through rainbow colors
 */
void rainbowTest() {
    Serial.println("[LED] Starting rainbow test...");
    
    for (int hue = 0; hue < 256; hue++) {
        fill_solid(leds, LED_COUNT, CHSV(hue, 255, 255));
        FastLED.show();
        delay(10);
    }
    
    clearAllLEDs();
    Serial.println("[LED] Rainbow test complete");
}
