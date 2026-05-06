// la_quiniela_poc.ino - DDM La Quiniela Phase 1 POC
//
// Single HX711 load cell + single WS2812B 8x8 matrix.
// Reads weight every SAMPLE_INTERVAL_MS, ships {"scale","weight","delta"}
// to DevPi over WebSocket, and renders {"cup","show":"<n>|X"} commands
// from DevPi onto the matrix.
//
// Required libraries (Arduino Library Manager names):
//   - HX711           by Bogdan Necula  (bogde/HX711)
//   - FastLED         by Daniel Garcia
//   - WebSockets      by Markus Sattler (Links2004/arduinoWebSockets)
//   - ArduinoJson     by Benoit Blanchon (v6 or v7)
//
// Board: ESP32 Dev Module (or any ESP32-WROOM-32 variant).

#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <FastLED.h>
#include <HX711.h>

#include "config.h"

// ---------- globals ----------
HX711 scale;
WebSocketsClient wsClient;

CRGB matrix[MATRIX_LED_COUNT];

float lastWeight = 0.0f;
unsigned long lastSampleMs = 0;

char currentText[4] = "";   // "" / "X" / "1".."20"
bool matrixDirty = true;

// ---------- 3x5 font for digits 0-9 and 'X' ----------
// Each glyph is 5 rows; low 3 bits of each byte = pixel row (MSB = left).
static const uint8_t FONT_3x5[11][5] = {
    {0b111, 0b101, 0b101, 0b101, 0b111}, // 0
    {0b010, 0b110, 0b010, 0b010, 0b111}, // 1
    {0b111, 0b001, 0b111, 0b100, 0b111}, // 2
    {0b111, 0b001, 0b111, 0b001, 0b111}, // 3
    {0b101, 0b101, 0b111, 0b001, 0b001}, // 4
    {0b111, 0b100, 0b111, 0b001, 0b111}, // 5
    {0b111, 0b100, 0b111, 0b101, 0b111}, // 6
    {0b111, 0b001, 0b010, 0b100, 0b100}, // 7
    {0b111, 0b101, 0b111, 0b101, 0b111}, // 8
    {0b111, 0b101, 0b111, 0b001, 0b111}, // 9
    {0b101, 0b101, 0b010, 0b101, 0b101}, // X (index 10)
};

static inline uint16_t xyToIndex(uint8_t x, uint8_t y) {
    if (MATRIX_FLIP_Y) y = (MATRIX_HEIGHT - 1) - y;
    if (MATRIX_SERPENTINE && (y & 1)) {
        return (uint16_t)y * MATRIX_WIDTH + (MATRIX_WIDTH - 1 - x);
    }
    return (uint16_t)y * MATRIX_WIDTH + x;
}

static inline uint8_t glyphIndex(char c) {
    if (c >= '0' && c <= '9') return (uint8_t)(c - '0');
    if (c == 'X' || c == 'x') return 10;
    return 255; // not renderable
}

void drawGlyph(uint8_t originX, uint8_t originY, uint8_t idx, const CRGB& color) {
    if (idx > 10) return;
    for (uint8_t row = 0; row < 5; row++) {
        uint8_t bits = FONT_3x5[idx][row];
        for (uint8_t col = 0; col < 3; col++) {
            if (bits & (1 << (2 - col))) {
                uint8_t x = originX + col;
                uint8_t y = originY + row;
                if (x < MATRIX_WIDTH && y < MATRIX_HEIGHT) {
                    matrix[xyToIndex(x, y)] = color;
                }
            }
        }
    }
}

void renderText(const char* s) {
    fill_solid(matrix, MATRIX_LED_COUNT, CRGB::Black);
    CRGB color = CRGB(MATRIX_COLOR_R, MATRIX_COLOR_G, MATRIX_COLOR_B);

    if (s == nullptr || s[0] == '\0') {
        FastLED.show();
        return;
    }

    size_t len = strlen(s);
    if (len == 1) {
        uint8_t idx = glyphIndex(s[0]);
        if (idx != 255) drawGlyph(2, 1, idx, color); // centered: x=2..4, y=1..5
    } else if (len == 2) {
        uint8_t a = glyphIndex(s[0]);
        uint8_t b = glyphIndex(s[1]);
        if (a != 255) drawGlyph(0, 1, a, color); // x=0..2
        if (b != 255) drawGlyph(4, 1, b, color); // x=4..6, 1px gap at x=3
    }
    FastLED.show();
}

