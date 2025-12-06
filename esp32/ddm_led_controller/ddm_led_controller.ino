// ddm_led_controller.ino - DDM Cup LED Controller STARTER CODE
// ESP32 WROOM-32 with FastLED - Basic WiFi, Socket Server, Simple LED Control
// Version 3.0 - Starter (Full animations to be added later)

#include <WiFi.h>
#include <FastLED.h>

// ===== CONFIGURATION =====
#define WIFI_SSID "BMP_WIFI_MAIN"
#define WIFI_PASSWORD "Derby1961"

#define SOCKET_PORT 5005
#define LED_PIN 18
#define LED_COUNT 640
#define STATUS_LED_PIN 2

// ===== GLOBAL VARIABLES =====
CRGB leds[LED_COUNT];
WiFiServer server(SOCKET_PORT);
String currentIP = "";
uint8_t currentBrightness = 128;

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
    Serial.println("="*50 + "\n");
    
    // Startup blink
    blinkStatus(3);
}

/**
 * MAIN LOOP - Runs continuously
 */
void loop() {
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
        fill_solid(leds, LED_COUNT, CRGB::White);
        FastLED.show();
        return "OK:ALL_ON";
    }
    
    // LED:ALL_OFF - Turn all LEDs off
    else if (cmd == "LED:ALL_OFF") {
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
        String hexColor = cmd.substring(10);
        CRGB color = hexToRGB(hexColor);
        fill_solid(leds, LED_COUNT, color);
        FastLED.show();
        return "OK:COLOR:" + hexColor;
    }
    
    // LED:CUP:N:RRGGBB - Set specific cup to color
    else if (cmd.startsWith("LED:CUP:")) {
        // Parse: LED:CUP:5:FFD700
        int firstColon = cmd.indexOf(':', 8);
        if (firstColon > 0) {
            int cupNum = cmd.substring(8, firstColon).toInt();
            String hexColor = cmd.substring(firstColon + 1);
            
            if (cupNum >= 1 && cupNum <= 20) {
                CRGB color = hexToRGB(hexColor);
                setCup(cupNum, color);
                FastLED.show();
                return "OK:CUP:" + String(cupNum) + ":" + hexColor;
            }
        }
        return "ERROR:INVALID_CUP";
    }
    
    // RESET - Clear all
    else if (cmd == "RESET") {
        FastLED.clear();
        FastLED.show();
        return "OK:RESET";
    }
    
    return "ERROR:UNKNOWN_COMMAND";
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
    Serial.println("   DDM Cup LED Controller - ESP32 STARTER");
    Serial.println("   Version 3.0 - Basic Commands Only");
    Serial.println("==================================================\n");
}
