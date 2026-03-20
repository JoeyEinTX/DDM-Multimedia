// ddm_led_controller.ino - DDM Cup LED Controller with ANIMATIONS
// ESP32 WROOM-32 with FastLED - WiFi, Socket Server, Full Animation Suite
// Version 3.1 - Complete Animation System

#include <WiFi.h>
#include <FastLED.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ===== CONFIGURATION =====
#define WIFI_SSID "BMP_WIFI_MAIN"
#define WIFI_PASSWORD "Derby1961"

#define SOCKET_PORT 5005
#define LED_PIN 18
#define LED_COUNT 640
#define STATUS_LED_PIN 2
#define NUM_CUPS 20
#define LEDS_PER_CUP 32

// ===== OLED DISPLAY =====
#define OLED_WIDTH 128
#define OLED_HEIGHT 64
#define OLED_RESET -1           // No reset pin (shared with ESP32 reset)
#define OLED_ADDR 0x3C          // I2C address for SSD1306
#define OLED_UPDATE_MS 500      // Display refresh interval (ms)
#define OLED_SDA 21
#define OLED_SCL 22

Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, OLED_RESET);

bool oledReady = false;                // true after successful begin()
unsigned long lastDisplayUpdate = 0;   // millis() timer for periodic refresh
bool displayDirty = true;              // flag to force immediate redraw
String currentMode = "IDLE";           // mode string shown on OLED

// ===== DDM COLORS =====
const CRGB COLOR_GOLD = CRGB(255, 215, 0);
const CRGB COLOR_GREEN = CRGB(34, 139, 34);
const CRGB COLOR_AMBER = CRGB(255, 165, 0);
const CRGB COLOR_RED = CRGB(255, 0, 0);
const CRGB COLOR_SILVER = CRGB(192, 192, 192);
const CRGB COLOR_BRONZE = CRGB(205, 127, 50);
const CRGB COLOR_WHITE = CRGB(255, 255, 255);
const CRGB COLOR_BLACK = CRGB(0, 0, 0);

// ===== GLOBAL VARIABLES =====
CRGB leds[LED_COUNT];
WiFiServer server(SOCKET_PORT);
String currentIP = "";
uint8_t currentBrightness = 128;

// Power estimation
uint32_t currentDrawMA = 0;
uint32_t peakDrawMA = 0;
uint32_t minDrawMA = 0;

// Animation state
bool animationRunning = false;
String currentAnimation = "";
unsigned long animStartTime = 0;
int animStep = 0;

// Results storage for RESULTS animation
int winCup = 0;
int placeCup = 0;
int showCup = 0;

// RACE_START animation state
int raceStartPhase = 0;                // 0 = green blast, 1 = galloping
unsigned long raceStartPhase2Start = 0; // when phase 2 (galloping) began
int raceStartCurrentCup = 1;           // current cup being pulsed (1-based)
unsigned long raceStartLastCupTime = 0; // when current cup's white flash started
bool raceStartFlashing = false;         // is a cup currently showing white?

// Cup locking for custom colors during animations
bool cupLocked[21] = {false};  // cups 1-20, index 0 unused
CRGB cupLockedColor[21];       // color for locked cups

// WELCOME animation state
int welcomePhase = 0;                  // 0-4 for phases 1-5
unsigned long welcomePhaseStart = 0;   // millis() when current phase began
int welcomeCometPos = 0;               // comet head position for chase phase
bool welcomeCometForward = true;       // comet direction for chase phase
int welcomeCometSweeps = 0;            // number of completed sweeps
CRGB welcomeMarchColors[NUM_CUPS];     // color assignment for march phase

// HEARTBEAT_COOLDOWN animation state
unsigned long cooldownStartTime = 0;
const unsigned long COOLDOWN_DURATION_MS = 90000; // 90 seconds deceleration

// DDM brand color palette (6 colors)
const CRGB DDM_PALETTE[] = {
    CRGB(34, 139, 34),    // Forest Green
    CRGB(255, 215, 0),    // Gold
    CRGB(220, 20, 60),    // Rose/Crimson
    CRGB(78, 205, 196),   // Mint
    CRGB(255, 165, 0),    // Orange
    CRGB(65, 105, 225)    // Blue
};
const int DDM_PALETTE_SIZE = 6;

// Saddle cloth colors: primary and accent for posts 1-20 (index 0 unused)
const CRGB SILK_PRIMARY[21] = {
    CRGB(0,0,0),           // 0: unused
    CRGB(231,24,55),       // 1:  red
    CRGB(255,255,255),     // 2:  white
    CRGB(0,51,160),        // 3:  blue
    CRGB(255,205,0),       // 4:  yellow
    CRGB(0,132,61),        // 5:  green
    CRGB(0,0,0),           // 6:  black
    CRGB(255,102,0),       // 7:  orange
    CRGB(255,105,180),     // 8:  pink
    CRGB(64,224,208),      // 9:  turquoise
    CRGB(102,51,153),      // 10: purple
    CRGB(128,128,128),     // 11: grey
    CRGB(50,205,50),       // 12: lime
    CRGB(139,69,19),       // 13: brown
    CRGB(128,0,0),         // 14: maroon
    CRGB(196,183,166),     // 15: khaki
    CRGB(135,206,235),     // 16: light blue
    CRGB(0,0,128),         // 17: navy
    CRGB(34,139,34),       // 18: forest green
    CRGB(0,0,139),         // 19: dark blue
    CRGB(255,0,255)        // 20: fuchsia
};
const CRGB SILK_ACCENT[21] = {
    CRGB(0,0,0),           // 0: unused
    CRGB(255,255,255),     // 1:  white
    CRGB(0,0,0),           // 2:  black
    CRGB(255,255,255),     // 3:  white
    CRGB(0,0,0),           // 4:  black
    CRGB(255,255,255),     // 5:  white
    CRGB(255,215,0),       // 6:  gold
    CRGB(0,0,0),           // 7:  black
    CRGB(0,0,0),           // 8:  black
    CRGB(0,0,0),           // 9:  black
    CRGB(255,255,255),     // 10: white
    CRGB(231,24,55),       // 11: red
    CRGB(0,0,0),           // 12: black
    CRGB(255,255,255),     // 13: white
    CRGB(255,205,0),       // 14: yellow
    CRGB(0,0,0),           // 15: black
    CRGB(231,24,55),       // 16: red
    CRGB(255,255,255),     // 17: white
    CRGB(255,205,0),       // 18: yellow
    CRGB(231,24,55),       // 19: red
    CRGB(255,205,0)        // 20: yellow
};

