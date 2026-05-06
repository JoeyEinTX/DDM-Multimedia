// config.h - DDM La Quiniela POC (single scale + single 8x8 matrix)
//
// Phase 1 hardware: ONE bar load cell via HX711, ONE WS2812B 8x8 matrix.

#ifndef CONFIG_H
#define CONFIG_H

// ===== WiFi =====
#define WIFI_SSID       "YOUR_WIFI_SSID"
#define WIFI_PASSWORD   "YOUR_WIFI_PASSWORD"

// ===== DevPi WebSocket endpoint =====
#define DEVPI_HOST      "10.0.0.87"
#define DEVPI_PORT      5000
#define DEVPI_WS_PATH   "/ws/la-quiniela"
#define WS_RECONNECT_MS 5000

// ===== Identity (POC = single cup) =====
#define SCALE_ID        1   // matches cup number for POC

// ===== HX711 load cell =====
#define HX711_DOUT_PIN  16
#define HX711_SCK_PIN   17

// Calibration factor: divides raw reading to produce grams.
// Run a calibration sketch with a known weight and replace this value.
// Placeholder — POC will need to be calibrated before bet detection works.
#define HX711_CAL_FACTOR  420.0f

// Number of samples averaged per reading (HX711 lib does the averaging).
#define HX711_SAMPLES     3

// Sampling cadence
#define SAMPLE_INTERVAL_MS  200

// Ticket detection — minimum delta to consider a real change (grams).
// Per spec, each ticket half is ~2-2.5g.
#define TICKET_WEIGHT_G     2.5f

// ===== WS2812B 8x8 matrix =====
#define MATRIX_PIN        15
#define MATRIX_WIDTH      8
#define MATRIX_HEIGHT     8
#define MATRIX_LED_COUNT  (MATRIX_WIDTH * MATRIX_HEIGHT)

// Most 8x8 panels use a serpentine layout (row 0 L->R, row 1 R->L, ...).
// Flip these if your panel is wired differently.
#define MATRIX_SERPENTINE 1
#define MATRIX_FLIP_Y     0   // set to 1 if (0,0) is bottom-left on your panel

#define MATRIX_BRIGHTNESS 64  // 0-255; 64 is plenty for indoor visibility

// Default render color (DDM gold)
#define MATRIX_COLOR_R 255
#define MATRIX_COLOR_G 215
#define MATRIX_COLOR_B 0

// ===== Status LED =====
#define STATUS_LED_PIN 2

#endif // CONFIG_H
