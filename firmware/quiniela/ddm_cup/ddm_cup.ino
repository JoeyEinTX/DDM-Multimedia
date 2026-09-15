/*
 * ddm_cup.ino — La Quiniela betting cup (ESP-NOW bench test)
 *
 * ESP32-2432S028R ("Cheap Yellow Display") as the horse-number display for
 * the DDM Live Betting System betting cups (DDM 2027).
 *
 * Derived from ddm_cyd_prototype.ino. The display pipeline — traced-outline
 * glyph fill, blend565 anti-aliasing, drawNumber condensing, saddle cloth
 * table, pickFont/centerText, backlight LEDC handling, pin defines — is
 * carried over unchanged; it is validated on hardware. Only the mode state
 * machine is replaced: this sketch renders whatever the gateway broadcasts
 * over ESP-NOW instead of walking demo modes.
 *
 * ===========================================================================
 * THREE FILES. All must sit in this folder:
 *     ddm_cup.ino       <- this file
 *     ddm_digits.h      <- traced digit outlines (GENERATED — do not edit)
 *     ddm_common.h      <- symlink to ../ddm_common.h (see ../README.md)
 * ===========================================================================
 *
 * LIBRARIES (Tools -> Manage Libraries):  Adafruit GFX Library,
 * Adafruit ILI9341, HX711 Arduino Library (Bogdan Necula / bogde). Say
 * Install All if it offers dependencies.
 *
 * BOARD:  ESP32 Dev Module
 *
 * UPLOADING:  hold BOOT, click Upload, keep holding until the log names the
 * chip, then let go.
 *
 * CONTROLS — BOOT button:
 *     Short press  ->  toggle the diagnostic overlay (RSSI, drops, seq, age,
 *                      tokens and net scale counts)
 *     Hold 3 s     ->  re-tare the scale: count back to 0, green LED blinks
 *     Hold 6 s     ->  (keep holding past the tare) step the display
 *                      orientation to the next of its four settings and
 *                      save it in NVS for this cup
 *
 * SERIAL (115200):  o = next orientation,  h = mirror left-right,
 *                   v = mirror top-bottom (all saved),  t = tare,  ? = help
 *
 * Until the gateway assigns this cup an ID, the screen shows this board's
 * own MAC address in large text — that is how the four MACs get collected
 * for the gateway's KNOWN_CUPS[] table with no serial cable.
 *
 * SCALE: an HX711 on CN1 (DT GPIO27, SCK GPIO22) weighs the tokens. Counting
 * is by steps against a slow-tracking baseline, never by absolute weight; the
 * two calibration constants in the tunables came from tools/hx711_calibrate/.
 * The cup never shows the count on a normal screen (the splash display does);
 * the diagnostic overlay shows TOKENS and the net counts for bench checks, and
 * telemetry carries rawWeight (reading minus tare) and tokenCount.
 */

#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <HX711.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include "ddm_digits.h"
#include "ddm_common.h"

#include <Fonts/FreeSansBold9pt7b.h>
#include <Fonts/FreeSansBold12pt7b.h>
#include <Fonts/FreeSansBold18pt7b.h>
#include <Fonts/FreeSansBold24pt7b.h>

// ===========================================================================
// Tunables
// ===========================================================================
#define ORIENT_DEFAULT 0xC0     // MADCTL MX|MY bits for a cup with nothing saved (see panelOrientation)
#define INVERT     false        // true if colors come out backwards

#define TRACK       0.06f       // gap between two digits, as a fraction of height
#define CONDENSE    0.72f       // narrowest a digit may be squeezed (1.0 = never)
#define SUBROWS        4        // vertical anti-alias samples per pixel row

// Link + render timing
#define HELLO_MS          1000  // hello cadence until the gateway answers
#define TELEMETRY_MS      2000  // telemetry cadence once assigned
#define LINK_TIMEOUT_MS  10000  // no gateway packet this long -> NO LINK badge
#define SCRATCH_FLASH_MS  1200  // scratched alternation period
#define PODIUM_STEP_MS    2000  // podium metal cycle period (from prototype)
#define OVERLAY_MS         500  // diagnostic overlay refresh

// ---------------------------------------------------------------------------
// Scale — HX711 on CN1. CALIBRATION CONSTANTS: measured on the bench with
// tools/hx711_calibrate/ (ten tokens of the current print dropped one at a
// time; mean step 6212 counts, sd 102 = 1.6%). If the token print changes,
// re-run that tool and paste its two #define lines over these.
// ---------------------------------------------------------------------------
#define COUNTS_PER_TOKEN   6212L    // mean step, 10 tokens, sd 102 (1.6%)
#define TOKEN_THRESHOLD    3106L    // half a token

#define HX711_DT                27  // CN1 data
#define HX711_SCK               22  // CN1 clock
#define SCALE_WARMUP_MS      30000  // HX711 drifts after power-up: no tare, no counting until this has passed
#define SCALE_TARE_SAMPLES      30  // averaged for the tare (~3 s at 10 SPS)
#define SCALE_BASELINE_TAU_MS 5000  // time constant of the slow baseline that absorbs drift and relaxation
#define SCALE_CONFIRM_SAMPLES    3  // consecutive samples past the threshold that make a drop/remove event
#define TARE_HOLD_MS          3000  // BOOT held this long re-tares
#define ORIENT_HOLD_MS        6000  // BOOT kept held this long steps the display orientation (saved)
#define TARE_BLINK_MS          150  // LED acknowledgement of a manual tare

// ---------------------------------------------------------------------------
// CYD hardware pins — correct for the ESP32-2432S028R, leave alone
// ---------------------------------------------------------------------------
#define TFT_MISO  12
#define TFT_MOSI  13
#define TFT_SCLK  14
#define TFT_CS    15
#define TFT_DC     2
#define TFT_RST   -1
#define TFT_BL    21

#define BOOT_BTN   0
#define LED_R      4
#define LED_G     16
#define LED_B     17

Adafruit_ILI9341 tft = Adafruit_ILI9341(TFT_CS, TFT_DC, TFT_RST);

Preferences prefs;                     // NVS: per-cup settings that survive reflashing
uint8_t     orientFlips = ORIENT_DEFAULT;  // MADCTL MX (0x40) | MY (0x80) bits, loaded from NVS at boot

