// ddm_led_controller.ino - DDM Cup LED Controller with ANIMATIONS
// ESP32 WROOM-32 with FastLED - WiFi, Socket Server, Full Animation Suite
// Version 3.1 - Complete Animation System

#include <WiFi.h>
#include <FastLED.h>

// ===== CONFIGURATION =====
#define WIFI_SSID "BMP_WIFI_MAIN"
#define WIFI_PASSWORD "Derby1961"

#define SOCKET_PORT 5005
#define LED_PIN 18
#define LED_COUNT 640
#define STATUS_LED_PIN 2
#define NUM_CUPS 20
#define LEDS_PER_CUP 32

// ===== DDM COLORS =====
const CRGB COLOR_GOLD = CRGB(255, 215, 0);
const CRGB COLOR_GREEN = CRGB(34, 139, 34);
const CRGB COLOR_AMBER = CRGB(255, 165, 0);
const CRGB COLOR_RED = CRGB(220, 20, 60);
const CRGB COLOR_SILVER = CRGB(192, 192, 192);
const CRGB COLOR_BRONZE = CRGB(205, 127, 50);
const CRGB COLOR_WHITE = CRGB(255, 255, 255);
const CRGB COLOR_BLACK = CRGB(0, 0, 0);

// ===== GLOBAL VARIABLES =====
CRGB leds[LED_COUNT];
WiFiServer server(SOCKET_PORT);
String currentIP = "";
uint8_t currentBrightness = 128;

// Animation state
bool animationRunning = false;
String currentAnimation = "";
unsigned long animStartTime = 0;
int animStep = 0;

// Results storage for RESULTS animation
int winCup = 0;
int placeCup = 0;
int showCup = 0;

// ===== FUNCTION DECLARATIONS =====
void connectWiFi();
String processCommand(String cmd);
CRGB hexToRGB(String hex);
void setCup(uint8_t cupNumber, CRGB color);
void blinkStatus(int times);
void printBanner();

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
void animHeartbeat();
void animResults();

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
    
    // Initialize LED strip
    Serial.println("[LED] Initializing 640 LEDs on GPIO 18...");
    FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, LED_COUNT);
    FastLED.setBrightness(currentBrightness);
    FastLED.clear();
    FastLED.show();
    Serial.println("[LED] LED strip initialized");
    
    // Connect to WiFi
    connectWiFi();
    
    // Start socket server
    server.begin();
    Serial.println("[Socket] Server started on port " + String(SOCKET_PORT));
    Serial.println("[Socket] Ready to receive commands");
    Serial.println("==================================================\n");    
    // Startup blink
    blinkStatus(3);
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
            }
        }
        client.stop();
    }
    
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
        fill_solid(leds, LED_COUNT, CRGB::White);
        FastLED.show();
        return "OK:ALL_ON";
    }
    
    // LED:ALL_OFF - Turn all LEDs off
    else if (cmd == "LED:ALL_OFF") {
        stopAnimation();
        FastLED.clear();
        FastLED.show();
        return "OK:ALL_OFF";
    }
    
    // LED:BRIGHTNESS:XX - Set brightness (0-100)
    else if (cmd.startsWith("LED:BRIGHTNESS:")) {
        int brightness = cmd.substring(15).toInt();
        brightness = constrain(brightness, 0, 100);
        currentBrightness = map(brightness, 0, 100, 0, 255);
        FastLED.setBrightness(currentBrightness);
        FastLED.show();
        return "OK:BRIGHTNESS:" + String(brightness);
    }
    
    // LED:COLOR:RRGGBB - Set all LEDs to hex color
    else if (cmd.startsWith("LED:COLOR:")) {
        stopAnimation();
        String hexColor = cmd.substring(10);
        CRGB color = hexToRGB(hexColor);
        fill_solid(leds, LED_COUNT, color);
        FastLED.show();
        return "OK:COLOR:" + hexColor;
    }
    
    // LED:CUP:N:RRGGBB - Set specific cup to hex color
    // LED:CUP:N:R,G,B - Set specific cup to RGB color
    else if (cmd.startsWith("LED:CUP:")) {
        stopAnimation();
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
                return "OK:CUP:" + String(cupNum) + ":" + colorStr;
            }
        }
        return "ERROR:INVALID_CUP";
    }
    
    // LED:TEST:R,G,B,BRIGHTNESS - RGB test mode with brightness
    else if (cmd.startsWith("LED:TEST:")) {
        stopAnimation();
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
            
            return "OK:TEST:" + String(r) + "," + String(g) + "," + String(b) + "," + String(brightness);
        }
        return "ERROR:INVALID_TEST_PARAMS";
    }
    
    // ANIM:WELCOME - Gold wave left to right
    else if (cmd == "ANIM:WELCOME") {
        currentAnimation = "WELCOME";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:WELCOME";
    }
    
    // ANIM:TEST - Sequential cup test
    else if (cmd == "ANIM:TEST") {
        currentAnimation = "TEST";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:TEST";
    }
    
    // ANIM:BETTING_60 - Slow green breathing
    else if (cmd == "ANIM:BETTING_60") {
        currentAnimation = "BETTING_60";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:BETTING_60";
    }
    
    // ANIM:BETTING_30 - Center-out amber pulse
    else if (cmd == "ANIM:BETTING_30") {
        currentAnimation = "BETTING_30";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:BETTING_30";
    }
    
    // ANIM:FINAL_CALL - Edges-in red urgency
    else if (cmd == "ANIM:FINAL_CALL") {
        currentAnimation = "FINAL_CALL";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:FINAL_CALL";
    }
    
    // ANIM:RACE_START - Fast white chase
    else if (cmd == "ANIM:RACE_START") {
        currentAnimation = "RACE_START";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:RACE_START";
    }
    
    // ANIM:CHAOS - Random madness
    else if (cmd == "ANIM:CHAOS") {
        currentAnimation = "CHAOS";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:CHAOS";
    }
    
    // ANIM:FINISH - Checkered flag pattern
    else if (cmd == "ANIM:FINISH") {
        currentAnimation = "FINISH";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:FINISH";
    }
    
    // ANIM:HEARTBEAT - Red breathing
    else if (cmd == "ANIM:HEARTBEAT") {
        currentAnimation = "HEARTBEAT";
        animationRunning = true;
        animStartTime = millis();
        animStep = 0;
        return "OK:ANIM:HEARTBEAT";
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
                currentAnimation = "RESULTS";
                animationRunning = true;
                animStartTime = millis();
                animStep = 0;
                return "OK:ANIM:RESULTS:" + String(winCup) + ":" + String(placeCup) + ":" + String(showCup);
            }
        }
        return "ERROR:INVALID_RESULTS";
    }
    
    // RESET - Clear all and stop animations
    else if (cmd == "RESET") {
        stopAnimation();
        FastLED.clear();
        FastLED.show();
        return "OK:RESET";
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
    } else if (currentAnimation == "HEARTBEAT") {
        animHeartbeat();
    } else if (currentAnimation == "RESULTS") {
        animResults();
    }
}

