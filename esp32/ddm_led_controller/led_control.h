// led_control.h - LED control functions for DDM Cup Project

#ifndef LED_CONTROL_H
#define LED_CONTROL_H

#include <FastLED.h>
#include "config.h"

// LED strip array
extern CRGB leds[LED_COUNT];

// LED Control Functions
void initLEDs();
void setAllLEDs(CRGB color);
void setAllLEDs(uint8_t r, uint8_t g, uint8_t b);
void setCup(uint8_t cupNumber, CRGB color);
void setCup(uint8_t cupNumber, uint8_t r, uint8_t g, uint8_t b);
void clearAllLEDs();
void setBrightness(uint8_t brightness);
uint8_t getBrightness();
void updateLEDs();

// Utility Functions
CRGB hexToRGB(const String& hexColor);
void getCupRange(uint8_t cupNumber, uint16_t& startIdx, uint16_t& endIdx);

// Test Functions
void testSequence();
void rainbowTest();

#endif // LED_CONTROL_H