// ===== FUNCTION DECLARATIONS =====
void connectWiFi();
String processCommand(String cmd);
CRGB hexToRGB(String hex);
void setCup(uint8_t cupNumber, CRGB color);
void blinkStatus(int times);
void printBanner();
void updatePowerEstimate();

// OLED display functions
void initOLED();
void showBootSplash();
void updateDisplay();
void updateDisplayNow();

// Animation functions
void runAnimation();
void stopAnimation();
void animWelcome();
void animTest();
void animBetting60();
void animBetting30();
void animFinalCall();
void animRaceStart();
void animChaos();
void animFinish();
void animResults();
void animResultsActive();
void animHeartbeatCooldown();
void animSilks();

/**
 * SETUP - Runs once at startup
 */
void setup() {
    // Initialize serial
    Serial.begin(115200);
    delay(1000);
    
    printBanner();
    
    // Initialize status LED
    pinMode(STATUS_LED_PIN, OUTPUT);
    digitalWrite(STATUS_LED_PIN, LOW);
    
    // Initialize OLED display (before WiFi so we can show status)
    initOLED();
    showBootSplash();   // "DDM v3.1" for 2 seconds (blocking, startup only)
    
    // Initialize LED strip
    Serial.println("[LED] Initializing 640 LEDs on GPIO 18...");
    FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, LED_COUNT);
    FastLED.setBrightness(currentBrightness);
    FastLED.clear();
    FastLED.show();
    updatePowerEstimate();
    Serial.println("[LED] LED strip initialized");
    
    // Show "CONNECTING..." on OLED during WiFi setup
    if (oledReady) {
        display.clearDisplay();
        display.setTextSize(1);
        display.setTextColor(SSD1306_WHITE);
        display.setCursor(16, 28);
        display.println(F("CONNECTING..."));
        display.display();
    }
    
    // Connect to WiFi
    connectWiFi();
    currentMode = "IDLE";
    
    // Start socket server
    server.begin();
    Serial.println("[Socket] Server started on port " + String(SOCKET_PORT));
    Serial.println("[Socket] Ready to receive commands");
    Serial.println("==================================================\n");    
    // Startup blink
    blinkStatus(3);
    
    // Force initial display update after boot
    displayDirty = true;
}

/**
 * MAIN LOOP - Runs continuously
 */
void loop() {
    // Run animation if active
    if (animationRunning) {
        runAnimation();
    }
    
    // Handle incoming socket connections
    WiFiClient client = server.available();
    
    if (client) {
        if (client.connected()) {
            if (client.available()) {
                // Read command
                String command = client.readStringUntil('\n');
                command.trim();
                
                Serial.println("[CMD] Received: " + command);
                
                // Process command
                String response = processCommand(command);
                
                // Send response
                client.println(response);
                Serial.println("[CMD] Response: " + response);
                
                // Immediate OLED update after command
                displayDirty = true;
            }
        }
        client.stop();
    }
    
    // Update OLED display (non-blocking, every 500ms or when dirty)
    updateDisplay();
    
    // Blink status LED (heartbeat)
    static unsigned long lastBlink = 0;
    if (millis() - lastBlink > 1000) {
        digitalWrite(STATUS_LED_PIN, !digitalRead(STATUS_LED_PIN));
        lastBlink = millis();
    }
    
    delay(10);
}

/**
 * Connect to WiFi
 */
void connectWiFi() {
    Serial.println("[WiFi] Connecting to: " + String(WIFI_SSID));
    
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    
    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 30) {
        delay(500);
        Serial.print(".");
        digitalWrite(STATUS_LED_PIN, attempts % 2);
        attempts++;
    }
    
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("\n[WiFi] ERROR: Connection failed!");
        while (1) {
            digitalWrite(STATUS_LED_PIN, HIGH);
            delay(100);
            digitalWrite(STATUS_LED_PIN, LOW);
            delay(100);
        }
    }
    
    currentIP = WiFi.localIP().toString();
    Serial.println("\n[WiFi] Connected!");
    Serial.println("[WiFi] IP Address: " + currentIP);
    Serial.println("[WiFi] Signal: " + String(WiFi.RSSI()) + " dBm\n");
}

/**
 * Process incoming command
 */