// ---------- WebSocket handling ----------
void sendWeight(float weight, float delta) {
    StaticJsonDocument<128> doc;
    doc["scale"]  = SCALE_ID;
    doc["weight"] = weight;
    doc["delta"]  = delta;
    char buf[128];
    size_t n = serializeJson(doc, buf, sizeof(buf));
    wsClient.sendTXT(buf, n);
}

void handleDisplayCommand(const char* payload, size_t len) {
    StaticJsonDocument<128> doc;
    DeserializationError err = deserializeJson(doc, payload, len);
    if (err) {
        Serial.print(F("[WS] JSON parse error: "));
        Serial.println(err.c_str());
        return;
    }
    int cup = doc["cup"] | -1;
    const char* show = doc["show"] | "";
    if (cup != SCALE_ID) return;          // not for us
    strncpy(currentText, show, sizeof(currentText) - 1);
    currentText[sizeof(currentText) - 1] = '\0';
    matrixDirty = true;
    Serial.print(F("[WS] display -> "));
    Serial.println(currentText);
}

void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED:
            Serial.println(F("[WS] connected"));
            break;
        case WStype_DISCONNECTED:
            Serial.println(F("[WS] disconnected"));
            break;
        case WStype_TEXT:
            handleDisplayCommand((const char*)payload, length);
            break;
        default:
            break;
    }
}

// ---------- WiFi ----------
void connectWifi() {
    Serial.print(F("[WiFi] connecting to "));
    Serial.println(WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
        delay(250);
        Serial.print('.');
    }
    Serial.println();
    if (WiFi.status() == WL_CONNECTED) {
        Serial.print(F("[WiFi] IP: "));
        Serial.println(WiFi.localIP());
    } else {
        Serial.println(F("[WiFi] failed; will keep retrying in background"));
    }
}

// ---------- setup / loop ----------
void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println(F("\n=== DDM La Quiniela POC ==="));

    pinMode(STATUS_LED_PIN, OUTPUT);
    digitalWrite(STATUS_LED_PIN, LOW);

    // Matrix
    FastLED.addLeds<WS2812B, MATRIX_PIN, GRB>(matrix, MATRIX_LED_COUNT);
    FastLED.setBrightness(MATRIX_BRIGHTNESS);
    fill_solid(matrix, MATRIX_LED_COUNT, CRGB::Black);
    FastLED.show();

    // HX711
    scale.begin(HX711_DOUT_PIN, HX711_SCK_PIN);
    Serial.println(F("[HX711] waiting for ready..."));
    unsigned long t0 = millis();
    while (!scale.is_ready() && millis() - t0 < 3000) delay(50);
    if (!scale.is_ready()) {
        Serial.println(F("[HX711] WARN: not ready, continuing anyway"));
    }
    scale.set_scale(HX711_CAL_FACTOR);
    scale.tare();
    Serial.print(F("[HX711] tared. cal_factor="));
    Serial.println(HX711_CAL_FACTOR);

    // WiFi + WebSocket
    connectWifi();
    wsClient.begin(DEVPI_HOST, DEVPI_PORT, DEVPI_WS_PATH);
    wsClient.onEvent(onWsEvent);
    wsClient.setReconnectInterval(WS_RECONNECT_MS);

    Serial.println(F("[setup] done"));
}

void loop() {
    wsClient.loop();

    unsigned long now = millis();
    if (now - lastSampleMs >= SAMPLE_INTERVAL_MS) {
        lastSampleMs = now;

        if (scale.is_ready()) {
            float w = scale.get_units(HX711_SAMPLES);
            float delta = w - lastWeight;
            lastWeight = w;

            Serial.printf("[scale %d] weight=%.2fg delta=%+.2fg\n",
                          SCALE_ID, w, delta);

            if (WiFi.status() == WL_CONNECTED) {
                sendWeight(w, delta);
                digitalWrite(STATUS_LED_PIN, HIGH);
            } else {
                digitalWrite(STATUS_LED_PIN, LOW);
            }
        } else {
            Serial.println(F("[HX711] not ready"));
        }
    }

    if (matrixDirty) {
        renderText(currentText);
        matrixDirty = false;
    }
}
