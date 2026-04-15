// ddm_led_controller.ino - DDM Cup LED Controller with ANIMATIONS
// ESP32 WROOM-32 with FastLED - WiFi, Socket Server, Full Animation Suite
// Version 3.1 - Complete Animation System

#include <WiFi.h>
#include <FastLED.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <EEPROM.h>

// ===== ANIMATION PARAMS (live-tunable, EEPROM-persisted) =====
#define EEPROM_MAGIC 0xDDu   // change if struct layout changes
#define EEPROM_SIZE  128

struct AnimParams {
    uint8_t  magic;                // must equal EEPROM_MAGIC

    // Global
    uint8_t  masterBrightness;     // 0-255, default 128

    // GATES_BURST — gate opening
    uint8_t  gateStepMs;           // ms per cup step, default 70
    uint8_t  gateFadeMs;           // ms to fade cup dark, default 60
    uint8_t  gateHoldMs;           // ms pause after all dark, default 80

    // GATES_BURST — hoofbeat gallop
    uint8_t  hoofQuickStart;       // quick beat start ms, default 120
    uint8_t  hoofQuickEnd;         // quick beat end ms, default 40
    uint16_t hoofPauseStart;       // pause beat start ms, default 350
    uint8_t  hoofPauseEnd;         // pause beat end ms, default 100
    uint8_t  hoofFlashMs;          // flash duration ms, default 80
    uint8_t  hoofFadeMs;           // fade duration ms, default 120

    // RESULTS_ENTRY — winner chase
    uint8_t  chaseGoldMs;          // gold chase interval ms, default 18
    uint8_t  chaseSilverMs;        // silver chase interval ms, default 42
    uint8_t  chaseBronzeMs;        // bronze chase interval ms, default 65

    // RESULTS_ENTRY — loser heartbeat
    uint8_t  loserBpmStart;        // starting BPM, default 140
    uint8_t  loserBpmEnd;          // ending BPM, default 50
    uint16_t loserDecelSec;        // decel duration seconds, default 300

    // HEARTBEAT_COOLDOWN
    uint8_t  cooldownBpmStart;     // starting BPM, default 160
    uint8_t  cooldownBpmEnd;       // ending BPM, default 60
    uint8_t  cooldownDecelSec;     // decel duration seconds (max 255), default 90

    // BETTING breathe speeds (cycle half-period ms)
    uint16_t betting60PeriodMs;    // default 1000
    uint16_t betting30PeriodMs;    // default 500
    uint16_t finalCallPeriodMs;    // default 300

    // AT_THE_GATE
    uint16_t atGatePeriodMs;       // breathe cycle half-period ms, default 2000
    uint8_t  atGateMinBright;      // min brightness, default 140
};

AnimParams P;  // global params instance — always use P.xxx in animation code

void loadParams() {
    EEPROM.begin(EEPROM_SIZE);
    EEPROM.get(0, P);
    if (P.magic != EEPROM_MAGIC) {
        // First boot or struct changed — load defaults
        resetParamsToDefault();
        saveParams();
    }
}

void saveParams() {
    EEPROM.put(0, P);
    EEPROM.commit();
}

void resetParamsToDefault() {
    P.magic             = EEPROM_MAGIC;
    P.masterBrightness  = 128;
    P.gateStepMs        = 70;
    P.gateFadeMs        = 60;
    P.gateHoldMs        = 80;
    P.hoofQuickStart    = 120;
    P.hoofQuickEnd      = 40;
    P.hoofPauseStart    = 350;
    P.hoofPauseEnd      = 100;
    P.hoofFlashMs       = 80;
    P.hoofFadeMs        = 120;
    P.chaseGoldMs       = 18;
    P.chaseSilverMs     = 42;
    P.chaseBronzeMs     = 65;
    P.loserBpmStart     = 140;
    P.loserBpmEnd       = 50;
    P.loserDecelSec     = 300;
    P.cooldownBpmStart  = 160;
    P.cooldownBpmEnd    = 60;
    P.cooldownDecelSec  = 90;
    P.betting60PeriodMs = 1000;
    P.betting30PeriodMs = 500;
    P.finalCallPeriodMs = 300;
    P.atGatePeriodMs    = 2000;
    P.atGateMinBright   = 140;
}

// ===== CONFIGURATION =====
#define WIFI_SSID "BMP_WIFI_MAIN"
#define WIFI_PASSWORD "Derby1961"

#define SOCKET_PORT 5005
#define LED_PIN 18
#define LED_COUNT 636
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
CRGB leds[636];
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

