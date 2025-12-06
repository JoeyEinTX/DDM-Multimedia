// oled_display.h - OLED display functions for DDM Cup Project

#ifndef OLED_DISPLAY_H
#define OLED_DISPLAY_H

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "config.h"

// OLED display object
extern Adafruit_SSD1306 display;

// Display functions
void initOLED();
bool isOLEDEnabled();
void clearDisplay();
void updateDisplay();

// Status display functions
void showConnecting(const String& ssid);
void showConnected(const String& ip);
void showError(const String& error);
void showStatus(const String& ip, const String& status, const String& animation, const String& lastCmd);

// Utility functions
void displayText(int x, int y, const String& text, int size = 1);
void displayCentered(int y, const String& text, int size = 1);

#endif // OLED_DISPLAY_H