// ---------------------------------------------------------------------------
#define RGB(r,g,b) ((uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3)))

#define C_BLACK   RGB(0x00,0x00,0x00)
#define C_WHITE   RGB(0xFF,0xFF,0xFF)
#define C_RED     RGB(0xE3,0x18,0x37)
#define C_GOLD    RGB(0xFF,0xD7,0x00)
#define C_SILVER  RGB(0xC0,0xC0,0xC8)
#define C_BRONZE  RGB(0xCD,0x7F,0x32)
#define C_DIM     RGB(0x50,0x50,0x50)
#define C_AMBER   RGB(0xD4,0xA0,0x00)

struct Cloth { uint16_t bg; uint16_t fg; };

Cloth cloth(uint8_t n) {
  switch (n) {
    case 1:  return { RGB(0xE3,0x18,0x37), C_WHITE };
    case 2:  return { C_WHITE,             C_BLACK };
    case 3:  return { RGB(0x00,0x33,0xA0), C_WHITE };
    case 4:  return { RGB(0xFF,0xCD,0x00), C_BLACK };
    case 5:  return { RGB(0x00,0x84,0x3D), C_WHITE };
    case 6:  return { C_BLACK,             RGB(0xFF,0xD7,0x00) };
    case 7:  return { RGB(0xFF,0x66,0x00), C_BLACK };
    case 8:  return { RGB(0xFF,0x69,0xB4), C_BLACK };
    case 9:  return { RGB(0x40,0xE0,0xD0), C_BLACK };
    case 10: return { RGB(0x66,0x33,0x99), C_WHITE };
    case 11: return { RGB(0x80,0x80,0x80), RGB(0xE3,0x18,0x37) };
    case 12: return { RGB(0x32,0xCD,0x32), C_BLACK };
    case 13: return { RGB(0x8B,0x45,0x13), C_WHITE };
    case 14: return { RGB(0x80,0x00,0x00), RGB(0xFF,0xCD,0x00) };
    case 15: return { RGB(0xC4,0xB7,0xA6), C_BLACK };
    case 16: return { RGB(0x87,0xCE,0xEB), RGB(0xE3,0x18,0x37) };
    case 17: return { RGB(0x00,0x00,0x80), C_WHITE };
    case 18: return { RGB(0x22,0x8B,0x22), RGB(0xFF,0xCD,0x00) };
    case 19: return { RGB(0x00,0x00,0x8B), RGB(0xE3,0x18,0x37) };
    case 20: return { RGB(0xFF,0x00,0xFF), RGB(0xFF,0xCD,0x00) };
    default: return { RGB(0x80,0x80,0x80), C_WHITE };
  }
}

int SW, SH;

// ===========================================================================
// TEXT — real typefaces at native size, never scaled
// ===========================================================================
const GFXfont* FONT_LADDER[] = {
  &FreeSansBold24pt7b, &FreeSansBold18pt7b,
  &FreeSansBold12pt7b, &FreeSansBold9pt7b
};

const GFXfont* pickFont(const char* s, int maxW) {
  int16_t x1, y1; uint16_t w, h;
  for (int i = 0; i < 4; i++) {
    tft.setFont(FONT_LADDER[i]);
    tft.setTextSize(1);
    tft.getTextBounds(s, 0, 0, &x1, &y1, &w, &h);
    if ((int)w <= maxW) return FONT_LADDER[i];
  }
  return &FreeSansBold9pt7b;
}

void centerText(const char* s, int cx, int cy, int maxW, uint16_t fg) {
  tft.setFont(pickFont(s, maxW));
  tft.setTextSize(1);
  tft.setTextColor(fg);
  int16_t x1, y1; uint16_t w, h;
  tft.getTextBounds(s, 0, 0, &x1, &y1, &w, &h);
  tft.setCursor(cx - w / 2 - x1, cy - h / 2 - y1);
  tft.print(s);
}

// ===========================================================================
// GLYPH FILL
//
// Even-odd scanline fill of the traced outlines, supersampled vertically and
// with fractional horizontal coverage, blended against the background color.
// ===========================================================================
static uint16_t rowBuf[340];
static uint8_t  cov[340];

static inline uint16_t blend565(uint16_t fg, uint16_t bg, uint16_t a) {
  uint16_t fr = (fg >> 11) & 0x1F, fgn = (fg >> 5) & 0x3F, fb = fg & 0x1F;
  uint16_t br = (bg >> 11) & 0x1F, bgn = (bg >> 5) & 0x3F, bb = bg & 0x1F;
  uint16_t r = (fr * a + br * (255 - a)) / 255;
  uint16_t g = (fgn * a + bgn * (255 - a)) / 255;
  uint16_t b = (fb * a + bb * (255 - a)) / 255;
  return (r << 11) | (g << 5) | b;
}