String processCommand(String cmd) {
    cmd.trim();
    cmd.toUpperCase();
    
    // PING - Test connection
    if (cmd == "PING") {
        blinkStatus(1);
        return "PONG";
    }
    
    // LED:ALL_ON - Turn all LEDs white
    else if (cmd == "LED:ALL_ON") {
        stopAnimation();
        currentMode = "ALL_ON";
        fill_solid(leds, LED_COUNT, CRGB::White);
        FastLED.show();
        updatePowerEstimate();
        return "OK:ALL_ON";
    }
    
    // LED:ALL_OFF - Turn all LEDs off
    else if (cmd == "LED:ALL_OFF") {
        stopAnimation();
        currentMode = "ALL_OFF";
        FastLED.clear();
        FastLED.show();
        updatePowerEstimate();
        return "OK:ALL_OFF";
    }
    
    // LED:BRIGHTNESS:XX - Set brightness (0-100)
    else if (cmd.startsWith("LED:BRIGHTNESS:")) {
        int brightness = cmd.substring(15).toInt();
        brightness = constrain(brightness, 0, 100);
        currentBrightness = map(brightness, 0, 100, 0, 255);
        FastLED.setBrightness(currentBrightness);
        FastLED.show();
        updatePowerEstimate();
        return "OK:BRIGHTNESS:" + String(brightness);
    }
    
    // LED:COLOR:RRGGBB - Set all LEDs to hex color
    else if (cmd.startsWith("LED:COLOR:")) {
        stopAnimation();
        currentMode = "COLOR";
        String hexColor = cmd.substring(10);
        CRGB color = hexToRGB(hexColor);
        fill_solid(leds, LED_COUNT, color);
        FastLED.show();
        updatePowerEstimate();
        return "OK:COLOR:" + hexColor;
    }
    
    // LED:CUP:N:RRGGBB - Set specific cup to hex color
    // LED:CUP:N:R,G,B - Set specific cup to RGB color
    else if (cmd.startsWith("LED:CUP:")) {
        stopAnimation();
        currentMode = "CUP_SET";
        // Parse: LED:CUP:5:FFD700 or LED:CUP:5:255,215,0
        int firstColon = cmd.indexOf(':', 8);
        if (firstColon > 0) {
            int cupNum = cmd.substring(8, firstColon).toInt();
            String colorStr = cmd.substring(firstColon + 1);
            
            if (cupNum >= 1 && cupNum <= 20) {
                CRGB color;
                
                // Check if RGB format (contains comma)
                if (colorStr.indexOf(',') > 0) {
                    // Parse RGB: "255,215,0"
                    int firstComma = colorStr.indexOf(',');
                    int secondComma = colorStr.indexOf(',', firstComma + 1);
                    
                    if (secondComma > 0) {
                        int r = colorStr.substring(0, firstComma).toInt();
                        int g = colorStr.substring(firstComma + 1, secondComma).toInt();
                        int b = colorStr.substring(secondComma + 1).toInt();
                        color = CRGB(r, g, b);
                    }
                } else {
                    // Parse hex: "FFD700"
                    color = hexToRGB(colorStr);
                }
                
                setCup(cupNum, color);
                FastLED.show();
                updatePowerEstimate();
                return "OK:CUP:" + String(cupNum) + ":" + colorStr;
            }
        }
        return "ERROR:INVALID_CUP";
    }
    
    // LED:TEST:R,G,B,BRIGHTNESS - RGB test mode with brightness
    else if (cmd.startsWith("LED:TEST:")) {
        stopAnimation();
        currentMode = "RGB_TEST";
        // Parse: LED:TEST:180,45,220,75
        String params = cmd.substring(9);
        
        int firstComma = params.indexOf(',');
        int secondComma = params.indexOf(',', firstComma + 1);
        int thirdComma = params.indexOf(',', secondComma + 1);
        
        if (firstComma > 0 && secondComma > 0 && thirdComma > 0) {
            int r = params.substring(0, firstComma).toInt();
            int g = params.substring(firstComma + 1, secondComma).toInt();
            int b = params.substring(secondComma + 1, thirdComma).toInt();
            int brightness = params.substring(thirdComma + 1).toInt();
            
            // Set brightness
            brightness = constrain(brightness, 0, 100);
            currentBrightness = map(brightness, 0, 100, 0, 255);
            FastLED.setBrightness(currentBrightness);
            
            // Set all LEDs to color
            CRGB color = CRGB(r, g, b);
            fill_solid(leds, LED_COUNT, color);
            FastLED.show();
            updatePowerEstimate();
            
            return "OK:TEST:" + String(r) + "," + String(g) + "," + String(b) + "," + String(brightness);
        }
        return "ERROR:INVALID_TEST_PARAMS";
    }
    
    // ANIM:WELCOME - 5-phase looping welcome playlist
    else if (cmd == "ANIM:WELCOME") {
        currentMode = "WELCOME";
        currentAnimation = "WELCOME";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        // Initialize welcome state
        welcomePhase = 0;
        welcomePhaseStart = millis();
        welcomeCometPos = 0;
        welcomeCometForward = true;
        welcomeCometSweeps = 0;
        FastLED.clear();
        FastLED.show();
        updatePowerEstimate();
        return "OK:ANIM:WELCOME";
    }
    
    // ANIM:TEST - Sequential cup test
    else if (cmd == "ANIM:TEST") {
        currentMode = "TEST";
        currentAnimation = "TEST";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:TEST";
    }
    
    // ANIM:BETTING_60 - Slow green breathing
    else if (cmd == "ANIM:BETTING_60") {
        currentMode = "BETTING_60";
        currentAnimation = "BETTING_60";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:BETTING_60";
    }
    
    // ANIM:BETTING_30 - Center-out amber pulse
    else if (cmd == "ANIM:BETTING_30") {
        currentMode = "BETTING_30";
        currentAnimation = "BETTING_30";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:BETTING_30";
    }
    
    // ANIM:FINAL_CALL - Edges-in red urgency
    else if (cmd == "ANIM:FINAL_CALL") {
        currentMode = "FINAL_CALL";
        currentAnimation = "FINAL_CALL";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:FINAL_CALL";
    }
    
    // ANIM:RACE_START - "They're Off!" green blast then galloping white sweep
    else if (cmd == "ANIM:RACE_START") {
        currentMode = "RACE_START";
        currentAnimation = "RACE_START";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        // Initialize race start state
        raceStartPhase = 0;             // Phase 1: Green Blast
        raceStartCurrentCup = 1;        // First cup to pulse in Phase 2
        raceStartLastCupTime = 0;
        raceStartFlashing = false;
        raceStartPhase2Start = 0;
        // Immediately set all LEDs to 100% bright green for Phase 1
        fill_solid(leds, LED_COUNT, CRGB(0, 255, 0));
        FastLED.show();
        updatePowerEstimate();
        return "OK:ANIM:RACE_START";
    }
    
    // ANIM:CHAOS - Random madness
    else if (cmd == "ANIM:CHAOS") {
        currentMode = "CHAOS";
        currentAnimation = "CHAOS";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:CHAOS";
    }
    
    // ANIM:FINISH - Checkered flag pattern
    else if (cmd == "ANIM:FINISH") {
        currentMode = "FINISH";
        currentAnimation = "FINISH";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:FINISH";
    }
    
    // ANIM:SILKS - Display saddle cloth colors on each cup
    else if (cmd == "ANIM:SILKS") {
        currentMode = "SILKS";
        currentAnimation = "SILKS";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:SILKS";
    }

    // ANIM:HEARTBEAT_COOLDOWN - Decelerating heartbeat (160 BPM → 60 BPM)
    else if (cmd == "ANIM:HEARTBEAT_COOLDOWN") {
        currentMode = "COOLDOWN";
        currentAnimation = "HEARTBEAT_COOLDOWN";
        animationRunning = true;
        animStartTime = millis();
        cooldownStartTime = millis();
        animStep = 0;
        return "OK:ANIM:HEARTBEAT_COOLDOWN";
    }

    // ANIM:RESULTS:W:P:S - Winner spotlight (e.g., ANIM:RESULTS:7:12:3)
    else if (cmd.startsWith("ANIM:RESULTS:")) {
        // Parse: ANIM:RESULTS:7:12:3
        int firstColon = cmd.indexOf(':', 13);
        int secondColon = cmd.indexOf(':', firstColon + 1);
        
        if (firstColon > 0 && secondColon > 0) {
            winCup = cmd.substring(13, firstColon).toInt();
            placeCup = cmd.substring(firstColon + 1, secondColon).toInt();
            showCup = cmd.substring(secondColon + 1).toInt();
            
            if (winCup >= 1 && winCup <= 20 && 
                placeCup >= 1 && placeCup <= 20 && 
                showCup >= 1 && showCup <= 20) {
                currentMode = "RESULTS";
                currentAnimation = "RESULTS";
                animationRunning = true;
                animStartTime = millis();
                animStep = 0;
                return "OK:ANIM:RESULTS:" + String(winCup) + ":" + String(placeCup) + ":" + String(showCup);
            }
        }
        return "ERROR:INVALID_RESULTS";
    }
    
    // ANIM:RESULTS_ACTIVE:W:P:S - Winner colors + slow heartbeat on others
    else if (cmd.startsWith("ANIM:RESULTS_ACTIVE:")) {
        // Parse: ANIM:RESULTS_ACTIVE:7:12:3
        int firstColon = cmd.indexOf(':', 20);
        int secondColon = cmd.indexOf(':', firstColon + 1);
        
        if (firstColon > 0 && secondColon > 0) {
            winCup = cmd.substring(20, firstColon).toInt();
            placeCup = cmd.substring(firstColon + 1, secondColon).toInt();
            showCup = cmd.substring(secondColon + 1).toInt();
            
            if (winCup >= 1 && winCup <= 20 && 
                placeCup >= 1 && placeCup <= 20 && 
                showCup >= 1 && showCup <= 20) {
                currentMode = "RESULTS_ACTIVE";
                currentAnimation = "RESULTS_ACTIVE";
                animationRunning = true;
                animStartTime = millis();
                animStep = 0;
                return "OK:ANIM:RESULTS_ACTIVE:" + String(winCup) + ":" + String(placeCup) + ":" + String(showCup);
            }
        }
        return "ERROR:INVALID_RESULTS_ACTIVE";
    }
    
    // CUP:LOCK:num:r:g:b - Lock cup to specific color during animations
    else if (cmd.startsWith("CUP:LOCK:")) {
        // CUP:LOCK:cupNum:r:g:b
        int firstColon = cmd.indexOf(':', 9);
        int secondColon = cmd.indexOf(':', firstColon + 1);
        int thirdColon = cmd.indexOf(':', secondColon + 1);
        
        if (firstColon > 0 && secondColon > 0 && thirdColon > 0) {
            int cupNum = cmd.substring(9, firstColon).toInt();
            int r = cmd.substring(firstColon + 1, secondColon).toInt();
            int g = cmd.substring(secondColon + 1, thirdColon).toInt();
            int b = cmd.substring(thirdColon + 1).toInt();
            
            if (cupNum >= 1 && cupNum <= 20) {
                cupLocked[cupNum] = true;
                cupLockedColor[cupNum] = CRGB(r, g, b);
                setCup(cupNum, cupLockedColor[cupNum]);
                FastLED.show();
                updatePowerEstimate();
                return "OK:CUP:LOCKED:" + String(cupNum);
            }
        }
        return "ERROR:INVALID_CUP";
    }
    
    // CUP:UNLOCK:num - Return cup to heartbeat animation
    else if (cmd.startsWith("CUP:UNLOCK:")) {
        String param = cmd.substring(11);
        
        if (param == "ALL") {
            // Unlock all cups
            for (int i = 1; i <= 20; i++) {
                cupLocked[i] = false;
            }
            return "OK:CUP:UNLOCKED:ALL";
        } else {
            // Unlock specific cup
            int cupNum = param.toInt();
            if (cupNum >= 1 && cupNum <= 20) {
                cupLocked[cupNum] = false;
                return "OK:CUP:UNLOCKED:" + String(cupNum);
            }
        }
        return "ERROR:INVALID_CUP";
    }
    
    // RESET - Clear all and stop animations
    else if (cmd == "RESET") {
        currentMode = "IDLE";
        stopAnimation();
        FastLED.clear();
        FastLED.show();
        updatePowerEstimate();
        return "OK:RESET";
    }

    // STATUS - Return power draw estimates
    else if (cmd == "STATUS") {
        return "STATUS:" + String(currentDrawMA) + ":" + String(peakDrawMA) + ":" + String(minDrawMA);
    }

    return "ERROR:UNKNOWN_COMMAND";
}

