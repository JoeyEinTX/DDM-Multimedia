// config.h - Configuration settings for DDM Cup LED Controller (ESP32)

#ifndef CONFIG_H
#define CONFIG_H

// ===== WiFi Configuration =====
#define WIFI_SSID "YOUR_WIFI_SSID"          // Replace with your WiFi network name
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"  // Replace with your WiFi password

// ===== Socket Server Settings =====
#define SOCKET_PORT 5005                     // Port for receiving commands from Pi5
#define SOCKET_TIMEOUT 1000                  // Socket timeout in milliseconds

// ===== LED Configuration =====
#define LED_PIN 18                           // GPIO pin for LED data output
#define LED_COUNT 640                        // Total LEDs (32 per cup × 20 cups)
#define LEDS_PER_CUP 32                      // LEDs in each cup base
#define NUM_CUPS 20                          // Total number of cups

// ===== FastLED Settings =====
#define LED_TYPE WS2812B                     // LED strip type
#define COLOR_ORDER GRB                      // Color order for WS2812B
#define DEFAULT_BRIGHTNESS 128               // Default brightness (0-255)
#define MAX_BRIGHTNESS 255                   // Maximum allowed brightness

// ===== OLED Display Configuration =====
#define OLED_ENABLED true                    // Set to false to disable OLED
#define OLED_SDA_PIN 21                      // I2C SDA pin (GPIO 21)
#define OLED_SCL_PIN 22                      // I2C SCL pin (GPIO 22)
#define OLED_WIDTH 128                       // OLED display width in pixels
#define OLED_HEIGHT 64                       // OLED display height (64 or 32)
#define OLED_ADDRESS 0x3C                    // I2C address (usually 0x3C or 0x3D)

// ===== Built-in LED =====
#define STATUS_LED_PIN 2                     // Built-in LED on GPIO 2

// ===== Animation Settings =====
#define ANIMATION_FPS 60                     // Target frames per second
#define ANIMATION_DELAY (1000 / ANIMATION_FPS)  // Delay between frames in ms

// ===== DDM Color Palette (RGB) =====
struct Color {
    uint8_t r, g, b;
};

const Color DDM_GREEN  = {34, 139, 34};      // #228B22 - Derby forest green
const Color DDM_GOLD   = {255, 215, 0};      // #FFD700 - Golden yellow
const Color DDM_ROSE   = {220, 20, 60};      // #DC143C - Deep rose/red
const Color DDM_WHITE  = {255, 255, 255};    // #FFFFFF - Clean white
const Color DDM_BLACK  = {0, 0, 0};          // #000000 - Off
const Color DDM_SILVER = {192, 192, 192};    // #C0C0C0 - Place
const Color DDM_BRONZE = {205, 127, 50};     // #CD7F32 - Show

#endif // CONFIG_H
