/*
 * DDM 8x8 Panel Prototype v4 — bench evaluation sketch
 *
 * Drives 1 or 2 chained WS2812B 8x8 ECO panels for visual evaluation
 * of horse-number readability and the WPS podium reveal animation.
 *
 * v3: Beefier 6x8 font with 2-pixel-thick strokes. Single digits fill
 *     the full panel height; two-digit numbers scroll right-to-left.
 *
 * Hardware: NodeMCU-32S (or any ESP32), WS2812B 8x8 ECO panels
 * Library:  FastLED 3.x
 *
 * Wiring (per panel):
 *   VCC (RED)   → 5V external supply
 *   GND (WHITE) → ESP32 GND + supply GND  (common ground REQUIRED)
 *   DIN (GREEN) → ESP32 GPIO18 via ~330Ω resistor inline
 *   DOUT        → DIN of next panel (daisy chain)
 *
 * Demo cycle (auto):
 *   1. Numbers 1–20  (single static, double scrolling)
 *   2. Color palette on #5
 *   3. Brightness sweep on #7
 *   4. Scratch X
 *   5. Winner gold sparkle
 *   6. Dual-panel: static + scrolling side-by-side
 *   7. PODIUM — gold/silver/bronze sparkle backgrounds with horse numbers
 *
 * At boot: orientation check — RED top-left, BLUE top-right, GREEN bottom-left.
 */

#include <FastLED.h>

// ============================================================
// CONFIG
// ============================================================
#define LED_PIN          18
#define NUM_PANELS        2
#define LEDS_PER_PANEL   64
#define NUM_LEDS         (NUM_PANELS * LEDS_PER_PANEL)
#define BRIGHTNESS       60
#define FLIP_VERTICAL    false

#define DIGIT_Y          0          // 6x8 fills full height
#define DIGIT_X_SINGLE   1          // center 6-wide digit on 8-wide panel
#define DIGIT_W          6
#define DIGIT_GAP        0          // tight: digits flush against each other
#define TWO_DIGIT_W      (DIGIT_W * 2 + DIGIT_GAP)   // = 12

CRGB leds[NUM_LEDS];

// ============================================================
// DDM palette
// ============================================================
const CRGB DDM_GREEN     = CRGB(0x3F, 0x8E, 0x43);
const CRGB DDM_YELLOW    = CRGB(0xFE, 0xC6, 0x00);
const CRGB DDM_RED       = CRGB(0xDE, 0x43, 0x43);
const CRGB DDM_WHITE     = CRGB(0xFF, 0xFF, 0xFF);
const CRGB DDM_ORANGE    = CRGB(0xFF, 0x6A, 0x13);
const CRGB DDM_TURQUOISE = CRGB(0x00, 0xB1, 0xB7);
const CRGB DDM_PINK      = CRGB(0xF5, 0x54, 0x7C);
const CRGB DDM_GOLD      = CRGB(0xFF, 0xAF, 0x00);

// Podium tones (base = dimmed bed color, sparkle = bright accent)
const CRGB GOLD_BASE      = CRGB(0xC8, 0x80, 0x00);
const CRGB GOLD_SPARKLE   = CRGB(0xFF, 0xE8, 0x80);
const CRGB SILVER_BASE    = CRGB(0x80, 0x80, 0x90);
const CRGB SILVER_SPARKLE = CRGB(0xFF, 0xFF, 0xFF);
const CRGB BRONZE_BASE    = CRGB(0x80, 0x40, 0x10);
const CRGB BRONZE_SPARKLE = CRGB(0xFF, 0xA0, 0x40);

const CRGB palette[] = {
  DDM_GREEN, DDM_YELLOW, DDM_RED, DDM_WHITE,
  DDM_ORANGE, DDM_TURQUOISE, DDM_PINK, DDM_GOLD
};
const char* paletteNames[] = {
  "Green", "Yellow", "Red", "White",
  "Orange", "Turquoise", "Pink", "Gold"
};
const int PALETTE_SIZE = 8;

// ============================================================
// 6x8 digit font — 2-pixel-thick strokes, fills full panel height
// Each row uses the low 6 bits, MSB = leftmost pixel.
// ============================================================
const uint8_t font6x8[10][8] = {
  // 0
  {0b011110, 0b110011, 0b110011, 0b110011, 0b110011, 0b110011, 0b110011, 0b011110},
  // 1
  {0b001100, 0b011100, 0b111100, 0b001100, 0b001100, 0b001100, 0b001100, 0b111111},
  // 2
  {0b011110, 0b110011, 0b000011, 0b000110, 0b001100, 0b011000, 0b110000, 0b111111},
  // 3
  {0b011110, 0b110011, 0b000011, 0b001111, 0b000011, 0b000011, 0b110011, 0b011110},
  // 4
  {0b110011, 0b110011, 0b110011, 0b110011, 0b111111, 0b000011, 0b000011, 0b000011},
  // 5
  {0b111111, 0b110000, 0b110000, 0b111110, 0b000011, 0b000011, 0b110011, 0b011110},
  // 6
  {0b011110, 0b110000, 0b110000, 0b111110, 0b110011, 0b110011, 0b110011, 0b011110},
  // 7
  {0b111111, 0b000011, 0b000011, 0b000110, 0b001100, 0b011000, 0b011000, 0b011000},
  // 8
  {0b011110, 0b110011, 0b110011, 0b011110, 0b110011, 0b110011, 0b110011, 0b011110},
  // 9
  {0b011110, 0b110011, 0b110011, 0b110011, 0b011111, 0b000011, 0b000011, 0b011110},
};

