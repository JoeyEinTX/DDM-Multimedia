// oled_display.cpp - OLED display implementation for DDM Cup Project

#include "oled_display.h"

// OLED display object
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);

static bool oledEnabled = OLED_ENABLED;

/**
 * Initialize the OLED display
 */
void initOLED() {
    if (!OLED_ENABLED) {
        Serial.println("[OLED] Disabled in config");
        oledEnabled = false;
        return;
    }
    
    // Initialize I2C
    Wire.begin(OLED_SDA_PIN, OLED_SCL_PIN);
    
    // Initialize display
    if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS)) {
        Serial.println("[OLED] ERROR: Failed to initialize");
        oledEnabled = false;
        return;
    }
    
    // Initial setup
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.display();
    
    oledEnabled = true;
    Serial.println("[OLED] Initialized successfully");
}

/**
 * Check if OLED is enabled
 */
bool isOLEDEnabled() {
    return oledEnabled;
}

/**
 * Clear the display
 */
void clearDisplay() {
    if (!oledEnabled) return;
    display.clearDisplay();
}

/**
 * Update the display (write buffer to screen)
 */
void updateDisplay() {
    if (!oledEnabled) return;
    display.display();
}

/**
 * Show "Connecting to WiFi" message
 */
void showConnecting(const String& ssid) {
    if (!oledEnabled) return;
    
    clearDisplay();
    displayCentered(0, "DDM Cup", 2);
    displayCentered(20, "LED Controller", 1);
    displayCentered(35, "Connecting to:", 1);
    displayCentered(45, ssid, 1);
    updateDisplay();
}

/**
 * Show "Connected" message with IP
 */
void showConnected(const String& ip) {
    if (!oledEnabled) return;
    
    clearDisplay();
    displayCentered(0, "CONNECTED!", 2);
    displayCentered(25, "IP Address:", 1);
    displayCentered(35, ip, 1);
    displayCentered(50, "Ready", 1);
    updateDisplay();
    
    delay(2000); // Show for 2 seconds
}

/**
 * Show error message
 */
void showError(const String& error) {
    if (!oledEnabled) return;
    
    clearDisplay();
    displayCentered(0, "ERROR!", 2);
    displayCentered(30, error, 1);
    updateDisplay();
}

/**
 * Show status information
 * Line 1: IP Address
 * Line 2: Connection status
 * Line 3: Current animation
 * Line 4: Last command
 */
void showStatus(const String& ip, const String& status, const String& animation, const String& lastCmd) {
    if (!oledEnabled) return;
    
    clearDisplay();
    
    // Line 1: IP Address
    displayText(0, 0, "IP: " + ip, 1);
    
    // Line 2: Status
    displayText(0, 12, "Status: " + status, 1);
    
    // Line 3: Animation
    displayText(0, 24, "Anim: " + animation, 1);
    
    // Line 4: Last Command
    String cmdDisplay = lastCmd;
    if (cmdDisplay.length() > 21) {
        cmdDisplay = cmdDisplay.substring(0, 18) + "...";
    }
    displayText(0, 36, "Cmd: " + cmdDisplay, 1);
    
    // Draw separator line
    display.drawLine(0, 48, OLED_WIDTH, 48, SSD1306_WHITE);
    
    // Bottom status
    displayCentered(52, "DDM Cup v3.0", 1);
    
    updateDisplay();
}

/**
 * Display text at specific position
 */
void displayText(int x, int y, const String& text, int size) {
    if (!oledEnabled) return;
    
    display.setTextSize(size);
    display.setCursor(x, y);
    display.print(text);
}

/**
 * Display centered text at Y position
 */
void displayCentered(int y, const String& text, int size) {
    if (!oledEnabled) return;
    
    display.setTextSize(size);
    int16_t x1, y1;
    uint16_t w, h;
    display.getTextBounds(text, 0, 0, &x1, &y1, &w, &h);
    int x = (OLED_WIDTH - w) / 2;
    display.setCursor(x, y);
    display.print(text);
}