// One glyph, its 0..DIGIT_GRID box mapped to (ox,oy) with scale (sx,sy)
static void fillGlyph(uint8_t d, float ox, float oy, float sx, float sy,
                      uint16_t fg, uint16_t bg) {
  uint16_t first = GLYPH_FIRST[d];
  uint8_t  nc    = GLYPH_NCONT[d];

  int xLo = (int)floorf(ox) - 1;
  int xHi = (int)ceilf(ox + DIGIT_W[d] * sx) + 1;
  int yLo = (int)floorf(oy) - 1;
  int yHi = (int)ceilf(oy + DIGIT_GRID * sy) + 1;

  if (xLo < 0) xLo = 0;
  if (yLo < 0) yLo = 0;
  if (xHi > SW) xHi = SW;
  if (yHi > SH) yHi = SH;
  int w = xHi - xLo;
  if (w <= 0 || w > (int)(sizeof(cov)) ) return;

  const uint8_t step = 255 / SUBROWS;
  float xs[24];

  tft.startWrite();
  for (int y = yLo; y < yHi; y++) {
    memset(cov, 0, w);
    bool any = false;

    for (int s = 0; s < SUBROWS; s++) {
      float yc = y + (s + 0.5f) / SUBROWS;
      int   n  = 0;

      for (uint8_t ci = 0; ci < nc; ci++) {
        uint16_t off = CONTOUR_OFF[first + ci];
        uint16_t len = CONTOUR_LEN[first + ci];
        for (uint16_t i = 0; i < len; i++) {
          uint16_t j = (i + 1 == len) ? 0 : i + 1;
          float ay = oy + DIGIT_PTS[(off + i) * 2 + 1] * sy;
          float by = oy + DIGIT_PTS[(off + j) * 2 + 1] * sy;
          if ((ay <= yc && by > yc) || (by <= yc && ay > yc)) {
            float ax = ox + DIGIT_PTS[(off + i) * 2] * sx;
            float bx = ox + DIGIT_PTS[(off + j) * 2] * sx;
            if (n < 24) xs[n++] = ax + (yc - ay) * (bx - ax) / (by - ay);
          }
        }
      }
      if (n < 2) continue;

      for (int a = 1; a < n; a++) {            // insertion sort
        float v = xs[a]; int b = a - 1;
        while (b >= 0 && xs[b] > v) { xs[b + 1] = xs[b]; b--; }
        xs[b + 1] = v;
      }

      for (int a = 0; a + 1 < n; a += 2) {
        float x1 = xs[a], x2 = xs[a + 1];
        if (x2 <= xLo || x1 >= xHi) continue;
        if (x1 < xLo) x1 = xLo;
        if (x2 > xHi) x2 = xHi;
        int p1 = (int)floorf(x1), p2 = (int)floorf(x2);
        any = true;
        if (p1 == p2) {
          int k = p1 - xLo;
          if (k >= 0 && k < w) cov[k] += (uint8_t)((x2 - x1) * step);
        } else {
          int k = p1 - xLo;
          if (k >= 0 && k < w) cov[k] += (uint8_t)((p1 + 1 - x1) * step);
          for (int p = p1 + 1; p < p2; p++) {
            k = p - xLo;
            if (k >= 0 && k < w) cov[k] += step;
          }
          k = p2 - xLo;
          if (k >= 0 && k < w) cov[k] += (uint8_t)((x2 - p2) * step);
        }
      }
    }

    if (!any) continue;
    for (int i = 0; i < w; i++) {
      uint16_t a = cov[i];
      if (a >= 250) rowBuf[i] = fg;
      else if (a == 0) rowBuf[i] = bg;
      else rowBuf[i] = blend565(fg, bg, a);
    }
    tft.setAddrWindow(xLo, y, w, 1);
    tft.writePixels(rowBuf, w);
  }
  tft.endWrite();
}

// Whole number, centered in the box, condensing rather than shrinking
void drawNumber(uint8_t n, int cx, int cy, int maxW, int maxH,
                uint16_t fg, uint16_t bg) {
  uint8_t d[2];
  int digits;
  if (n >= 10) { d[0] = n / 10; d[1] = n % 10; digits = 2; }
  else         { d[0] = n;                     digits = 1; }

  float gap    = TRACK * DIGIT_GRID;
  float totalW = (digits - 1) * gap;
  for (int i = 0; i < digits; i++) totalW += DIGIT_W[d[i]];

  float sy = (float)maxH / DIGIT_GRID;
  float sx = fminf(sy, (float)maxW / totalW);
  if (sx < sy * CONDENSE) sy = sx / CONDENSE;

  float x = cx - (totalW * sx) * 0.5f;
  float y = cy - (DIGIT_GRID * sy) * 0.5f;

  for (int i = 0; i < digits; i++) {
    fillGlyph(d[i], x, y, sx, sy, fg, bg);
    x += (DIGIT_W[d[i]] + gap) * sx;
  }
}

// ---------------------------------------------------------------------------
void backlightInit() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(TFT_BL, 5000, 8);
#else
  ledcSetup(0, 5000, 8);
  ledcAttachPin(TFT_BL, 0);
#endif
}

void backlight(uint8_t duty) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(TFT_BL, duty);
#else
  ledcWrite(0, duty);
#endif
}