/**
 * Stop any running animation
 */
void stopAnimation() {
    animationRunning = false;
    currentAnimation = "";
    animStep = 0;
}

/**
 * Run the current animation (called from loop)
 */
void runAnimation() {
    if (!animationRunning) return;
    
    if (currentAnimation == "WELCOME") {
        animWelcome();
    } else if (currentAnimation == "TEST") {
        animTest();
    } else if (currentAnimation == "BETTING_60") {
        animBetting60();
    } else if (currentAnimation == "BETTING_30") {
        animBetting30();
    } else if (currentAnimation == "FINAL_CALL") {
        animFinalCall();
    } else if (currentAnimation == "RACE_START") {
        animRaceStart();
    } else if (currentAnimation == "CHAOS") {
        animChaos();
    } else if (currentAnimation == "FINISH") {
        animFinish();
    } else if (currentAnimation == "HEARTBEAT_COOLDOWN") {
        animHeartbeatCooldown();
    } else if (currentAnimation == "SILKS") {
        animSilks();
    } else if (currentAnimation == "RESULTS") {
        animResults();
    } else if (currentAnimation == "RESULTS_ACTIVE") {
        animResultsActive();
    }
}

/**
 * ANIM:WELCOME - 5-phase looping welcome playlist
 * Cycles through 5 distinct visual phases (~8-10 sec each), looping forever.
 * Non-blocking millis() timing throughout. Interruptible by any command.
 *
 * Phase 0: DDM Color Cascade   (10 sec) - Sequential cup fill with DDM colors
 * Phase 1: Breathing Wave      (10 sec) - All cups breathe, color shifts each cycle
 * Phase 2: Chase Comet         ( 8 sec) - White comet with tail on dim green
 * Phase 3: Gold Sparkle        ( 8 sec) - Random gold/white twinkles on dim green
 * Phase 4: Color March         (10 sec) - Rotating barber-pole of DDM colors
 */