// GATES_BURST animation state
int   gatesPhase = 0;                          // 0=flash, 1=green, 2=chaos, 3=gallop
unsigned long gatesPhaseStart = 0;             // millis when current phase began
// LEGACY - chaos phase removed
// unsigned long cupChaosLastChange[NUM_CUPS + 1];
// unsigned long cupChaosInterval[NUM_CUPS + 1];
// uint8_t       cupChaosState[NUM_CUPS + 1];
// Hoofbeat sequences (3 simultaneous horses)
#define NUM_HORSES 3
int   hoofPos[NUM_HORSES];               // current cup position for each horse (1-20)
int   hoofBeat[NUM_HORSES];              // current beat within sequence (0-2 = quick, 3 = pause)
unsigned long hoofLastBeat[NUM_HORSES];  // millis of last beat for each horse
bool  hoofFlashing[NUM_CUPS + 1];        // true if cup is currently flashing
unsigned long hoofFlashStart[NUM_CUPS + 1]; // millis when flash started

// Gate opening state (2-gate 4-door sweep)
int   gateStep = 0;                         // current step 0-4
unsigned long gateStepStart = 0;            // millis when current step began
unsigned long gateFadeStart[NUM_CUPS + 1];  // millis when each cup started fading
bool  gateFading[NUM_CUPS + 1];             // true if cup is currently fading to black
bool  gateDark[NUM_CUPS + 1];               // true if cup has fully gone dark

// Cup locking for custom colors during animations
bool cupLocked[NUM_CUPS + 1] = {false};  // cups 1-20, index 0 unused
CRGB cupLockedColor[NUM_CUPS + 1];      // color for locked cups

// WELCOME animation state
int welcomePhase = 0;                  // 0-4 for phases 1-5
unsigned long welcomePhaseStart = 0;   // millis() when current phase began
int welcomeCometPos = 0;               // comet head position for chase phase
bool welcomeCometForward = true;       // comet direction for chase phase
int welcomeCometSweeps = 0;            // number of completed sweeps
CRGB welcomeMarchColors[NUM_CUPS];     // color assignment for march phase

// HEARTBEAT_COOLDOWN animation state
unsigned long cooldownStartTime = 0;
// COOLDOWN_DURATION_MS now derived from P.cooldownDecelSec

// RESULTS_ENTRY animation state
int  cupChasePos[NUM_CUPS + 1];                // current head LED offset (0 to CUP_LED_COUNT[cup]-1)
unsigned long cupChaseLastStep[NUM_CUPS + 1];  // millis of last chase step per cup

unsigned long resultsEntryStart = 0;           // millis when RESULTS_ENTRY triggered
bool resultsFinalizing = false;                // true after RESULTS:FINALIZE received
unsigned long finalizeTime = 0;               // millis when RESULTS:FINALIZE received