// ===========================================================================
// ESP-NOW link state
// ===========================================================================
static const uint8_t BCAST[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

uint8_t  myMac[6];
uint8_t  gatewayMac[6];
bool     gatewayKnown  = false;
int      myCupId       = -1;      // -1 until the gateway's hello-ack assigns one

DdmStatePacket lastState;
bool     haveState     = false;
uint32_t lastSeq       = 0;
bool     haveSeq       = false;
uint32_t droppedCount  = 0;       // gaps detected in the gateway's seq
int8_t   lastRssi      = 0;       // RSSI of the last gateway packet heard
uint32_t lastPacketAt  = 0;       // millis() of last gateway packet; 0 = never
uint32_t versionRejects = 0;

uint32_t tHello = 0, tTelemetry = 0;

// ---------------------------------------------------------------------------
// Scale state. Everything the HX711 produces lives here; the display never
// reads `tokens` except for the diagnostic overlay.
// ---------------------------------------------------------------------------
HX711 hx711;

enum ScalePhase : uint8_t {
  SCALE_WARMUP,    // first SCALE_WARMUP_MS after boot: sampling, not counting
  SCALE_TARING,    // collecting SCALE_TARE_SAMPLES for the empty reading
  SCALE_RUNNING    // counting
};

bool       scaleOk       = false;    // HX711 answered at boot
ScalePhase scalePhase    = SCALE_WARMUP;
uint32_t   tScaleWarmup  = 0;        // millis() at boot
uint32_t   tScaleSample  = 0;        // millis() of the last sample (baseline time constant)
long       tareSum       = 0;        // tare accumulator
uint8_t    tareN         = 0;
long       tare          = 0;        // raw counts that mean "empty"
long       lastReading   = 0;        // most recent raw sample
float      baseline      = 0;        // slow-tracking raw baseline the detector compares against
uint8_t    confirmN      = 0;        // consecutive samples past TOKEN_THRESHOLD
uint16_t   tokens        = 0;        // what telemetry reports as tokenCount
uint32_t   tLedOff       = 0;        // non-blocking end of the tare-acknowledge blink

// Handoff from the ESP-NOW receive callback (WiFi task) to loop(). The
// callback only copies bytes and sets a flag; all rendering and state
// mutation happens in loop().
volatile bool  rxStatePending = false;
volatile bool  rxAckPending   = false;
DdmStatePacket rxStateBuf;
uint8_t        rxAckCupId     = 0;
uint8_t        rxSrcMac[6];
int8_t         rxRssiVal      = 0;

// ---------------------------------------------------------------------------
// Receive path. Core 3.x hands us esp_now_recv_info_t (with per-packet RSSI);
// core 2.x hands us just the MAC. Same guard pattern as the LEDC handling.
// ---------------------------------------------------------------------------
#if ESP_ARDUINO_VERSION_MAJOR >= 3
void onDataRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
  const uint8_t* src = info->src_addr;
  int8_t rssi = info->rx_ctrl ? info->rx_ctrl->rssi : 0;
#else
void onDataRecv(const uint8_t* src, const uint8_t* data, int len) {
  int8_t rssi = 0;   // core 2.x recv path exposes no RSSI
#endif
  if (len < 2) return;
  if (data[0] != DDM_PROTO_VERSION) { versionRejects++; return; }

  if (data[1] == DDM_MSG_STATE && len == (int)sizeof(DdmStatePacket)) {
    memcpy(&rxStateBuf, data, sizeof(rxStateBuf));
    memcpy((void*)rxSrcMac, src, 6);
    rxRssiVal = rssi;
    rxStatePending = true;

  } else if (data[1] == DDM_MSG_HELLO && len == (int)sizeof(DdmTelemetryPacket)) {
    // Gateway's hello-ack: the DdmTelemetryPacket layout coming back at us
    // with cupId = the ID this cup was assigned (see gateway PROTOCOL NOTE).
    const DdmTelemetryPacket* p = (const DdmTelemetryPacket*)data;
    rxAckCupId = p->cupId;
    memcpy((void*)rxSrcMac, src, 6);
    rxRssiVal = rssi;
    rxAckPending = true;
  }
}

static void ensureGatewayPeer(const uint8_t* mac) {
  if (gatewayKnown && memcmp(gatewayMac, mac, 6) == 0) return;
  memcpy(gatewayMac, mac, 6);
  gatewayKnown = true;
  if (!esp_now_is_peer_exist(mac)) {
    esp_now_peer_info_t p = {};
    memcpy(p.peer_addr, mac, 6);
    p.channel = DDM_ESPNOW_CHANNEL;
    p.ifidx   = WIFI_IF_STA;
    p.encrypt = false;
    esp_now_add_peer(&p);
  }
  Serial.printf("[link] gateway %02X:%02X:%02X:%02X:%02X:%02X registered\n",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

static void sendHello() {
  DdmTelemetryPacket p = {};
  p.version = DDM_PROTO_VERSION;
  p.msgType = DDM_MSG_HELLO;
  p.cupId   = (myCupId < 0) ? 0xFF : (uint8_t)myCupId;  // 0xFF = unassigned;
                                                        // gateway keys on MAC
  p.rssi    = lastRssi;
  esp_now_send(gatewayKnown ? gatewayMac : BCAST, (const uint8_t*)&p, sizeof(p));
}

static void sendTelemetry() {
  DdmTelemetryPacket p = {};
  p.version    = DDM_PROTO_VERSION;
  p.msgType    = DDM_MSG_TELEMETRY;
  p.cupId      = (uint8_t)myCupId;
  p.seq        = lastSeq;
  p.dropped    = droppedCount;
  p.rawWeight  = (int32_t)(lastReading - tare);   // net counts, uncalibrated
  p.tokenCount = tokens;
  p.rssi       = lastRssi;
  esp_now_send(gatewayMac, (const uint8_t*)&p, sizeof(p));
}

// ===========================================================================
// Rendering — redraw only when the rendered state actually changes.
// A full-screen glyph fill on every 500ms packet would flicker badly, so the
// current render inputs are compared against the last-rendered set and the
// draw is skipped when identical. Animated screens (scratched flash, podium
// cycle) keep their own timers.
// ===========================================================================
enum Screen : uint8_t {
  SCR_BOOT,        // nothing drawn yet
  SCR_WAITING,     // no cup ID yet: show own MAC big
  SCR_NO_HORSE,    // assigned, but horseForCup[me] == 0
  SCR_NUMBER,      // giant number on saddle cloth
  SCR_SCRATCHED,   // alternating flash
  SCR_PODIUM       // DDM_WINNER treatment
};

// NOTE: the Arduino .ino preprocessor hoists auto-generated prototypes above
// this struct, so functions below keep Rendered out of their signatures and
// work through the cur/lastDrawn globals instead.
struct Rendered {
  Screen  scr;
  uint8_t horse;
  bool    noLink;
};

Rendered cur         = { SCR_BOOT, 0, false };   // computed each render tick
Rendered lastDrawn   = { SCR_BOOT, 0, false };
bool     overlayOn   = false;
uint32_t tAnim       = 0;    // scratched/podium frame timer
uint8_t  animStep    = 0;
uint32_t tOverlay    = 0;

// --- screens ---------------------------------------------------------------

void drawWaiting() {
  tft.fillScreen(C_BLACK);
  centerText("LA QUINIELA", SW / 2, 30, SW - 24, C_AMBER);
  centerText("WAITING FOR", SW / 2, SH / 2 - 78, SW - 40, C_WHITE);
  centerText("GATEWAY",     SW / 2, SH / 2 - 46, SW - 40, C_WHITE);

  // Own MAC, split across two lines so pickFont can go big enough to read
  // and type into KNOWN_CUPS[] without a serial cable.
  char l1[12], l2[12];
  snprintf(l1, sizeof(l1), "%02X:%02X:%02X", myMac[0], myMac[1], myMac[2]);
  snprintf(l2, sizeof(l2), "%02X:%02X:%02X", myMac[3], myMac[4], myMac[5]);
  centerText(l1, SW / 2, SH / 2 + 16, SW - 16, C_GOLD);
  centerText(l2, SW / 2, SH / 2 + 66, SW - 16, C_GOLD);

  centerText("MAC ADDRESS", SW / 2, SH - 28, SW - 24, C_DIM);

  // Scale note, only while the HX711 is still warming up or taring, so it is
  // obvious why nothing is being counted yet. Built-in font, in the free band
  // between the MAC and its caption.
  if (scaleOk && scalePhase != SCALE_RUNNING) {
    const char* note = "SCALE WARMING UP";
    tft.setFont(nullptr);
    tft.setTextSize(2);
    tft.setTextColor(C_AMBER);
    tft.setCursor((SW - 12 * (int)strlen(note)) / 2, SH - 66);
    tft.print(note);
  }
}

void drawNoHorse() {
  tft.fillScreen(C_BLACK);
  char buf[16];
  snprintf(buf, sizeof(buf), "CUP %d", myCupId);
  centerText(buf, SW / 2, SH / 2 - 24, SW - 30, C_WHITE);
  centerText("NO HORSE", SW / 2, SH / 2 + 24, SW - 30, C_DIM);
}

void drawGiant(uint8_t n) {
  Cloth c = cloth(n);
  tft.fillScreen(c.bg);
  drawNumber(n, SW / 2, SH / 2, SW - 16, SH - 36, c.fg, c.bg);
}

// Scratched frame A: greyed number with SCRATCHED (geometry from the
// prototype's scratched screen, minus the X)
void drawScratchedNumber(uint8_t n) {
  tft.fillScreen(C_BLACK);

  int foot = 52;
  int side = min(SW - 30, SH - foot - 36);
  int cx   = SW / 2;
  int cy   = (SH - foot) / 2;

  drawNumber(n, cx, cy, side, side, C_DIM, C_BLACK);
  centerText("SCRATCHED", cx, SH - foot / 2, SW - 20, C_RED);
}

// Scratched frame B: the boxed X (X stroke thickness from the prototype)
void drawScratchedX() {
  tft.fillScreen(C_BLACK);

  int side = min(SW, SH) - 70;
  int cx   = SW / 2;
  int cy   = SH / 2;
  int h    = side / 2;

  tft.drawRect(cx - h - 14, cy - h - 14, side + 28, side + 28, C_RED);
  tft.drawRect(cx - h - 15, cy - h - 15, side + 30, side + 30, C_RED);

  int t = max(5, side / 26);
  for (int i = -t / 2; i <= t / 2; i++) {
    tft.drawLine(cx - h + i, cy - h, cx + h + i, cy + h, C_RED);
    tft.drawLine(cx + h + i, cy - h, cx - h + i, cy + h, C_RED);
  }
}

// Podium treatment from the prototype, applied to this cup's own horse.
// PROTOCOL NOTE: DdmStatePacket carries no win/place/show results, so in
// DDM_WINNER every cup cycles the metal treatment on its own number.
void drawPodiumFrame(uint8_t step, uint8_t horse) {
  const char* labels[3] = { "WIN", "PLACE", "SHOW" };
  uint16_t    metals[3] = { C_GOLD, C_SILVER, C_BRONZE };
  uint8_t     i = step % 3;

  tft.fillScreen(C_BLACK);
  tft.drawRect(6, 6, SW - 12, SH - 12, metals[i]);
  tft.drawRect(7, 7, SW - 14, SH - 14, metals[i]);

  int foot = 56;
  drawNumber(horse, SW / 2, (SH - foot) / 2,
             SW - 40, SH - foot - 40, metals[i], C_BLACK);

  centerText(labels[i], SW / 2, SH - foot / 2, SW - 30, metals[i]);
}

// --- badges / overlay ------------------------------------------------------

void drawNoLinkBadge() {
  tft.fillRect(0, 0, 88, 20, C_BLACK);
  tft.drawRect(0, 0, 88, 20, C_RED);
  tft.setFont(nullptr);
  tft.setTextSize(1);
  tft.setTextColor(C_RED, C_BLACK);
  tft.setCursor(6, 6);
  tft.print("NO LINK");
}

void drawOverlay() {
  uint32_t now = millis();
  int h = 78;                       // four lines of the built-in font
  int y = SH - h;
  uint8_t horse = 0, st = 0;
  if (haveState && myCupId >= 0) {
    horse = lastState.horseForCup[myCupId];
    st    = lastState.raceState;
  }

  tft.fillRect(0, y, SW, h, C_BLACK);
  tft.drawRect(0, y, SW, h, C_AMBER);
  tft.setFont(nullptr);
  tft.setTextSize(1);
  tft.setTextColor(C_WHITE, C_BLACK);

  tft.setCursor(6, y + 8);
  tft.printf("ID:%d  HORSE:%u  STATE:%u  VER!:%lu",
             myCupId, horse, st, (unsigned long)versionRejects);
  tft.setCursor(6, y + 24);
  tft.printf("RSSI:%d dBm  DROP:%lu", lastRssi, (unsigned long)droppedCount);
  tft.setCursor(6, y + 40);
  if (lastPacketAt == 0)
    tft.printf("SEQ:-  AGE:never");
  else
    tft.printf("SEQ:%lu  AGE:%lums",
               (unsigned long)lastSeq, (unsigned long)(now - lastPacketAt));
  tft.setCursor(6, y + 56);
  if (!scaleOk)
    tft.printf("TOKENS:-  NET:-  no HX711");
  else
    tft.printf("TOKENS:%u  NET:%+ld  %s", tokens, lastReading - tare,
               scalePhase == SCALE_WARMUP ? "warmup" :
               scalePhase == SCALE_TARING ? "taring" : "ok");
}

// --- render decision --------------------------------------------------------

static void computeRendered() {   // fills `cur`
  cur.noLink = (lastPacketAt != 0) && (millis() - lastPacketAt > LINK_TIMEOUT_MS);

  if (myCupId < 0) {
    cur.scr = SCR_WAITING; cur.horse = 0;
    return;
  }

  uint8_t horse     = haveState ? lastState.horseForCup[myCupId] : 0;
  uint8_t scratched = haveState ? lastState.scratched[myCupId]   : 0;
  uint8_t state     = haveState ? lastState.raceState            : DDM_PRE_RACE;

  if (horse == 0)                { cur.scr = SCR_NO_HORSE;  cur.horse = 0;     }
  else if (scratched)            { cur.scr = SCR_SCRATCHED; cur.horse = horse; }
  else if (state == DDM_WINNER)  { cur.scr = SCR_PODIUM;    cur.horse = horse; }
  else                           { cur.scr = SCR_NUMBER;    cur.horse = horse; }
}

static void drawBase() {          // draws `cur`
  const Rendered& r = cur;
  switch (r.scr) {
    case SCR_WAITING:   drawWaiting();                    break;
    case SCR_NO_HORSE:  drawNoHorse();                    break;
    case SCR_NUMBER:    drawGiant(r.horse);               break;
    case SCR_SCRATCHED: animStep = 0; tAnim = millis();
                        drawScratchedNumber(r.horse);     break;
    case SCR_PODIUM:    animStep = 0; tAnim = millis();
                        drawPodiumFrame(0, r.horse);      break;
    default: break;
  }
  if (r.noLink && r.scr != SCR_WAITING) drawNoLinkBadge();
  if (overlayOn) drawOverlay();
}

static void renderTick() {
  uint32_t now = millis();
  computeRendered();
  const Rendered& r = cur;

  // Full redraw only when something actually changed
  if (r.scr != lastDrawn.scr || r.horse != lastDrawn.horse ||
      r.noLink != lastDrawn.noLink) {
    drawBase();
    lastDrawn = r;
    return;
  }

  // Animated screens advance on their own timers
  if (r.scr == SCR_SCRATCHED && now - tAnim >= SCRATCH_FLASH_MS) {
    tAnim = now;
    animStep++;
    if (animStep & 1) drawScratchedX();
    else              drawScratchedNumber(r.horse);
    if (r.noLink) drawNoLinkBadge();
    if (overlayOn) drawOverlay();
  } else if (r.scr == SCR_PODIUM && now - tAnim >= PODIUM_STEP_MS) {
    tAnim = now;
    drawPodiumFrame(++animStep, r.horse);
    if (r.noLink) drawNoLinkBadge();
    if (overlayOn) drawOverlay();
  }

  // Overlay values refresh while visible
  if (overlayOn && now - tOverlay >= OVERLAY_MS) {
    tOverlay = now;
    drawOverlay();
  }
}

// ---------------------------------------------------------------------------
// The CYD's display controller is not an ILI9341: the panel probe
// (tools/panel_probe) reads an ST7789-family register map on every board.
// Two things in the Adafruit ILI9341 init table go wrong on it:
//
//  * It sends a Vertical Scrolling Start Address (0x37) and never Normal
//    Display Mode ON, which leaves the panel in vertical-scroll mode (status
//    register bit D15 reads 1 after tft.begin()). On these panels the bottom
//    quarter of the glass then stops following frame memory: writes land,
//    nothing shows. Define the scroll area as the whole panel, zero the
//    scroll start and send NORON (0x13).
//
//  * It writes 0xC0 = 0x23. On an ILI9341 that is Power Control 1; on an
//    ST7789 it is LCMCTRL, a byte of XOR flags laid over MADCTL:
//    0x23 = XBGR | XMV | XGS. XMV inverts the meaning of MADCTL's MV
//    (row/column exchange) bit. While the panel is scrolling MV is not
//    applied, which is why rotation 0 looked right before the NORON fix and
//    rotations 1/3 "stayed portrait"; the moment scroll mode ends the
//    inverted MV bit takes effect and rotation 0 comes out transposed.
//    XBGR likewise inverts the driver's BGR bit, so the glass ran in RGB
//    order and horse 1's red cloth came up blue (Board A, 2026-09-12).
//    Rewrite LCMCTRL with XMV and XBGR cleared, keeping only XGS as the
//    init left it (0x01); panelOrientation() picks the MADCTL that is
//    upright with XGS set.
//
// On a genuine ILI9341 the scroll commands are harmless and the 0xC0 write
// only sets a slightly lower GVDD in Power Control 1.
// ---------------------------------------------------------------------------
static void panelNormalMode() {
  static const uint8_t lcmctrl[1] = { 0x01 };                               // XGS only: XMV and XBGR cleared
  static const uint8_t vscrdef[6] = { 0x00, 0x00, 0x01, 0x40, 0x00, 0x00 }; // TFA 0, VSA 320, BFA 0
  static const uint8_t vscsad[2]  = { 0x00, 0x00 };                         // scroll start 0
  tft.sendCommand(0xC0, lcmctrl, 1);                                        // LCMCTRL: MV means MV, BGR means BGR
  tft.sendCommand(0x33, vscrdef, 6);
  tft.sendCommand(0x37, vscsad, 2);
  tft.sendCommand(0x13);                                                    // NORON: scroll mode off
}

// ---------------------------------------------------------------------------
// The driver's portrait MADCTL values assume an ILI9341 glass: 0x48 (MX|BGR)
// for rotation 0 and 0x88 (MY|BGR) for rotation 2. On these panels, once
// they are out of scroll mode, 0x48 comes out flipped top-to-bottom and
// 0x88 flipped left-to-right (Board A, 2026-09-12). Upright needs MX and MY
// both set, which no library rotation produces, so setRotation() is used
// for its width/height bookkeeping only and the MADCTL byte is sent here.
// BGR stays set as the driver would send it; colour order is LCMCTRL's job.
// The cup with the scale (2026-09-15) wants MX alone (0x48): same build,
// vertical flip the other way, so the glass is wired differently between
// board batches. Which MX/MY pair is upright is therefore a per-cup setting:
// ORIENT_DEFAULT is only the default, NVS holds the real bits, and serial
// o/h/v or a 6 s BOOT hold change and save them.
// ---------------------------------------------------------------------------
static void panelOrientation() {
  uint8_t madctl = 0x08 | orientFlips;                  // BGR | MX? | MY?
  tft.sendCommand(0x36, &madctl, 1);                    // MADCTL
}

static void panelSetOrientation(uint8_t flips, const char* how) {
  orientFlips = flips & 0xC0;
  prefs.putUChar("flip", orientFlips);
  panelOrientation();
  lastDrawn.scr = SCR_BOOT;                             // full redraw on the next tick
  Serial.printf("[panel] orientation MADCTL 0x%02X saved to NVS (%s)\n", 0x08 | orientFlips, how);
}

// Next of the four settings, one bit at a time: C8 -> 48 -> 08 -> 88 -> C8.
static void panelNextOrientation(const char* how) {
  static const uint8_t cycle[4] = { 0xC0, 0x40, 0x00, 0x80 };
  uint8_t i = 0;
  while (i < 3 && cycle[i] != orientFlips) i++;
  panelSetOrientation(cycle[(i + 1) & 3], how);
}

// Controller ID bytes (RDDID 0x04), raw over SPI at 2 MHz and realigned for
// the one dummy clock, exactly as tools/panel_probe does it. Board A reads
// 10 81 B3 here (the init table's 0xC1 write lands in IDSET). Logged so a
// board revision with different glass wiring can be told apart later.
static void panelLogId() {
  uint8_t in[4], id[3];
  SPI.beginTransaction(SPISettings(2000000, MSBFIRST, SPI_MODE0));
  digitalWrite(TFT_CS, LOW);
  digitalWrite(TFT_DC, LOW);
  SPI.transfer(0x04);
  digitalWrite(TFT_DC, HIGH);
  for (uint8_t i = 0; i < 4; i++) in[i] = SPI.transfer(0x00);
  digitalWrite(TFT_CS, HIGH);
  SPI.endTransaction();
  for (uint8_t i = 0; i < 3; i++) id[i] = (uint8_t)((in[i] << 1) | (in[i + 1] >> 7));
  Serial.printf("panel RDDID: %02X %02X %02X  (Board A reads 10 81 B3 here)\n", id[0], id[1], id[2]);
}

// ===========================================================================
// SCALE — HX711 token counting
//
// Counting is by increments against a slow-tracking baseline, never by
// absolute weight: a stack of tokens keeps relaxing for 10-15 s after every
// impact (about 2% of the total load), so absolute weight drifts by roughly
// a token in sixty. The baseline is a low-pass of the reading (time constant
// SCALE_BASELINE_TAU_MS) that only updates while the reading is within half a
// token of it, so warm-up drift and post-impact relaxation are absorbed and
// never counted. A jump of at least half a token that holds for
// SCALE_CONFIRM_SAMPLES consecutive samples is an event: the step is rounded
// to whole tokens (two dropped together count as two), the baseline snaps to
// the new reading, and tracking resumes. Downward steps decrement the same
// way. A bump spikes and returns within a sample or two, so it never confirms.
//
// Everything here is non-blocking: the ADC is only read when is_ready() says
// a conversion is waiting, so the display and the radio never stall on it.
// ===========================================================================
static void scaleStartTare(const char* why, bool blink) {
  scalePhase = SCALE_TARING;
  tareSum    = 0;
  tareN      = 0;
  confirmN   = 0;
  tokens     = 0;                       // a tare means "this is empty"
  Serial.printf("[tare] %s: averaging %d samples\n", why, SCALE_TARE_SAMPLES);
  if (blink) {
    digitalWrite(LED_G, LOW);           // CYD LEDs are active low
    tLedOff = millis() + TARE_BLINK_MS;
  }
}

static void scaleTick(uint32_t now) {
  if (tLedOff && (int32_t)(now - tLedOff) >= 0) {
    digitalWrite(LED_G, HIGH);
    tLedOff = 0;
  }
  if (!scaleOk) return;

  if (scalePhase == SCALE_WARMUP && now - tScaleWarmup >= SCALE_WARMUP_MS)
    scaleStartTare("warm-up done", false);

  if (!hx711.is_ready()) return;        // nothing waiting: never block on the ADC
  long r = hx711.read();
  uint32_t dt = now - tScaleSample;
  tScaleSample = now;
  lastReading  = r;

  switch (scalePhase) {
    case SCALE_WARMUP:
      return;                           // sampling only, so the overlay has a live number

    case SCALE_TARING:
      tareSum += r;
      if (++tareN >= SCALE_TARE_SAMPLES) {
        tare       = tareSum / SCALE_TARE_SAMPLES;
        baseline   = (float)tare;
        scalePhase = SCALE_RUNNING;
        Serial.printf("[tare] offset=%ld tokens=%u\n", tare, tokens);
        if (cur.scr == SCR_WAITING) lastDrawn.scr = SCR_BOOT;   // drop the WARMING UP note
      }
      return;

    case SCALE_RUNNING: {
      float delta = (float)r - baseline;
      if (fabsf(delta) < (float)TOKEN_THRESHOLD) {
        // Quiet: track slowly. alpha = dt / tau, capped so a long gap cannot overshoot.
        confirmN = 0;
        float a = (float)dt / (float)SCALE_BASELINE_TAU_MS;
        if (a > 1.0f) a = 1.0f;
        baseline += delta * a;
        if (tokens == 0) tare = lroundf(baseline);   // an empty cup keeps re-zeroing itself
        return;
      }
      if (++confirmN < SCALE_CONFIRM_SAMPLES) return;   // a bump does not get this far
      confirmN = 0;
      long n = lroundf(delta / (float)COUNTS_PER_TOKEN);
      if (n > 0) {
        tokens += (uint16_t)n;
        Serial.printf("[drop] +%ld tokens=%u step=%ld baseline=%ld\n",
                      n, tokens, (long)delta, (long)baseline);
      } else if (n < 0) {
        long take = -n;
        if (take > tokens) take = tokens;              // clamp at 0
        tokens -= (uint16_t)take;
        Serial.printf("[remove] -%ld tokens=%u step=%ld baseline=%ld\n",
                      take, tokens, (long)delta, (long)baseline);
      }
      baseline = (float)r;                             // snap, then resume tracking
      return;
    }
  }
}

// ===========================================================================
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("DDM La Quiniela cup — ESP-NOW bench test");

  pinMode(LED_R, OUTPUT); digitalWrite(LED_R, HIGH);
  pinMode(LED_G, OUTPUT); digitalWrite(LED_G, HIGH);
  pinMode(LED_B, OUTPUT); digitalWrite(LED_B, HIGH);
  pinMode(BOOT_BTN, INPUT_PULLUP);

  SPI.begin(TFT_SCLK, TFT_MISO, TFT_MOSI, TFT_CS);

  // Per-cup settings from NVS (survive reflashing; only an NVS erase clears them)
  prefs.begin("ddmcup", false);
  if (prefs.isKey("rot")) prefs.remove("rot");          // key from the two-way version of this setting
  orientFlips = prefs.getUChar("flip", ORIENT_DEFAULT) & 0xC0;

  tft.begin();
  panelLogId();
  panelNormalMode();
  tft.setRotation(0);          // library bookkeeping only: 240 wide, 320 tall
  panelOrientation();          // the MADCTL this cup is set to
  Serial.printf("orientation MADCTL 0x%02X%s; serial o/h/v or BOOT held 6 s changes and saves\n",
                0x08 | orientFlips, prefs.isKey("flip") ? " from NVS" : " (default)");
  tft.invertDisplay(INVERT);

  SW = tft.width();
  SH = tft.height();
  Serial.printf("screen %dx%d\n", SW, SH);

  backlightInit();
  backlight(255);

  // ESP-NOW only: STA mode, never associated, pinned channel
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);
  esp_wifi_set_channel(DDM_ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);

  WiFi.macAddress(myMac);
  Serial.printf("cup MAC: %02X:%02X:%02X:%02X:%02X:%02X  proto v%d  channel %d\n",
                myMac[0], myMac[1], myMac[2], myMac[3], myMac[4], myMac[5],
                DDM_PROTO_VERSION, DDM_ESPNOW_CHANNEL);

  if (esp_now_init() != ESP_OK) {
    Serial.println("FATAL esp_now_init failed");
    tft.fillScreen(C_BLACK);
    centerText("ESP-NOW FAIL", SW / 2, SH / 2, SW - 20, C_RED);
    while (true) delay(1000);
  }
  esp_now_register_recv_cb(onDataRecv);

  // Broadcast peer for the hello phase, before the gateway MAC is known
  esp_now_peer_info_t p = {};
  memcpy(p.peer_addr, BCAST, 6);
  p.channel = DDM_ESPNOW_CHANNEL;
  p.ifidx   = WIFI_IF_STA;
  p.encrypt = false;
  esp_now_add_peer(&p);

  // Scale. The only blocking call is this one-second boot check; if the
  // HX711 is missing the cup runs without counting and the overlay says so.
  hx711.begin(HX711_DT, HX711_SCK, 128);
  pinMode(HX711_DT, INPUT_PULLUP);      // a missing amplifier reads "not ready", not garbage
  scaleOk      = hx711.wait_ready_timeout(1000, 10);
  tScaleWarmup = millis();
  tScaleSample = tScaleWarmup;
  Serial.printf("scale: %s\n", scaleOk
                ? "HX711 ok, warming up 30 s before tare"
                : "no HX711 on CN1 (DT 27, SCK 22), counting disabled");

  renderTick();   // puts up the waiting screen
}

void loop() {
  uint32_t now = millis();

  // --- BOOT button: short press toggles the diagnostic overlay; held for
  //     TARE_HOLD_MS it re-tares the scale, kept held to ORIENT_HOLD_MS it
  //     steps the display orientation and saves it (neither press toggles)
  static bool     down      = false;
  static uint32_t downAt    = 0;
  static uint8_t  holdStage = 0;      // 0 nothing yet, 1 tare fired, 2 flip fired

  bool pressed = (digitalRead(BOOT_BTN) == LOW);
  if (pressed && !down) {
    down = true; downAt = now; holdStage = 0;
  } else if (pressed && down && holdStage == 0 && now - downAt >= TARE_HOLD_MS) {
    holdStage = 1;                    // fires once per hold, while still held
    if (scaleOk) scaleStartTare("BOOT held", true);
    else         Serial.println("[tare] ignored: no HX711");
  } else if (pressed && down && holdStage == 1 && now - downAt >= ORIENT_HOLD_MS) {
    holdStage = 2;
    panelNextOrientation("BOOT held");
  } else if (!pressed && down) {
    down = false;
    if (holdStage == 0 && now - downAt > 30) {   // debounce; a hold is not a short press
      overlayOn = !overlayOn;
      Serial.printf("[btn] overlay %s\n", overlayOn ? "on" : "off");
      if (overlayOn) { tOverlay = now; drawOverlay(); }
      else           { lastDrawn.scr = SCR_BOOT; }   // force base redraw
    }
  }

  // --- serial commands (bench): o/h/v set the orientation and save, t = tare
  while (Serial.available()) {
    char c = (char)Serial.read();
    if      (c == 'o') panelNextOrientation("serial o");
    else if (c == 'h') panelSetOrientation(orientFlips ^ 0x40, "serial h: mirror left-right");
    else if (c == 'v') panelSetOrientation(orientFlips ^ 0x80, "serial v: mirror top-bottom");
    else if (c == 't') { if (scaleOk) scaleStartTare("serial", true); else Serial.println("[tare] ignored: no HX711"); }
    else if (c == '?') Serial.println("commands: o = next orientation, h = mirror left-right, v = mirror top-bottom (all saved to NVS), t = tare, ? = help");
  }

  // --- drain packets handed over by the receive callback --------------------
  if (rxAckPending) {
    rxAckPending = false;
    ensureGatewayPeer((const uint8_t*)rxSrcMac);
    lastRssi     = rxRssiVal;
    lastPacketAt = now;
    if ((int)rxAckCupId != myCupId) {
      myCupId = rxAckCupId;
      Serial.printf("[link] assigned cup ID %d\n", myCupId);
    }
  }

  if (rxStatePending) {
    rxStatePending = false;
    ensureGatewayPeer((const uint8_t*)rxSrcMac);
    memcpy(&lastState, &rxStateBuf, sizeof(lastState));

    // Drop detection: any gap in seq is packets we missed. A seq lower than
    // the last one seen means the gateway rebooted — resync, don't count.
    if (haveSeq && lastState.seq > lastSeq + 1)
      droppedCount += lastState.seq - lastSeq - 1;
    lastSeq  = lastState.seq;
    haveSeq  = true;
    haveState = true;
    lastRssi     = rxRssiVal;
    lastPacketAt = now;
  }

  scaleTick(now);

  // --- uplink cadence --------------------------------------------------------
  if (myCupId < 0) {
    if (now - tHello >= HELLO_MS) {
      tHello = now;
      sendHello();
    }
  } else if (gatewayKnown && now - tTelemetry >= TELEMETRY_MS) {
    tTelemetry = now;
    sendTelemetry();
  }

  renderTick();
}