void animWelcome() {
    unsigned long now = millis();
    unsigned long phaseElapsed = now - welcomePhaseStart;

    switch (welcomePhase) {

    // ===== PHASE 0: DDM Color Cascade (10 seconds) =====
    // Cups light 1→20 every 400ms with cycling DDM palette, then hold 2s
    case 0: {
        static unsigned long lastCascade = 0;
        // Fill phase: 20 cups × 400ms = 8000ms, then 2000ms hold = 10000ms
        if (animStep < NUM_CUPS) {
            if (now - lastCascade >= 400) {
                lastCascade = now;
                setCup(animStep + 1, DDM_PALETTE[animStep % DDM_PALETTE_SIZE]);
                FastLED.show();
                updatePowerEstimate();
                animStep++;
            }
        } else {
            // All lit — hold until 10s total
            if (phaseElapsed >= 10000) {
                // Advance to Phase 1
                welcomePhase = 1;
                welcomePhaseStart = now;
                animStep = 0;
            }
        }
        break;
    }

    // ===== PHASE 1: Breathing Wave (10 seconds) =====
    // All cups breathe together (~2s cycle). Color shifts each full cycle.
    case 1: {
        // Each breath cycle = 2000ms (one full sin period)
        float t = phaseElapsed / 1000.0;               // seconds elapsed
        int breathCycle = (int)(t / 2.0);               // which breath we're on
        float breathPhase = fmod(t, 2.0) / 2.0;        // 0.0→1.0 within cycle
        float breathe = (sin(breathPhase * 6.2832 - 1.5708) + 1.0) / 2.0; // 0→1→0

        uint8_t brightness = 30 + (uint8_t)(breathe * 225); // 30-255
        CRGB color = DDM_PALETTE[breathCycle % DDM_PALETTE_SIZE];
        color.nscale8(brightness);

        fill_solid(leds, LED_COUNT, color);
        FastLED.show();
        updatePowerEstimate();

        if (phaseElapsed >= 10000) {
            welcomePhase = 2;
            welcomePhaseStart = now;
            animStep = 0;
            welcomeCometPos = 0;
            welcomeCometForward = true;
            welcomeCometSweeps = 0;
        }
        break;
    }

    // ===== PHASE 2: Chase Comet (8 seconds) =====
    // White comet with 3-cup fading tail sweeps back and forth on dim green
    case 2: {
        static unsigned long lastComet = 0;
        if (now - lastComet >= 100) {
            lastComet = now;

            // Set all cups to dim green background
            CRGB bgDim = CRGB(0, 40, 0);
            for (int c = 1; c <= NUM_CUPS; c++) {
                setCup(c, bgDim);
            }

            // Draw tail (3 cups behind head, decreasing brightness)
            uint8_t tailBright[] = {180, 100, 40};
            for (int t = 1; t <= 3; t++) {
                int tailPos;
                if (welcomeCometForward) {
                    tailPos = welcomeCometPos - t;
                } else {
                    tailPos = welcomeCometPos + t;
                }
                if (tailPos >= 0 && tailPos < NUM_CUPS) {
                    CRGB tailColor = CRGB(tailBright[t - 1], tailBright[t - 1], tailBright[t - 1]);
                    setCup(tailPos + 1, tailColor);
                }
            }

            // Draw head (full white)
            if (welcomeCometPos >= 0 && welcomeCometPos < NUM_CUPS) {
                setCup(welcomeCometPos + 1, CRGB(255, 255, 255));
            }

            FastLED.show();
            updatePowerEstimate();

            // Advance comet position
            if (welcomeCometForward) {
                welcomeCometPos++;
                if (welcomeCometPos >= NUM_CUPS) {
                    welcomeCometPos = NUM_CUPS - 1;
                    welcomeCometForward = false;
                    welcomeCometSweeps++;
                }
            } else {
                welcomeCometPos--;
                if (welcomeCometPos < 0) {
                    welcomeCometPos = 0;
                    welcomeCometForward = true;
                    welcomeCometSweeps++;
                }
            }
        }

        if (phaseElapsed >= 8000) {
            welcomePhase = 3;
            welcomePhaseStart = now;
            animStep = 0;
        }
        break;
    }

    // ===== PHASE 3: Gold Sparkle (8 seconds) =====
    // Dim green background, 3-4 random cups flash gold or white each frame
    case 3: {
        static unsigned long lastSparkle = 0;
        if (now - lastSparkle >= 100) {
            lastSparkle = now;

            // All cups to dim green background
            CRGB bgDim = CRGB(0, 50, 0);
            for (int c = 1; c <= NUM_CUPS; c++) {
                setCup(c, bgDim);
            }

            // Pick 3-4 random cups to sparkle
            int sparkleCount = 3 + random(0, 2); // 3 or 4
            for (int i = 0; i < sparkleCount; i++) {
                int cup = random(1, NUM_CUPS + 1);
                if (random(0, 2) == 0) {
                    setCup(cup, CRGB(255, 215, 0));   // Gold
                } else {
                    setCup(cup, CRGB(255, 255, 255));  // White
                }
            }

            FastLED.show();
            updatePowerEstimate();
        }

        if (phaseElapsed >= 8000) {
            welcomePhase = 4;
            welcomePhaseStart = now;
            animStep = 0;
            // Initialize march colors
            for (int c = 0; c < NUM_CUPS; c++) {
                welcomeMarchColors[c] = DDM_PALETTE[c % DDM_PALETTE_SIZE];
            }
        }
        break;
    }

    // ===== PHASE 4: Color March (10 seconds) =====
    // DDM palette assigned to cups in order, shifts right every 300ms (barber-pole)
    case 4: {
        static unsigned long lastMarch = 0;
        if (now - lastMarch >= 300) {
            lastMarch = now;

            // Rotate colors: save last cup's color, shift all right by 1
            CRGB saved = welcomeMarchColors[NUM_CUPS - 1];
            for (int c = NUM_CUPS - 1; c > 0; c--) {
                welcomeMarchColors[c] = welcomeMarchColors[c - 1];
            }
            welcomeMarchColors[0] = saved;

            // Apply colors to cups
            for (int c = 0; c < NUM_CUPS; c++) {
                setCup(c + 1, welcomeMarchColors[c]);
            }

            FastLED.show();
            updatePowerEstimate();
        }

        if (phaseElapsed >= 10000) {
            // Loop back to Phase 0 — seamless restart
            welcomePhase = 0;
            welcomePhaseStart = now;
            animStep = 0;
            FastLED.clear();
            FastLED.show();
            updatePowerEstimate();
        }
        break;
    }

    } // end switch
}