// RESULTS_DECEL_MS now derived from P.loserDecelSec

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
const CRGB SILK_PRIMARY[NUM_CUPS + 1] = {
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
const CRGB SILK_ACCENT[NUM_CUPS + 1] = {
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

// Per-cup LED counts (cups 6,9,15,17 have 31 LEDs, all others 32)
const uint8_t CUP_LED_COUNT[NUM_CUPS + 1] = {
  0,                          // index 0 unused
  32, 32, 32, 32, 32,        // cups 1-5
  31, 32, 32, 31, 32,        // cups 6-10
  32, 32, 32, 32, 31,        // cups 11-15
  32, 31, 32, 32, 32         // cups 16-20
};

// Pre-calculated start indices (filled in setup())
int CUP_START_INDEX[NUM_CUPS + 1] = {0};

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
void animAtTheGate();
void animGatesBurst();
void animChaos();
void animFinish();
void animHeartbeatCooldown();
void animSilks();
void animResultsEntry();

/**
 * SETUP - Runs once at startup
 */
void setup() {
    // Initialize serial
    Serial.begin(115200);
    delay(1000);
    loadParams();

    printBanner();
    
    // Initialize status LED
    pinMode(STATUS_LED_PIN, OUTPUT);
    digitalWrite(STATUS_LED_PIN, LOW);
    
    // Initialize OLED display (before WiFi so we can show status)
    initOLED();
    showBootSplash();   // "DDM v3.1" for 2 seconds (blocking, startup only)
    
    // Calculate cumulative start indices
    CUP_START_INDEX[1] = 0;
    for (int i = 2; i <= NUM_CUPS; i++) {
        CUP_START_INDEX[i] = CUP_START_INDEX[i-1] + CUP_LED_COUNT[i-1];
    }
    // Print for verification
    Serial.println("[LED] Cup start indices:");
    for (int i = 1; i <= NUM_CUPS; i++) {
        Serial.println("  Cup " + String(i) + ": start=" + String(CUP_START_INDEX[i]) + " count=" + String(CUP_LED_COUNT[i]));
    }

    // Initialize LED strip
    Serial.println("[LED] Initializing 636 LEDs on GPIO 18...");
    FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, LED_COUNT);
    FastLED.setBrightness(P.masterBrightness);
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
    String originalCmd = cmd;  // preserve original case for PARAM:SET
    cmd.toUpperCase();
    
    // PING - Test connection
    if (cmd == "PING") {
        blinkStatus(1);
        return "PONG";
    }
    
    // PARAM:GET:ALL - Return all params as key=value pairs
    else if (cmd == "PARAM:GET:ALL") {
        String result = "PARAMS:";
        result += "masterBrightness=" + String(P.masterBrightness) + ",";
        result += "gateStepMs=" + String(P.gateStepMs) + ",";
        result += "gateFadeMs=" + String(P.gateFadeMs) + ",";
        result += "gateHoldMs=" + String(P.gateHoldMs) + ",";
        result += "hoofQuickStart=" + String(P.hoofQuickStart) + ",";
        result += "hoofQuickEnd=" + String(P.hoofQuickEnd) + ",";
        result += "hoofPauseStart=" + String(P.hoofPauseStart) + ",";
        result += "hoofPauseEnd=" + String(P.hoofPauseEnd) + ",";
        result += "hoofFlashMs=" + String(P.hoofFlashMs) + ",";
        result += "hoofFadeMs=" + String(P.hoofFadeMs) + ",";
        result += "chaseGoldMs=" + String(P.chaseGoldMs) + ",";
        result += "chaseSilverMs=" + String(P.chaseSilverMs) + ",";
        result += "chaseBronzeMs=" + String(P.chaseBronzeMs) + ",";
        result += "loserBpmStart=" + String(P.loserBpmStart) + ",";
        result += "loserBpmEnd=" + String(P.loserBpmEnd) + ",";
        result += "loserDecelSec=" + String(P.loserDecelSec) + ",";
        result += "cooldownBpmStart=" + String(P.cooldownBpmStart) + ",";
        result += "cooldownBpmEnd=" + String(P.cooldownBpmEnd) + ",";
        result += "cooldownDecelSec=" + String(P.cooldownDecelSec) + ",";
        result += "betting60PeriodMs=" + String(P.betting60PeriodMs) + ",";
        result += "betting30PeriodMs=" + String(P.betting30PeriodMs) + ",";
        result += "finalCallPeriodMs=" + String(P.finalCallPeriodMs) + ",";
        result += "atGatePeriodMs=" + String(P.atGatePeriodMs) + ",";
        result += "atGateMinBright=" + String(P.atGateMinBright);
        return result;
    }

    // PARAM:SET:key:value - Set a single param live
    else if (cmd.startsWith("PARAM:SET:")) {
        int colonPos = originalCmd.indexOf(':', 10);
        if (colonPos > 0) {
            String key = originalCmd.substring(10, colonPos);
            int value  = originalCmd.substring(colonPos + 1).toInt();

            if (key == "masterBrightness") { P.masterBrightness = constrain(value, 0, 255); FastLED.setBrightness(P.masterBrightness); }
            else if (key == "gateStepMs")        P.gateStepMs        = constrain(value, 10, 200);
            else if (key == "gateFadeMs")        P.gateFadeMs        = constrain(value, 10, 200);
            else if (key == "gateHoldMs")        P.gateHoldMs        = constrain(value, 0, 500);
            else if (key == "hoofQuickStart")    P.hoofQuickStart    = constrain(value, 20, 250);
            else if (key == "hoofQuickEnd")      P.hoofQuickEnd      = constrain(value, 10, 100);
            else if (key == "hoofPauseStart")    P.hoofPauseStart    = constrain(value, 50, 1000);
            else if (key == "hoofPauseEnd")      P.hoofPauseEnd      = constrain(value, 20, 300);
            else if (key == "hoofFlashMs")       P.hoofFlashMs       = constrain(value, 20, 200);
            else if (key == "hoofFadeMs")        P.hoofFadeMs        = constrain(value, 20, 300);
            else if (key == "chaseGoldMs")       P.chaseGoldMs       = constrain(value, 5, 100);
            else if (key == "chaseSilverMs")     P.chaseSilverMs     = constrain(value, 5, 100);
            else if (key == "chaseBronzeMs")     P.chaseBronzeMs     = constrain(value, 5, 150);
            else if (key == "loserBpmStart")     P.loserBpmStart     = constrain(value, 60, 200);
            else if (key == "loserBpmEnd")       P.loserBpmEnd       = constrain(value, 30, 100);
            else if (key == "loserDecelSec")     P.loserDecelSec     = constrain(value, 30, 600);
            else if (key == "cooldownBpmStart")  P.cooldownBpmStart  = constrain(value, 60, 200);
            else if (key == "cooldownBpmEnd")    P.cooldownBpmEnd    = constrain(value, 30, 100);
            else if (key == "cooldownDecelSec")  P.cooldownDecelSec  = constrain(value, 10, 255);
            else if (key == "betting60PeriodMs") P.betting60PeriodMs = constrain(value, 200, 3000);
            else if (key == "betting30PeriodMs") P.betting30PeriodMs = constrain(value, 100, 2000);
            else if (key == "finalCallPeriodMs") P.finalCallPeriodMs = constrain(value, 50, 1000);
            else if (key == "atGatePeriodMs")    P.atGatePeriodMs    = constrain(value, 500, 5000);
            else if (key == "atGateMinBright")   P.atGateMinBright   = constrain(value, 0, 200);
            else return "ERROR:UNKNOWN_PARAM:" + key;

            return "OK:PARAM:SET:" + key + "=" + String(value);
        }
        return "ERROR:INVALID_PARAM_CMD";
    }

    // PARAM:SAVE - Write current params to EEPROM
    else if (cmd == "PARAM:SAVE") {
        saveParams();
        return "OK:PARAM:SAVED";
    }

    // PARAM:RESET - Reset to defaults and save
    else if (cmd == "PARAM:RESET") {
        resetParamsToDefault();
        saveParams();
        FastLED.setBrightness(P.masterBrightness);
        return "OK:PARAM:RESET";
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
            
            if (cupNum >= 1 && cupNum <= NUM_CUPS) {
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
    
    // LEGACY - ANIM:RACE_START replaced by ANIM:GATES_BURST
    /*
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
    */

    // ANIM:AT_THE_GATE - Horses loading, steady bright white glow
    else if (cmd == "ANIM:AT_THE_GATE") {
        stopAnimation();
        currentMode = "AT_THE_GATE";
        currentAnimation = "AT_THE_GATE";
        animationRunning = true;
        animStartTime = millis();
        // All cups full bright white
        fill_solid(leds, LED_COUNT, CRGB(255, 255, 255));
        FastLED.show();
        updatePowerEstimate();
        return "OK:ANIM:AT_THE_GATE";
    }

    // ANIM:GATES_BURST - Explosive race start animation
    else if (cmd == "ANIM:GATES_BURST") {
        stopAnimation();
        // Initialize gate opening
        gateStep = 0;
        gateStepStart = millis();
        for (int i = 1; i <= NUM_CUPS; i++) {
            gateFading[i] = false;
            gateDark[i] = false;
        }
        gatesPhase = 0;
        gatesPhaseStart = millis();
        // All cups start full white
        fill_solid(leds, LED_COUNT, CRGB(255, 255, 255));
        FastLED.show();
        updatePowerEstimate();
        currentMode = "GATES_BURST";
        currentAnimation = "GATES_BURST";
        animationRunning = true;
        return "OK:ANIM:GATES_BURST";
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

    // ANIM:RESULTS_ENTRY - New results entry animation with chase + heartbeat
    else if (cmd == "ANIM:RESULTS_ENTRY") {
        stopAnimation();
        for (int i = 1; i <= NUM_CUPS; i++) {
            cupLocked[i] = false;
        }
        resultsEntryStart = millis();
        resultsFinalizing = false;
        finalizeTime = 0;
        for (int i = 0; i <= NUM_CUPS; i++) {
            cupChasePos[i] = 0;
            cupChaseLastStep[i] = 0;
        }
        currentMode = "RESULTS_ENTRY";
        currentAnimation = "RESULTS_ENTRY";
        animationRunning = true;
        return "OK:ANIM:RESULTS_ENTRY";
    }

    // RESULTS:FINALIZE - Begin deceleration of winner chases
    else if (cmd == "RESULTS:FINALIZE") {
        resultsFinalizing = true;
        finalizeTime = millis();
        return "OK:RESULTS:FINALIZE";
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
            
            if (winCup >= 1 && winCup <= NUM_CUPS && 
                placeCup >= 1 && placeCup <= NUM_CUPS && 
                showCup >= 1 && showCup <= NUM_CUPS) {
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
            
            if (winCup >= 1 && winCup <= NUM_CUPS && 
                placeCup >= 1 && placeCup <= NUM_CUPS && 
                showCup >= 1 && showCup <= NUM_CUPS) {
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
            
            if (cupNum >= 1 && cupNum <= NUM_CUPS) {
                cupLocked[cupNum] = true;
                cupLockedColor[cupNum] = CRGB(r, g, b);
                cupChasePos[cupNum] = 0;
                cupChaseLastStep[cupNum] = 0;
                // Do not call setCup() or FastLED.show() — animation loop handles rendering
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
            for (int i = 1; i <= NUM_CUPS; i++) {
                cupLocked[i] = false;
            }
            return "OK:CUP:UNLOCKED:ALL";
        } else {
            // Unlock specific cup
            int cupNum = param.toInt();
            if (cupNum >= 1 && cupNum <= NUM_CUPS) {
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
    // LEGACY - RACE_START replaced by GATES_BURST
    // } else if (currentAnimation == "RACE_START") {
    //     animRaceStart();
    } else if (currentAnimation == "AT_THE_GATE") {
        animAtTheGate();
    } else if (currentAnimation == "GATES_BURST") {
        animGatesBurst();
    } else if (currentAnimation == "CHAOS") {
        animChaos();
    } else if (currentAnimation == "FINISH") {
        animFinish();
    } else if (currentAnimation == "HEARTBEAT_COOLDOWN") {
        animHeartbeatCooldown();
    } else if (currentAnimation == "SILKS") {
        animSilks();
    } else if (currentAnimation == "RESULTS_ENTRY") {
        animResultsEntry();
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
    float breathe = (sin(elapsed / (float)P.betting60PeriodMs) + 1.0) / 2.0; // 0.0 to 1.0
    
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
    float breathe = (sin(elapsed / (float)P.betting30PeriodMs) + 1.0) / 2.0; // faster
    
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
    float breathe = (sin(elapsed / (float)P.finalCallPeriodMs) + 1.0) / 2.0; // fastest
    
    uint8_t brightness = 50 + (breathe * 205); // 50-255 range
    CRGB color = COLOR_RED;
    color.nscale8(brightness);
    
    fill_solid(leds, LED_COUNT, color);
    FastLED.show();
    updatePowerEstimate();
    
    // Continuous animation
}

/**
 * LEGACY - ANIM:RACE_START replaced by ANIM:GATES_BURST
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
 * ANIM:AT_THE_GATE - Steady bright white with slow gentle breathe
 * All 20 cups glow white, slow sine pulse 85%→100% brightness
 * Builds tension while horses load into the gate
 */
void animAtTheGate() {
    unsigned long elapsed = millis() - animStartTime;
    // Slow breathe: 3 second cycle, 55%→100% brightness
    // sin() period = 2π × 477 ≈ 3000ms
    float breathe = (sin(elapsed / (float)P.atGatePeriodMs) + 1.0f) / 2.0f;
    uint8_t brightness = P.atGateMinBright + (uint8_t)(breathe * (255 - P.atGateMinBright));
    CRGB color = CRGB(255, 255, 255);
    color.nscale8(brightness);
    fill_solid(leds, LED_COUNT, color);
    FastLED.show();
    updatePowerEstimate();
}

/**
 * ANIM:GATES_BURST - Explosive race start
 * Phase 0: Gate opening — 5-gate outward white burst
 * Phase 2 (800–3500ms): Per-cup chaos — each cup independently cycles
 *                        silk color / white / green at random intervals
 * Phase 3 (3500ms+):   Right-to-left gallop on dim green background,
 *                        accelerating from 200ms→60ms stride over 45s
 */
void animGatesBurst() {
    unsigned long now = millis();
    unsigned long elapsed = now - gatesPhaseStart;

    // ===== PHASE 0: Gate opening — 2 gates, 4 doors, sweep dark outward =====
    if (gatesPhase == 0) {
        unsigned long gateElapsed = now - gatesPhaseStart;

        // Cup groups per step (both gates fire simultaneously)
        const int stepCups[5][4] = {
            {5,  6,  15, 16},   // step 0: inner-most (gate center)
            {4,  7,  14, 17},   // step 1
            {3,  8,  13, 18},   // step 2
            {2,  9,  12, 19},   // step 3
            {1,  10, 11, 20}    // step 4: outer-most
        };

        // Trigger new step when timer elapses
        if (gateStep < 5 && now - gateStepStart >= (unsigned long)(gateStep == 0 ? 0 : P.gateStepMs)) {
            // Start fading the cups in this step
            for (int i = 0; i < 4; i++) {
                int cup = stepCups[gateStep][i];
                gateFading[cup] = true;
                gateFadeStart[cup] = now;
                gateDark[cup] = false;
            }
            gateStep++;
            gateStepStart = now;
        }

        // Render all cups
        bool allDark = true;
        for (int cup = 1; cup <= NUM_CUPS; cup++) {
            if (gateFading[cup]) {
                unsigned long fadeElapsed = now - gateFadeStart[cup];
                if (fadeElapsed >= P.gateFadeMs) {
                    // Fully dark
                    setCup(cup, CRGB(0, 0, 0));
                    gateFading[cup] = false;
                    gateDark[cup] = true;
                } else {
                    // Fading
                    float fadeProgress = (float)fadeElapsed / (float)P.gateFadeMs;
                    uint8_t brightness = (uint8_t)(255.0f * (1.0f - fadeProgress));
                    setCup(cup, CRGB(brightness, brightness, brightness));
                    allDark = false;
                }
            } else if (!gateDark[cup]) {
                // Not yet triggered — stays white
                setCup(cup, CRGB(255, 255, 255));
                allDark = false;
            }
        }

        FastLED.show();
        updatePowerEstimate();

        // Once all cups dark, hold briefly then go straight to gallop
        if (allDark && gateStep >= 5) {
            if (now - gateStepStart >= P.gateHoldMs) {
                // Transition directly to Phase 3 (gallop) — skip chaos entirely
                gatesPhase = 3;
                gatesPhaseStart = now;
                // Stagger horse starting positions
                hoofPos[0] = 20;
                hoofPos[1] = 14;
                hoofPos[2] = 7;
                hoofBeat[0] = 0;
                hoofBeat[1] = 1;
                hoofBeat[2] = 2;
                hoofLastBeat[0] = now;
                hoofLastBeat[1] = now - 40;
                hoofLastBeat[2] = now - 80;
                for (int i = 1; i <= NUM_CUPS; i++) {
                    hoofFlashing[i] = false;
                    hoofFlashStart[i] = 0;
                }
                // Set dim green background for gallop
                fill_solid(leds, LED_COUNT, CRGB(0, 64, 0));
                FastLED.show();
                updatePowerEstimate();
            }
        }
        return;
    }

    // Phase 2 (chaos) is removed — skip directly to Phase 3 if somehow reached
    if (gatesPhase == 2) {
        gatesPhase = 3;
        gatesPhaseStart = now;
        return;
    }

    // ===== PHASE 3: Hoofbeat rhythm gallop =====
    if (gatesPhase == 3) {
        unsigned long phase3Elapsed = now - gatesPhaseStart;

        // Tempo accelerates over 45 seconds
        // Quick beat interval: 120ms → 40ms
        // Pause duration: 350ms → 100ms
        float tempoProgress = (phase3Elapsed >= 45000) ? 1.0f : (float)phase3Elapsed / 45000.0f;
        unsigned long quickBeat = P.hoofQuickStart - (unsigned long)((float)(P.hoofQuickStart - P.hoofQuickEnd) * tempoProgress);
        unsigned long pauseBeat = P.hoofPauseStart - (unsigned long)((float)(P.hoofPauseStart - P.hoofPauseEnd) * tempoProgress);

        const unsigned long FLASH_DURATION = P.hoofFlashMs;
        const unsigned long FADE_DURATION  = P.hoofFadeMs;

        // Update each horse sequence
        for (int h = 0; h < NUM_HORSES; h++) {
            unsigned long interval = (hoofBeat[h] == 3) ? pauseBeat : quickBeat;

            if (now - hoofLastBeat[h] >= interval) {
                hoofLastBeat[h] = now;

                if (hoofBeat[h] < 3) {
                    // Quick beat — flash current cup white, advance position
                    int cup = hoofPos[h];
                    hoofFlashing[cup] = true;
                    hoofFlashStart[cup] = now;
                    setCup(cup, CRGB(255, 255, 255));

                    // Move right to left
                    hoofPos[h]--;
                    if (hoofPos[h] < 1) hoofPos[h] = NUM_CUPS;

                    hoofBeat[h]++;
                } else {
                    // Pause beat (beat 3) — no flash, just advance position slightly
                    hoofBeat[h] = 0;  // reset to next sequence
                    // Skip 1 cup during suspension (all 4 feet off ground)
                    hoofPos[h]--;
                    if (hoofPos[h] < 1) hoofPos[h] = NUM_CUPS;
                }

                FastLED.show();
                updatePowerEstimate();
            }
        }

        // Fade flashing cups back to dim green
        bool needsShow = false;
        for (int cup = 1; cup <= NUM_CUPS; cup++) {
            if (hoofFlashing[cup]) {
                unsigned long flashElapsed = now - hoofFlashStart[cup];
                if (flashElapsed >= FLASH_DURATION + FADE_DURATION) {
                    setCup(cup, CRGB(0, 64, 0));
                    hoofFlashing[cup] = false;
                    needsShow = true;
                } else if (flashElapsed >= FLASH_DURATION) {
                    float fadeProgress = (float)(flashElapsed - FLASH_DURATION) / (float)FADE_DURATION;
                    uint8_t r = (uint8_t)(255.0f * (1.0f - fadeProgress));
                    uint8_t g = (uint8_t)(255.0f * (1.0f - fadeProgress) + 64.0f * fadeProgress);
                    uint8_t b = (uint8_t)(255.0f * (1.0f - fadeProgress));
                    setCup(cup, CRGB(r, g, b));
                    needsShow = true;
                }
            }
        }
        if (needsShow) {
            FastLED.show();
            updatePowerEstimate();
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
 * over configured decel duration, then holds at end BPM indefinitely.
 */
void animHeartbeatCooldown() {
    unsigned long now = millis();
    unsigned long cooldownElapsed = now - cooldownStartTime;

    // Linear interpolation: BPM deceleration over configured duration
    float progress = (float)cooldownElapsed / ((float)P.cooldownDecelSec * 1000.0f);
    if (progress > 1.0) progress = 1.0;

    float currentBPM = (float)P.cooldownBpmStart - (progress * (float)(P.cooldownBpmStart - P.cooldownBpmEnd));
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
        setCup(cup, SILK_PRIMARY[cup]);
    }
    FastLED.show();
    updatePowerEstimate();
    // Static hold — stop the animation loop
    animationRunning = false;
}

// LEGACY - animResults()
/*
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
*/

// LEGACY - animResultsActive()
/*
void animResultsActive() {
    unsigned long elapsed = millis() - animStartTime;

    // Shared 2-second breathing cycle for all cups (sin 0→1→0 in 2 sec)
    float breathe = (sin(elapsed * M_PI / 1000.0f - (M_PI / 2.0f)) + 1.0f) / 2.0f;

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
*/

/**
 * ANIM:RESULTS_ENTRY - New results entry animation
 * Unlocked cups: decelerating red heartbeat (140→50 BPM over 5 min)
 * Locked cups: colored chase with tail, decelerating to heartbeat after FINALIZE
 */
void animResultsEntry() {
    unsigned long now = millis();

    // Clear all LEDs to prevent stale data bleeding between frames
    FastLED.clear();

    // ===== Part B: Winner Cup Chase (locked cups) =====
    for (int cup = 1; cup <= NUM_CUPS; cup++) {
        if (!cupLocked[cup]) continue;

        int cupLedCount = CUP_LED_COUNT[cup];
        CRGB lockedCol = cupLockedColor[cup];

        // Determine base chase interval from locked color
        float baseInterval;
        if (lockedCol.r == 255 && lockedCol.g == 215 && lockedCol.b == 0) {
            baseInterval = (float)P.chaseGoldMs;
        } else if (lockedCol.r == 192 && lockedCol.g == 192 && lockedCol.b == 192) {
            baseInterval = (float)P.chaseSilverMs;
        } else if (lockedCol.r == 205 && lockedCol.g == 127 && lockedCol.b == 50) {
            baseInterval = (float)P.chaseBronzeMs;
        } else {
            baseInterval = 40.0;   // Other
        }

        // Compute blend factor if finalizing
        float b = 0.0;
        if (resultsFinalizing) {
            b = (float)(now - finalizeTime) / 180000.0;
            if (b > 1.0) b = 1.0;
        }

        // Winner heartbeat values (used during/after finalization)
        float winnerPeriodMs = 1500.0;   // 40 BPM
        float winnerPhase = fmod((float)(now - finalizeTime), winnerPeriodMs) / winnerPeriodMs;
        float winnerPulse  = (sin(winnerPhase * 2.0 * PI) + 1.0) / 2.0;
        uint8_t winnerBright = 140 + (uint8_t)(winnerPulse * 115);  // 140–255

        // If fully finalized, skip chase — render pure heartbeat
        if (resultsFinalizing && b >= 1.0) {
            CRGB winColor = lockedCol;
            winColor.nscale8(winnerBright);
            int startIdx = CUP_START_INDEX[cup];
            for (int i = startIdx; i < startIdx + cupLedCount; i++) {
                leds[i] = winColor;
            }
            continue;
        }

        // Compute effective interval with deceleration
        float effectiveInterval = baseInterval;
        if (resultsFinalizing) {
            effectiveInterval = baseInterval * (1.0 + b * 4.0);
        }

        // Advance chase position (non-blocking)
        if (now - cupChaseLastStep[cup] >= (unsigned long)effectiveInterval) {
            cupChasePos[cup]++;
            if (cupChasePos[cup] >= cupLedCount) cupChasePos[cup] = 0;
            cupChaseLastStep[cup] = now;
        }

        // Render LEDs directly
        int startIdx = CUP_START_INDEX[cup];

        // Base: all LEDs in cup at 15% brightness
        for (int i = 0; i < cupLedCount; i++) {
            int stripIdx = startIdx + i;
            leds[stripIdx] = lockedCol;
            leds[stripIdx].nscale8(38);
        }

        // Tail: 6 LEDs behind head, decreasing brightness
        // Tail offsets -1 through -6, brightness: 200, 150, 100, 60, 30, 10
        uint8_t tailBright[] = {200, 150, 100, 60, 30, 10};
        for (int t = 1; t <= 6; t++) {
            int tailOffset = (cupChasePos[cup] - t + cupLedCount) % cupLedCount;
            int stripIdx = startIdx + tailOffset;
            CRGB tailColor = lockedCol;
            uint8_t chaseBright = tailBright[t - 1];

            if (resultsFinalizing && b > 0) {
                uint8_t finalBright = (uint8_t)((1.0 - b) * chaseBright + b * winnerBright);
                tailColor.nscale8(finalBright);
            } else {
                tailColor.nscale8(chaseBright);
            }
            leds[stripIdx] = tailColor;
        }

        // Head: 3 LEDs at full brightness (center + 1 either side)
        for (int h = -1; h <= 1; h++) {
            int headOffset = (cupChasePos[cup] + h + cupLedCount) % cupLedCount;
            int stripIdx = startIdx + headOffset;
            CRGB headColor = lockedCol;
            uint8_t chaseBright = 255;

            if (resultsFinalizing && b > 0) {
                uint8_t finalBright = (uint8_t)((1.0 - b) * chaseBright + b * winnerBright);
                headColor.nscale8(finalBright);
            } else {
                headColor.nscale8(chaseBright);
            }
            leds[stripIdx] = headColor;
        }
    }

    // ===== Part A: Loser Heartbeat (all unlocked cups) =====
    float progress = (float)(now - resultsEntryStart) / ((float)P.loserDecelSec * 1000.0f);
    if (progress > 1.0) progress = 1.0;

    float currentBPM = (float)P.loserBpmStart - (progress * (float)(P.loserBpmStart - P.loserBpmEnd));
    float periodMs = 60000.0 / currentBPM;

    uint8_t minBright = 51  + (uint8_t)(progress * 89);  // 51→140  (20%→55%)
    uint8_t maxBright = 255 - (uint8_t)(progress * 64);  // 255→191 (100%→75%)

    float phase = fmod((float)(now - resultsEntryStart), periodMs) / periodMs;
    float pulse  = (sin(phase * 2.0 * PI) + 1.0) / 2.0;
    uint8_t loserBrightness = minBright + (uint8_t)(pulse * (maxBright - minBright));

    CRGB loserColor = COLOR_RED;
    loserColor.nscale8(loserBrightness);

    // Apply loser heartbeat to unlocked cups
    for (int cup = 1; cup <= NUM_CUPS; cup++) {
        if (!cupLocked[cup]) {
            int startIdx = CUP_START_INDEX[cup];
            int count = CUP_LED_COUNT[cup];
            for (int i = startIdx; i < startIdx + count; i++) {
                leds[i] = loserColor;
            }
        }
    }

    // ===== Part C: Show Output =====
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
    if (cupNumber < 1 || cupNumber > NUM_CUPS) return;

    int startIdx = CUP_START_INDEX[cupNumber];
    int count = CUP_LED_COUNT[cupNumber];

    for (int i = startIdx; i < startIdx + count; i++) {
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