/**
 * ANIM:WELCOME - Gold wave sweeps left to right
 */
void animWelcome() {
    static unsigned long lastUpdate = 0;
    if (millis() - lastUpdate < 80) return; // ~80ms per cup
    lastUpdate = millis();
    
    if (animStep < NUM_CUPS) {
        setCup(animStep + 1, COLOR_GOLD);
        FastLED.show();
        animStep++;
    } else {
        // Wave complete, hold for a moment then clear
        if (millis() - animStartTime > 3000) {
            FastLED.clear();
            FastLED.show();
            stopAnimation();
        }
    }
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
        animStep++;
    } else {
        FastLED.clear();
        FastLED.show();
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
    
    // Continuous animation
}

/**
 * ANIM:RACE_START - Quick white streak chasing 1→20 repeatedly
 */
void animRaceStart() {
    static unsigned long lastUpdate = 0;
    if (millis() - lastUpdate < 50) return; // Fast chase
    lastUpdate = millis();
    
    FastLED.clear();
    
    // Show streak of 3 cups
    for (int i = 0; i < 3; i++) {
        int cup = ((animStep - i) % NUM_CUPS) + 1;
        if (cup >= 1 && cup <= 20) {
            uint8_t brightness = 255 - (i * 85); // Fade trail
            CRGB color = COLOR_WHITE;
            color.nscale8(brightness);
            setCup(cup, color);
        }
    }
    
    FastLED.show();
    animStep = (animStep + 1) % NUM_CUPS;
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
}

/**
 * ANIM:FINISH - Green checkered flag pattern (celebration!)
 */
void animFinish() {
    static unsigned long lastUpdate = 0;
    if (millis() - lastUpdate < 500) return; // Alternate every 500ms
    lastUpdate = millis();
    
    FastLED.clear();
    
    for (int cup = 1; cup <= NUM_CUPS; cup++) {
        if (animStep % 2 == 0) {
            // Even step: odd cups green, even cups black
            if (cup % 2 == 1) {
                setCup(cup, COLOR_GREEN);
            }
        } else {
            // Odd step: even cups green, odd cups black
            if (cup % 2 == 0) {
                setCup(cup, COLOR_GREEN);
            }
        }
    }
    
    FastLED.show();
    animStep++;
}

/**
 * ANIM:HEARTBEAT - Slow red throb (all cups breathe together)
 */
void animHeartbeat() {
    unsigned long elapsed = millis() - animStartTime;
    
    // Double pulse pattern (heartbeat)
    float beat = 0.0;
    float phase = fmod(elapsed / 1000.0, 2.0); // 2-second cycle
    
    if (phase < 0.3) {
        beat = sin(phase * 10.47) * 0.5 + 0.5; // First beat
    } else if (phase < 0.6) {
        beat = sin((phase - 0.3) * 10.47) * 0.3 + 0.3; // Second beat (softer)
    } else {
        beat = 0.1; // Rest
    }
    
    uint8_t brightness = 25 + (beat * 230);
    CRGB color = COLOR_RED;
    color.nscale8(brightness);
    
    fill_solid(leds, LED_COUNT, color);
    FastLED.show();
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
    // Static display - stays until changed
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