/**
 * ANIM:TEST - Sequential check, each cup lights 1→20
 */
void animTest() {
    static unsigned long lastUpdate = 0;
    if (millis() - lastUpdate < 200) return; // 200ms per cup
    lastUpdate = millis();
    
    if (animStep < NUM_CUPS) {
        FastLED.clear();
        setCup(animStep + 1, COLOR_WHITE);
        FastLED.show();
        updatePowerEstimate();
        animStep++;
    } else {
        FastLED.clear();
        FastLED.show();
        updatePowerEstimate();
        stopAnimation();
    }
}

/**
 * ANIM:BETTING_60 - Slow green breathing pulse (all cups together)
 */
void animBetting60() {
    unsigned long elapsed = millis() - animStartTime;
    float breathe = (sin(elapsed / 1000.0) + 1.0) / 2.0; // 0.0 to 1.0
    
    uint8_t brightness = 50 + (breathe * 205); // 50-255 range
    CRGB color = COLOR_GREEN;
    color.nscale8(brightness);
    
    fill_solid(leds, LED_COUNT, color);
    FastLED.show();
    updatePowerEstimate();
    
    // Continuous animation
}

/**
 * ANIM:BETTING_30 - Amber breathing (faster than 60, more urgent)
 */
void animBetting30() {
    unsigned long elapsed = millis() - animStartTime;
    float breathe = (sin(elapsed / 500.0) + 1.0) / 2.0; // 0.5 sec cycle (faster)
    
    uint8_t brightness = 50 + (breathe * 205); // 50-255 range
    CRGB color = COLOR_AMBER;
    color.nscale8(brightness);
    
    fill_solid(leds, LED_COUNT, color);
    FastLED.show();
    updatePowerEstimate();
    
    // Continuous animation
}

/**
 * ANIM:FINAL_CALL - Red breathing (fastest, most urgent)
 */
void animFinalCall() {
    unsigned long elapsed = millis() - animStartTime;
    float breathe = (sin(elapsed / 300.0) + 1.0) / 2.0; // 0.3 sec cycle (fastest)
    
    uint8_t brightness = 50 + (breathe * 205); // 50-255 range
    CRGB color = COLOR_RED;
    color.nscale8(brightness);
    
    fill_solid(leds, LED_COUNT, color);
    FastLED.show();
    updatePowerEstimate();
    
    // Continuous animation
}

/**
 * ANIM:RACE_START - "They're Off!" Two-phase race start animation
 * 
 * Phase 1 (Green Blast): All LEDs instantly go 100% bright green (#00FF00)
 *   - Holds for 1000ms — sharp, dramatic start (gates are open!)
 * 
 * Phase 2 (Galloping on Green): Runs continuously until next command
 *   - Background: All cups stay solid green (~40% brightness)
 *   - A bright white pulse sweeps across cups sequentially (1→2→3→...→20→1...)
 *   - Each cup's white flash is all 32 LEDs going full white for ~60ms
 *   - After the flash, cup fades back to green background
 *   - Tempo starts at 250ms between pulses and accelerates to 80ms over 45 seconds
 *   - Non-blocking millis() timing throughout
 */
void animRaceStart() {
    unsigned long now = millis();
    
    // ===== PHASE 1: Green Blast (1 second hold) =====
    if (raceStartPhase == 0) {
        // All LEDs were set to bright green in the command handler
        // Wait for 1 second to elapse
        if (now - animStartTime >= 1000) {
            // Transition to Phase 2: Galloping on Green
            raceStartPhase = 1;
            raceStartPhase2Start = now;
            raceStartCurrentCup = 1;
            raceStartFlashing = false;
            raceStartLastCupTime = now;  // Start first pulse timing
            
            // Set all cups to green background (~40% brightness = ~102/255)
            CRGB bgGreen = CRGB(0, 102, 0);
            fill_solid(leds, LED_COUNT, bgGreen);
            FastLED.show();
            updatePowerEstimate();
        }
        return;
    }
    
    // ===== PHASE 2: Galloping on Green =====
    
    // Calculate current gap speed based on time elapsed in phase 2
    // Starts at 250ms gap between cup pulses, accelerates to 80ms over 45 seconds
    unsigned long phase2Elapsed = now - raceStartPhase2Start;
    unsigned long currentGap;
    if (phase2Elapsed >= 45000) {
        currentGap = 80;  // Minimum gap (max speed) after 45 seconds
    } else {
        // Linear interpolation: 250ms → 80ms over 45 seconds
        currentGap = 250 - (unsigned long)(170.0 * (float)phase2Elapsed / 45000.0);
    }
    
    // White flash duration per cup (~60ms, snappy pulse)
    const unsigned long FLASH_DURATION = 60;
    
    if (raceStartFlashing) {
        // A cup is currently showing white — check if flash duration is over
        if (now - raceStartLastCupTime >= FLASH_DURATION) {
            // Flash complete — restore this cup to green background
            setCup(raceStartCurrentCup, CRGB(0, 102, 0));
            FastLED.show();
            updatePowerEstimate();
            raceStartFlashing = false;
            
            // Advance to next cup (wrap around 1→20→1)
            raceStartCurrentCup++;
            if (raceStartCurrentCup > NUM_CUPS) {
                raceStartCurrentCup = 1;
            }
            // raceStartLastCupTime stays as-is; gap timer starts from flash START
        }
    } else {
        // Waiting for the gap to elapse before starting next cup's white flash
        if (now - raceStartLastCupTime >= currentGap) {
            // Time to flash the current cup white!
            setCup(raceStartCurrentCup, CRGB(255, 255, 255));
            FastLED.show();
            updatePowerEstimate();
            raceStartFlashing = true;
            raceStartLastCupTime = now;  // Record when this flash started
        }
    }
}

/**
 * ANIM:CHAOS - Random cups, random colors, fast chaotic energy
 */