// ============================================================
// Coordinate mapping — serpentine, even rows right-to-left
// ============================================================
int xyToIndex(int x, int y, int panel) {
  if (x < 0 || x > 7 || y < 0 || y > 7) return -1;
  int yEff = FLIP_VERTICAL ? (7 - y) : y;
  int idx = (yEff % 2 == 0) ? (yEff * 8 + (7 - x)) : (yEff * 8 + x);
  return panel * LEDS_PER_PANEL + idx;
}

void clearPanel(int p) {
  for (int i = 0; i < LEDS_PER_PANEL; i++) leds[p * LEDS_PER_PANEL + i] = CRGB::Black;
}
void clearAll() { for (int i = 0; i < NUM_LEDS; i++) leds[i] = CRGB::Black; }

// ============================================================
// Drawing primitives
// ============================================================
void drawDigit6x8(int digit, int xOff, int yOff, int panel, CRGB color) {
  if (digit < 0 || digit > 9) return;
  for (int row = 0; row < 8; row++) {
    uint8_t bits = font6x8[digit][row];
    for (int col = 0; col < 6; col++) {
      if (bits & (1 << (5 - col))) {
        int idx = xyToIndex(xOff + col, yOff + row, panel);
        if (idx >= 0) leds[idx] = color;
      }
    }
  }
}

void drawNumberAt(int n, int xOff, int yOff, int panel, CRGB color) {
  if (n < 10) {
    drawDigit6x8(n, xOff, yOff, panel, color);
  } else {
    drawDigit6x8(n / 10, xOff, yOff, panel, color);
    drawDigit6x8(n % 10, xOff + DIGIT_W + DIGIT_GAP, yOff, panel, color);
  }
}

void drawNumberStatic(int n, int panel, CRGB color) {
  clearPanel(panel);
  drawNumberAt(n, DIGIT_X_SINGLE, DIGIT_Y, panel, color);
}

void drawX(int panel, CRGB color) {
  clearPanel(panel);
  for (int i = 0; i < 8; i++) {
    int a = xyToIndex(i, i, panel);
    int b = xyToIndex(7 - i, i, panel);
    if (a >= 0) leds[a] = color;
    if (b >= 0) leds[b] = color;
  }
}

void sparkleBackground(int panel, CRGB baseColor, CRGB sparkleColor) {
  CRGB dim = baseColor;
  dim.nscale8_video(70);
  for (int i = 0; i < LEDS_PER_PANEL; i++) leds[panel * LEDS_PER_PANEL + i] = dim;
  for (int s = 0; s < 7; s++) {
    int idx = xyToIndex(random(8), random(8), panel);
    if (idx >= 0) leds[idx] = sparkleColor;
  }
}

// ============================================================
// DEMOS
// ============================================================
void demoNumbers() {
  for (int n = 1; n <= 20; n++) {
    Serial.printf("Horse #%d\n", n);
    if (n < 10) {
      for (int p = 0; p < NUM_PANELS; p++) drawNumberStatic(n, p, DDM_GREEN);
      FastLED.show();
      delay(1000);
    } else {
      for (int xOff = 8; xOff >= -TWO_DIGIT_W; xOff--) {
        for (int p = 0; p < NUM_PANELS; p++) {
          clearPanel(p);
          drawNumberAt(n, xOff, DIGIT_Y, p, DDM_GREEN);
        }
        FastLED.show();
        delay(80);
      }
    }
  }
}

void demoColors() {
  for (int c = 0; c < PALETTE_SIZE; c++) {
    for (int p = 0; p < NUM_PANELS; p++) drawNumberStatic(5, p, palette[c]);
    FastLED.show();
    Serial.printf("Color: %s\n", paletteNames[c]);
    delay(900);
  }
}

void demoBrightness() {
  for (int p = 0; p < NUM_PANELS; p++) drawNumberStatic(7, p, DDM_WHITE);
  for (int b = 10; b <= 200; b += 5) { FastLED.setBrightness(b); FastLED.show(); delay(35); }
  for (int b = 200; b >= 10; b -= 5) { FastLED.setBrightness(b); FastLED.show(); delay(35); }
  FastLED.setBrightness(BRIGHTNESS);
  Serial.println("Brightness sweep done");
}

void demoScratch() {
  for (int p = 0; p < NUM_PANELS; p++) drawX(p, DDM_RED);
  FastLED.show();
  Serial.println("SCRATCH");
  delay(1500);
  for (int i = 0; i < 3; i++) {
    clearAll(); FastLED.show(); delay(140);
    for (int p = 0; p < NUM_PANELS; p++) drawX(p, DDM_RED);
    FastLED.show(); delay(180);
  }
}

void demoWinner() {
  Serial.println("WINNER!");
  unsigned long t0 = millis();
  while (millis() - t0 < 2200) {
    clearAll();
    for (int s = 0; s < 14; s++) {
      int idx = xyToIndex(random(8), random(8), random(NUM_PANELS));
      if (idx >= 0) leds[idx] = DDM_GOLD;
    }
    FastLED.show();
    delay(55);
  }
  for (int p = 0; p < NUM_PANELS; p++) drawNumberStatic(1, p, DDM_GOLD);
  FastLED.show();
  delay(1600);
}

void demoDualPanel() {
  if (NUM_PANELS < 2) { Serial.println("Skip: NUM_PANELS=1"); return; }
  struct Pair { int still; int scroll; };
  Pair pairs[] = { {5, 12}, {8, 17}, {3, 20}, {7, 11} };
  for (Pair& pr : pairs) {
    Serial.printf("P0 static:#%d  P1 scroll:#%d\n", pr.still, pr.scroll);
    for (int xOff = 8; xOff >= -TWO_DIGIT_W; xOff--) {
      drawNumberStatic(pr.still, 0, DDM_GREEN);
      clearPanel(1);
      drawNumberAt(pr.scroll, xOff, DIGIT_Y, 1, DDM_YELLOW);
      FastLED.show();
      delay(80);
    }
  }
}

void demoPodium() {
  struct Medal { CRGB base; CRGB sparkle; int number; const char* name; };
  Medal medals[] = {
    { GOLD_BASE,   GOLD_SPARKLE,   5,  "GOLD"   },
    { SILVER_BASE, SILVER_SPARKLE, 12, "SILVER" },
    { BRONZE_BASE, BRONZE_SPARKLE, 17, "BRONZE" }
  };

  for (Medal& m : medals) {
    Serial.printf("Podium: %s #%d\n", m.name, m.number);
    if (m.number < 10) {
      unsigned long t0 = millis();
      while (millis() - t0 < 3000) {
        for (int p = 0; p < NUM_PANELS; p++) {
          sparkleBackground(p, m.base, m.sparkle);
          drawNumberAt(m.number, DIGIT_X_SINGLE, DIGIT_Y, p, DDM_WHITE);
        }
        FastLED.show();
        delay(100);
      }
    } else {
      for (int xOff = 8; xOff >= -TWO_DIGIT_W; xOff--) {
        for (int p = 0; p < NUM_PANELS; p++) {
          sparkleBackground(p, m.base, m.sparkle);
          drawNumberAt(m.number, xOff, DIGIT_Y, p, DDM_WHITE);
        }
        FastLED.show();
        delay(110);
      }
    }
  }
}

// ============================================================
// Setup / loop
// ============================================================
void orientationCheck() {
  clearAll();
  for (int p = 0; p < NUM_PANELS; p++) {
    int a = xyToIndex(0, 0, p); if (a >= 0) leds[a] = CRGB::Red;
    int b = xyToIndex(7, 0, p); if (b >= 0) leds[b] = CRGB::Blue;
    int c = xyToIndex(0, 7, p); if (c >= 0) leds[c] = CRGB::Green;
  }
  FastLED.show();
  Serial.println("Orientation: RED=top-left, BLUE=top-right, GREEN=bottom-left");
  delay(2500);
}

void setup() {
  Serial.begin(115200);
  delay(400);
  Serial.println("\n=== DDM 8x8 Panel Prototype v4 ===");
  Serial.printf("Panels:%d  LEDs:%d  Pin:GPIO%d  Bright:%d  Font:6x8\n",
                NUM_PANELS, NUM_LEDS, LED_PIN, BRIGHTNESS);

  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, NUM_LEDS);
  FastLED.setBrightness(BRIGHTNESS);
  FastLED.clear();
  FastLED.show();

  for (CRGB c : {CRGB::Red, CRGB::Green, CRGB::Blue}) {
    fill_solid(leds, NUM_LEDS, c);
    FastLED.show();
    delay(280);
  }
  orientationCheck();
  clearAll();
  FastLED.show();
  delay(400);
}

void loop() {
  Serial.println("\n--- 1: Horse numbers 1-20 ---");   demoNumbers();    delay(500);
  Serial.println("\n--- 2: Palette on #5 ---");         demoColors();     delay(500);
  Serial.println("\n--- 3: Brightness sweep #7 ---");   demoBrightness(); delay(500);
  Serial.println("\n--- 4: Scratch X ---");             demoScratch();    delay(500);
  Serial.println("\n--- 5: Winner celebration ---");    demoWinner();     delay(500);
  Serial.println("\n--- 6: Dual-panel modes ---");      demoDualPanel();  delay(500);
  Serial.println("\n--- 7: Podium W/P/S ---");          demoPodium();     delay(1000);
}