void animChaos() {
    static unsigned long lastUpdate = 0;
    if (millis() - lastUpdate < 60) return; // Very fast
    lastUpdate = millis();
    
    // Random 5 cups each frame
    FastLED.clear();
    for (int i = 0; i < 5; i++) {
        int cup = random(1, 21);
        CRGB colors[] = {COLOR_GOLD, COLOR_RED, COLOR_AMBER, COLOR_GREEN, COLOR_WHITE};
        CRGB color = colors[random(0, 5)];
        setCup(cup, color);
    }
    
    FastLED.show();
    updatePowerEstimate();
}

/**
 * ANIM:FINISH - Green/white checkered flag pattern (celebration!)
 * Alternates green and white every 500ms — full brightness, no cups off.
 */
void animFinish() {
    static unsigned long lastUpdate = 0;
    if (millis() - lastUpdate < 500) return; // Alternate every 500ms
    lastUpdate = millis();
    
    for (int cup = 1; cup <= NUM_CUPS; cup++) {
        if (animStep % 2 == 0) {
            // Even step: odd cups = green, even cups = white
            setCup(cup, (cup % 2 == 1) ? COLOR_GREEN : COLOR_WHITE);
        } else {
            // Odd step: even cups = green, odd cups = white
            setCup(cup, (cup % 2 == 0) ? COLOR_GREEN : COLOR_WHITE);
        }
    }
    
    FastLED.show();
    updatePowerEstimate();
    animStep++;
}

/**
 * ANIM:HEARTBEAT_COOLDOWN - Decelerating heartbeat
 * Starts at ~160 BPM with dramatic pulses, linearly slows to ~60 BPM
 * over COOLDOWN_DURATION_MS, then holds at 60 BPM indefinitely.
 */
void animHeartbeatCooldown() {
    unsigned long now = millis();
    unsigned long cooldownElapsed = now - cooldownStartTime;

    // Linear interpolation: 160 BPM → 60 BPM over COOLDOWN_DURATION_MS
    float progress = (float)cooldownElapsed / (float)COOLDOWN_DURATION_MS;
    if (progress > 1.0) progress = 1.0;

    float currentBPM = 160.0 - (progress * 100.0); // 160 → 60
    float periodMs = 60000.0 / currentBPM;

    // Brightness range narrows as BPM decreases
    // Peak (160 BPM): 77-255 (30%-100%)  →  Rest (60 BPM): 153-217 (60%-85%)
    uint8_t minBright = (uint8_t)(77 + progress * (153 - 77));
    uint8_t maxBright = (uint8_t)(255 - progress * (255 - 217));

    // Sine wave pulse based on current period
    float phase = fmod((float)(now - animStartTime), periodMs) / periodMs;
    float pulse = (sin(phase * 2.0 * PI) + 1.0) / 2.0; // 0.0 to 1.0

    uint8_t brightness = minBright + (uint8_t)(pulse * (maxBright - minBright));
    CRGB color = COLOR_RED;
    color.nscale8(brightness);

    // Slow fade for locked (winner) cups: gentle breathing at ~40 BPM
    float winnerPhase = fmod((float)(now - animStartTime), 1500.0) / 1500.0;
    float winnerPulse = (sin(winnerPhase * 2.0 * PI) + 1.0) / 2.0;
    uint8_t winnerBright = 140 + (uint8_t)(winnerPulse * 115); // 140-255 (55%-100%)

    for (int cup = 1; cup <= NUM_CUPS; cup++) {
        if (cupLocked[cup]) {
            CRGB lockedColor = cupLockedColor[cup];
            lockedColor.nscale8(winnerBright);
            setCup(cup, lockedColor);
        } else {
            setCup(cup, color);
        }
    }

    FastLED.show();
    updatePowerEstimate();
}

/**
 * ANIM:SILKS - Display saddle cloth colors on each cup
 * Static: alternating 4-LED blocks of primary/accent per cup
 */
void animSilks() {
    for (int cup = 1; cup <= NUM_CUPS; cup++) {
        int startIdx = (cup - 1) * LEDS_PER_CUP;
        for (int i = 0; i < LEDS_PER_CUP; i++) {
            // 4-LED blocks: 0-3 primary, 4-7 accent, 8-11 primary, ...
            bool isPrimary = ((i / 4) % 2 == 0);
            leds[startIdx + i] = isPrimary ? SILK_PRIMARY[cup] : SILK_ACCENT[cup];
        }
    }
    FastLED.show();
    updatePowerEstimate();
    // Static hold — stop the animation loop
    animationRunning = false;
}

/**
 * ANIM:RESULTS - Gold/Silver/Bronze on win/place/show, others dim
 */
void animResults() {
    FastLED.clear();
    
    // Spotlight on winners
    for (int cup = 1; cup <= NUM_CUPS; cup++) {
        if (cup == winCup) {
            setCup(cup, COLOR_GOLD);
        } else if (cup == placeCup) {
            setCup(cup, COLOR_SILVER);
        } else if (cup == showCup) {
            setCup(cup, COLOR_BRONZE);
        } else {
            // Others very dim
            setCup(cup, CRGB(10, 10, 10));
        }
    }
    
    FastLED.show();
    updatePowerEstimate();
    // Static display - stays until changed
}

/**
 * ANIM:RESULTS_ACTIVE - Entire mantle breathes in unison
 * Win/Place/Show: Pulsing gold/silver/bronze (40-100%) — dramatic pulse
 * Other 17 cups: Red heartbeat (15-60%) — visible throb, subordinate to winners
 * Same 2-second sine wave for all — cohesive, living display
 */
void animResultsActive() {
    unsigned long elapsed = millis() - animStartTime;
    
    // Shared 2-second breathing cycle for all cups (sin 0→1→0 in 2 sec)
    float breathe = (sin(elapsed * 3.14159f / 1000.0f - 1.5708f) + 1.0f) / 2.0f;
    
    // Winners: pulse 40%–100% (102-255) — dramatic dim-to-bright pulse
    uint8_t winnerBrightness = 102 + (uint8_t)(breathe * 153);  // 102-255 = 40-100%

    // Others: pulse 15%–60% (38-153) — visible throb, clearly dimmer than winners
    uint8_t otherBrightness = 38 + (uint8_t)(breathe * 115);    // 38-153 = 15-60%
    
    CRGB winnerGold, winnerSilver, winnerBronze;
    winnerGold   = COLOR_GOLD;
    winnerSilver = COLOR_SILVER;
    winnerBronze = COLOR_BRONZE;
    winnerGold.nscale8(winnerBrightness);
    winnerSilver.nscale8(winnerBrightness);
    winnerBronze.nscale8(winnerBrightness);
    
    CRGB otherRed = COLOR_RED;
    otherRed.nscale8(otherBrightness);
    
    for (int cup = 1; cup <= NUM_CUPS; cup++) {
        if (cup == winCup) {
            setCup(cup, winnerGold);
        } else if (cup == placeCup) {
            setCup(cup, winnerSilver);
        } else if (cup == showCup) {
            setCup(cup, winnerBronze);
        } else {
            setCup(cup, otherRed);
        }
    }
    
    FastLED.show();
    updatePowerEstimate();
}

/**
 * Update software-estimated power draw from current LED state
 */
void updatePowerEstimate() {
    uint32_t mW = calculate_unscaled_power_mW(leds, LED_COUNT);
    currentDrawMA = mW / 5; // 5V system
    if (currentDrawMA > peakDrawMA) {
        peakDrawMA = currentDrawMA;
    }
    if (currentDrawMA > 0 && (minDrawMA == 0 || currentDrawMA < minDrawMA)) {
        minDrawMA = currentDrawMA;
    }
}

/**
 * Convert hex color string to CRGB
 */
CRGB hexToRGB(String hex) {
    hex.replace("#", "");
    
    if (hex.length() != 6) {
        return CRGB::Black;
    }
    
    long number = strtol(hex.c_str(), NULL, 16);
    uint8_t r = (number >> 16) & 0xFF;
    uint8_t g = (number >> 8) & 0xFF;
    uint8_t b = number & 0xFF;
    
    return CRGB(r, g, b);
}

/**
 * Set a specific cup to a color (Cups 1-20, 32 LEDs each)
 */
void setCup(uint8_t cupNumber, CRGB color) {
    if (cupNumber < 1 || cupNumber > 20) return;
    
    int startIdx = (cupNumber - 1) * 32;
    int endIdx = startIdx + 32;
    
    for (int i = startIdx; i < endIdx; i++) {
        leds[i] = color;
    }
}

/**
 * Blink status LED
 */
void blinkStatus(int times) {
    for (int i = 0; i < times; i++) {
        digitalWrite(STATUS_LED_PIN, HIGH);
        delay(50);
        digitalWrite(STATUS_LED_PIN, LOW);
        delay(50);
    }
}

/**
 * Print startup banner
 */
void printBanner() {
    Serial.println("\n==================================================");
    Serial.println("   DDM Cup LED Controller - ESP32 FULL");
    Serial.println("   Version 3.1 - Complete Animation System");
    Serial.println("==================================================\n");
}

// =====================================================================
// ===== OLED DISPLAY FUNCTIONS ========================================
// =====================================================================

/**
 * Initialize SSD1306 OLED display over I2C
 * SDA: GPIO 21, SCL: GPIO 22, Address: 0x3C
 * Sets oledReady flag; all display calls check this before writing.
 */
void initOLED() {
    Serial.println("[OLED] Initializing SSD1306 128x64...");
    Wire.begin(OLED_SDA, OLED_SCL);

    if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
        Serial.println("[OLED] ERROR: SSD1306 not found — display disabled");
        oledReady = false;
        return;
    }

    oledReady = true;
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.display();
    Serial.println("[OLED] Display ready");
}

/**
 * Show boot splash screen — "DDM v3.1" centred for 2 seconds.
 * This is the ONLY blocking delay used for the display (startup only).
 */
void showBootSplash() {
    if (!oledReady) return;

    display.clearDisplay();

    // Large centred title
    display.setTextSize(2);
    display.setCursor(16, 12);
    display.println(F("DDM v3.1"));

    // Subtitle
    display.setTextSize(1);
    display.setCursor(16, 44);
    display.println(F("Cup LED Controller"));

    display.display();
    delay(2000);  // Blocking — startup only
}

/**
 * Non-blocking display refresh called every loop() iteration.
 * Redraws only when:
 *   - displayDirty flag is set (command just received), OR
 *   - 500 ms have elapsed since last refresh (RSSI can change)
 * Clears the dirty flag after each redraw.
 */
void updateDisplay() {
    if (!oledReady) return;

    unsigned long now = millis();

    // Skip if not dirty and interval hasn't elapsed
    if (!displayDirty && (now - lastDisplayUpdate < OLED_UPDATE_MS)) return;

    lastDisplayUpdate = now;
    displayDirty = false;

    updateDisplayNow();
}

/**
 * Actually render the five-line status screen:
 *   Line 1: "DDM CTRL"  (header, textSize 2, centered in yellow band)
 *   Line 2: MODE: <currentMode>
 *   Line 3: WiFi: <IP> or "NO WIFI"
 *   Line 4: RSSI: <value> dBm
 *   Line 5: LEDs: <LED_COUNT>
 */
void updateDisplayNow() {
    if (!oledReady) return;

    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);

    // ---- Line 1 (y=0): Header - textSize 2, centered in yellow band ----
    display.setTextSize(2);
    display.setCursor(16, 0);
    display.println(F("DDM CTRL"));

    // Thin separator line at yellow/blue boundary
    display.drawLine(0, 16, 127, 16, SSD1306_WHITE);

    display.setTextSize(1);       // 6x8 pixels per char for remaining lines

    // ---- Line 2 (y=20): Current mode ----
    display.setCursor(0, 20);
    display.print(F("MODE: "));
    display.println(currentMode);

    // ---- Line 3 (y=32): WiFi + IP ----
    display.setCursor(0, 32);
    if (WiFi.status() == WL_CONNECTED) {
        display.print(F("WiFi: "));
        display.println(WiFi.localIP().toString());
    } else {
        display.println(F("WiFi: NO WIFI"));
    }

    // ---- Line 4 (y=42): Signal strength ----
    display.setCursor(0, 42);
    if (WiFi.status() == WL_CONNECTED) {
        display.print(F("RSSI: "));
        display.print(WiFi.RSSI());
        display.println(F(" dBm"));
    } else {
        display.println(F("RSSI: --"));
    }

    // ---- Line 5 (y=52): Current draw ----
    display.setCursor(0, 52);
    display.print(F("Draw: "));
    display.print(currentDrawMA);
    display.print(F("mA (pk:"));
    display.print(peakDrawMA);
    display.print(F(")"));

    display.display();
}
