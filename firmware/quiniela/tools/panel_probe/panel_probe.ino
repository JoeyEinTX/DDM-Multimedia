/*
 * panel_probe.ino — CYD (ESP32-2432S028R) display controller probe
 *
 * Standalone diagnostic for the "bottom band never clears" symptom seen on
 * the newer cup boards (B/C/D) but not on the original prototype (A).
 * It does NOT need ddm_common.h or ddm_digits.h. Flash it exactly like
 * ddm_cup.ino (board: ESP32 Dev Module, libraries: Adafruit GFX Library +
 * Adafruit ILI9341, hold BOOT to upload). Serial monitor at 115200.
 *
 * Run it on Board A and on one of B/C/D, keep the serial log of each, and
 * photograph the glass during the phases described below.
 *
 * WHAT IT DOES
 *   Phase 1  Controller ID registers, read raw over SPI at 2 MHz (well inside
 *            the ILI9341/ST7789 read timing), BEFORE and AFTER the Adafruit
 *            ILI9341 init table runs:  RDDID (0x04), RDID4 (0xD3, also via
 *            the ILI9341 0xD9 index trick), RDDST (0x09), RDDPM (0x0A),
 *            RDDMADCTL (0x0B), RDDCOLMOD (0x0C), RDDIM (0x0D), RDDSM (0x0E),
 *            RDDSDR (0x0F). Known ID signatures are matched automatically.
 *   Phase 2  Rotation walk 0..3: the library's width x height, the MADCTL
 *            value Adafruit_ILI9341 writes for that rotation, the MADCTL read
 *            back from the controller, and on the glass: a fill, a 3px border
 *            at the extreme edges of what the driver believes the panel to
 *            be, TL/TR/BL/BR corner tags, and the rotation number in large
 *            type. ~3 s each.
 *   Phase 3  Labelled ruler per rotation: a horizontal line every 20 px with
 *            its y coordinate at both ends, x ticks along the top, the last
 *            row and column the driver believes exist drawn in red. ~5 s
 *            each. The last readable y label = the real drawable height.
 *   Phase 4  Raw CASET/PASET/RAMWR writes that bypass Adafruit_GFX and the
 *            library's clipping entirely: every MADCTL value the driver uses,
 *            each with both a portrait (240x320) and a transposed (320x240)
 *            window, then candidate row offsets 20/40/80/160 in both
 *            addressing modes. Each write is verified by reading the pixel
 *            back out of GRAM (RAMRD), so "the write never landed" and "it
 *            landed but that GRAM region is not what feeds the bottom of the
 *            glass" are told apart. ~2.5 s each; a WHITE block marks the
 *            window origin and a YELLOW block marks its far corner so the
 *            physical landing spot can be read off the glass.
 *   Phase 5  Only if Phase 1 identified an ST7789: the same two full-frame
 *            writes after restoring the ST7789 LCMCTRL register (0xC0) to
 *            its datasheet default 0x2C. See WHY below.
 *   Then a summary screen alternates between rotation 0 and 3 so it is
 *   readable on either board revision. Short-press BOOT to run again.
 *
 * WHY THE ST7789 CHECK
 *   The Adafruit_ILI9341 init table sends  0xC0 = 0x23  (ILI9341 "Power
 *   Control 1", VRH). On an ST7789V, 0xC0 is LCMCTRL, whose parameter bits
 *   are  -, XMY, XBGR, XINV, XMX, XMH, XMV, XGS  (default 0x2C). 0x23 sets
 *   XMV = 1 and XGS = 1: the controller then XORs the MV bit of every MADCTL
 *   write and flips the gate scan direction. Rotation 3 (MADCTL 0xE8, MV=1)
 *   therefore lands the panel in PORTRAIT addressing while the library
 *   believes it is 320 wide x 240 tall, so every full-frame draw programs
 *   PASET 0..239 and glass rows 240..319 — exactly a quarter of the panel —
 *   are never written. The init table also sends 0xC1 = 0x10, which on an
 *   ST7789V is IDSET and overwrites ID1, so RDDID reads differently before
 *   and after tft.begin(): that difference alone is a fingerprint.
 *
 * The 2-USB (micro-USB + USB-C) revision of the ESP32-2432S028R is widely
 * reported to ship with an ST7789 instead of the ILI9341.
 */

#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <Fonts/FreeSansBold12pt7b.h>
#include <Fonts/FreeSansBold24pt7b.h>

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------
#define PROBE_SPI_HZ   40000000   // same as ddm_cup.ino (Adafruit default on ESP32)
#define READ_SPI_HZ     2000000   // register / GRAM reads run slow on purpose
#define INVERT         false      // mirror ddm_cup.ino

#define HOLD_ROT_MS    3000
#define HOLD_RULER_MS  5000
#define HOLD_RAW_MS    2500

// Phase 5 is normally gated on an ST7789 ID match. On a genuine ILI9341,
// 0xC0 is a power register (VRH) and 0x2C would set GVDD above the value
// the init table chose, so it is only forced by hand and only for a test.
#define FORCE_LCMCTRL_TEST 0

// ---------------------------------------------------------------------------
// CYD hardware pins — identical to ddm_cup.ino
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

// ---------------------------------------------------------------------------
// Colours (RGB565)
// ---------------------------------------------------------------------------
#define C_BLACK    0x0000
#define C_WHITE    0xFFFF
#define C_RED      0xF800
#define C_GREEN    0x07E0
#define C_BLUE     0x001F
#define C_YELLOW   0xFFE0
#define C_CYAN     0x07FF
#define C_MAGENTA  0xF81F
#define C_ORANGE   0xFD20
#define C_GREY     0x7BEF
#define C_NAVY     0x000F
#define C_DKGREEN  0x03E0
#define C_MAROON   0x7800
#define C_PURPLE   0x780F

// Fill colours for the raw tests, rotated so consecutive tests differ and a
// region that did NOT get written keeps the previous test's colour.
static const uint16_t FILL_C[] = { C_BLUE, C_GREEN, C_RED, C_MAGENTA, C_CYAN, C_ORANGE };
static const char*    FILL_N[] = { "BLUE", "GREEN", "RED", "MAGENTA", "CYAN", "ORANGE" };
#define NFILL 6

// MADCTL values Adafruit_ILI9341::setRotation() writes (MX=0x40 MY=0x80
// MV=0x20 BGR=0x08), taken from the library source.
static const uint8_t ROT_MADCTL[4] = { 0x48, 0x28, 0x88, 0xE8 };
static const uint16_t ROT_BG[4]    = { C_NAVY, C_DKGREEN, C_MAROON, C_PURPLE };
static const char*   ROT_BGN[4]    = { "navy", "dark green", "maroon", "purple" };

// Register names
#define CMD_SWRESET 0x01
#define CMD_RDDID   0x04
#define CMD_RDDST   0x09
#define CMD_RDDPM   0x0A
#define CMD_RDDMADC 0x0B
#define CMD_RDDCOLM 0x0C
#define CMD_RDDIM   0x0D
#define CMD_RDDSM   0x0E
#define CMD_RDDSDR  0x0F
#define CMD_CASET   0x2A
#define CMD_PASET   0x2B
#define CMD_RAMWR   0x2C
#define CMD_RAMRD   0x2E
#define CMD_MADCTL  0x36
#define CMD_RDID4   0xD3
#define CMD_IDXRD   0xD9   // ILI9341 undocumented "select parameter of next read"

// ---------------------------------------------------------------------------
// Detection state (filled by Phase 1, shown on the summary screen)
// ---------------------------------------------------------------------------
static char    detectedName[40] = "unrecognised";
static bool    detectedST7789   = false;
static uint8_t deadReads        = 0;      // multi-byte reads that came back all 00 / all FF
static uint8_t idPre[4], idPost[4], id4Post[5];
static int     rotW[4], rotH[4];
static uint8_t rotMadRead[4];

// ===========================================================================
// Raw SPI helpers — own transaction at READ_SPI_HZ, CS/DC driven by hand.
// Never call these between tft.startWrite() and tft.endWrite().
// ===========================================================================
static void rawBegin() {
  SPI.beginTransaction(SPISettings(READ_SPI_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(TFT_CS, LOW);
}
static void rawEnd() {
  digitalWrite(TFT_CS, HIGH);
  SPI.endTransaction();
}
static void rawCmdByte(uint8_t c) {
  digitalWrite(TFT_DC, LOW);
  SPI.transfer(c);
  digitalWrite(TFT_DC, HIGH);
}
static void rawData16(uint16_t v) {
  SPI.transfer(v >> 8);
  SPI.transfer(v & 0xFF);
}

// Command with optional parameters, slow bus (used before tft.begin()).
static void rawCommand(uint8_t cmd, const uint8_t* data, uint8_t n) {
  rawBegin();
  rawCmdByte(cmd);
  for (uint8_t i = 0; i < n; i++) SPI.transfer(data[i]);
  rawEnd();
}

// Read n bytes after a command byte.
static void rawRead(uint8_t cmd, uint8_t* buf, uint8_t n) {
  rawBegin();
  rawCmdByte(cmd);
  for (uint8_t i = 0; i < n; i++) buf[i] = SPI.transfer(0x00);
  rawEnd();
}

// ILI9341-style indexed read: 0xD9 <0x10+idx> then one byte of <cmd>.
// This is what Adafruit_ILI9341::readcommand8() does.
static uint8_t rawReadIndexed(uint8_t cmd, uint8_t idx) {
  uint8_t sel = 0x10 + idx;
  rawCommand(CMD_IDXRD, &sel, 1);
  uint8_t b;
  rawRead(cmd, &b, 1);
  return b;
}

// Multi-byte reads on these controllers carry one dummy clock before the
// data, so the bytes come back shifted right by one bit. This undoes it.
static void shiftLeft1(const uint8_t* in, uint8_t* out, uint8_t n) {
  for (uint8_t i = 0; i < n; i++)
    out[i] = (uint8_t)((in[i] << 1) | ((i + 1 < n) ? (in[i + 1] >> 7) : 0));
}

static void hexStr(const uint8_t* b, uint8_t n, char* out, size_t outLen) {
  size_t p = 0;
  for (uint8_t i = 0; i < n && p + 3 < outLen; i++)
    p += snprintf(out + p, outLen - p, "%02X ", b[i]);
  if (p > 0) out[p - 1] = 0; else out[0] = 0;
}

static bool allSame(const uint8_t* b, uint8_t n) {
  for (uint8_t i = 1; i < n; i++) if (b[i] != b[0]) return false;
  return (b[0] == 0x00 || b[0] == 0xFF);
}

// Read one pixel back out of GRAM at (x,y) under the CURRENT MADCTL.
// Serial RAMRD returns one dummy byte then R,G,B with 6 bits in the top of
// each byte on both the ILI9341 and the ST7789. Returns RGB565.
static uint16_t readGram(uint16_t x, uint16_t y, uint8_t* raw4) {
  rawBegin();
  rawCmdByte(CMD_CASET); rawData16(x); rawData16(x);
  rawCmdByte(CMD_PASET); rawData16(y); rawData16(y);
  rawCmdByte(CMD_RAMRD);
  for (uint8_t i = 0; i < 4; i++) raw4[i] = SPI.transfer(0x00);
  rawEnd();
  return (uint16_t)(((raw4[1] & 0xF8) << 8) | ((raw4[2] & 0xFC) << 3) | (raw4[3] >> 3));
}

// ===========================================================================
// Raw write helpers — through the library's bus at PROBE_SPI_HZ, but with the
// window programmed by hand so nothing in Adafruit_GFX can clip or cache it.
// ===========================================================================
static void rawWindowFill(uint16_t x0, uint16_t x1, uint16_t y0, uint16_t y1, uint16_t color) {
  tft.startWrite();
  tft.writeCommand(CMD_CASET); tft.SPI_WRITE16(x0); tft.SPI_WRITE16(x1);
  tft.writeCommand(CMD_PASET); tft.SPI_WRITE16(y0); tft.SPI_WRITE16(y1);
  tft.writeCommand(CMD_RAMWR);
  tft.writeColor(color, (uint32_t)(x1 - x0 + 1) * (uint32_t)(y1 - y0 + 1));
  tft.endWrite();
}

static void setMadctl(uint8_t m) {
  uint8_t v = m;
  tft.sendCommand(CMD_MADCTL, &v, 1);
}

// Adafruit_ILI9341::setAddrWindow() remembers the last CASET/PASET it sent and
// skips resending an unchanged window. After any raw window write that cache
// is stale, so poke a throwaway 1x1 window through it before library drawing.
static void poisonAddrCache() {
  tft.startWrite();
  tft.setAddrWindow(1, 1, 1, 1);
  tft.endWrite();
}

// ===========================================================================
// Backlight (same LEDC handling as ddm_cup.ino)
// ===========================================================================
static void backlightInit() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(TFT_BL, 5000, 8);
  ledcWrite(TFT_BL, 255);
#else
  ledcSetup(0, 5000, 8);
  ledcAttachPin(TFT_BL, 0);
  ledcWrite(0, 255);
#endif
}

// ===========================================================================
// Text helpers
// ===========================================================================
static void centerText(const char* s, int cx, int cy, const GFXfont* f, uint8_t size, uint16_t fg) {
  tft.setFont(f);
  tft.setTextSize(size);
  tft.setTextColor(fg);
  int16_t x1, y1; uint16_t w, h;
  tft.getTextBounds(s, 0, 0, &x1, &y1, &w, &h);
  tft.setCursor(cx - w / 2 - x1, cy - h / 2 - y1);
  tft.print(s);
}

static void smallText(int x, int y, uint16_t fg, const char* s) {
  tft.setFont(nullptr);
  tft.setTextSize(1);
  tft.setTextColor(fg);
  tft.setCursor(x, y);
  tft.print(s);
}

// ===========================================================================
// Phase 1 — controller identification
// ===========================================================================
struct IdSig { uint8_t a; uint8_t b; const char* name; };
static const IdSig SIGS[] = {
  { 0x93, 0x41, "ILI9341" },
  { 0x93, 0x42, "ILI9342C" },
  { 0x85, 0x52, "ST7789V (RDDID 85 85 52)" },
  { 0x97, 0x96, "ST7796" },
  { 0x94, 0x88, "ILI9488" },
  { 0x94, 0x86, "ILI9486" },
  { 0x94, 0x81, "ILI9481" },
  { 0x9A, 0x01, "GC9A01" },
  { 0x89, 0xF0, "ST7735" },
};

static const char* matchSig(const uint8_t* b, uint8_t n) {
  for (uint8_t i = 0; i + 1 < n; i++)
    for (size_t s = 0; s < sizeof(SIGS) / sizeof(SIGS[0]); s++)
      if (b[i] == SIGS[s].a && b[i + 1] == SIGS[s].b) return SIGS[s].name;
  return nullptr;
}

// Reads a multi-byte register, prints raw and 1-bit-shifted views, and
// reports any signature match in either alignment.
static const char* readAndPrintId(const char* label, uint8_t cmd, uint8_t n, uint8_t* keep) {
  uint8_t raw[8], sh[8];
  char s1[32], s2[32];
  rawRead(cmd, raw, n);
  shiftLeft1(raw, sh, n);
  hexStr(raw, n, s1, sizeof(s1));
  hexStr(sh,  n, s2, sizeof(s2));
  const char* m = matchSig(raw, n);
  const char* m2 = matchSig(sh, n);
  Serial.printf("  %-14s (0x%02X): raw %-16s | shifted<<1 %-16s | %s\n",
                label, cmd, s1, s2,
                m ? m : (m2 ? m2 : "no known signature"));
  if (keep) memcpy(keep, raw, n);
  if (allSame(raw, n)) deadReads++;
  return m ? m : m2;
}

static void readAndPrint8(const char* label, uint8_t cmd) {
  uint8_t b[2];
  rawRead(cmd, b, 2);
  Serial.printf("  %-14s (0x%02X): 0x%02X  (next byte 0x%02X, <<1 view 0x%02X)\n",
                label, cmd, b[0], b[1], (uint8_t)((b[0] << 1) | (b[1] >> 7)));
}

static void identify(const char* when, bool keepAsPre) {
  Serial.printf("--- controller registers %s ---\n", when);
  deadReads = 0;
  const char* m1 = readAndPrintId("RDDID", CMD_RDDID, 4, keepAsPre ? idPre : idPost);
  const char* m2 = readAndPrintId("RDID4", CMD_RDID4, 5, keepAsPre ? nullptr : id4Post);

  uint8_t ix[4];
  for (uint8_t i = 0; i < 4; i++) ix[i] = rawReadIndexed(CMD_RDID4, i);
  char s[32]; hexStr(ix, 4, s, sizeof(s));
  const char* m3 = matchSig(ix, 4);
  Serial.printf("  %-14s (0xD9 index 0..3 of 0xD3): %s | %s\n",
                "RDID4 indexed", s, m3 ? m3 : "no known signature");

  uint8_t st[5];
  readAndPrintId("RDDST", CMD_RDDST, 5, st);
  readAndPrint8("RDDPM",     CMD_RDDPM);
  readAndPrint8("RDDMADCTL", CMD_RDDMADC);
  readAndPrint8("RDDCOLMOD", CMD_RDDCOLM);
  readAndPrint8("RDDIM",     CMD_RDDIM);
  readAndPrint8("RDDSM",     CMD_RDDSM);
  readAndPrint8("RDDSDR",    CMD_RDDSDR);

  const char* best = m1 ? m1 : (m2 ? m2 : m3);
  if (best) {
    strncpy(detectedName, best, sizeof(detectedName) - 1);
    detectedName[sizeof(detectedName) - 1] = 0;
    if (strstr(best, "ST7789")) detectedST7789 = true;
  }
  if (deadReads >= 3)
    Serial.println("  NOTE: RDDID, RDID4 and RDDST all came back all-0x00 or all-0xFF. Either the\n"
                   "        controller's SDO/MISO is not wired on this board or reads are not\n"
                   "        working; every readback below is then meaningless. The visual tests\n"
                   "        still stand. (A genuine ILI9341 often returns zeros for RDDID alone —\n"
                   "        that on its own is normal; RDID4 is the one that must answer.)");
  Serial.printf("  => identification: %s\n", detectedName);
}

// ===========================================================================
// Phase 2 — rotation walk
// ===========================================================================
static void showRotation(uint8_t r) {
  tft.setRotation(r);
  int w = tft.width(), h = tft.height();
  uint8_t mb[2];
  rawRead(CMD_RDDMADC, mb, 2);
  rotW[r] = w; rotH[r] = h; rotMadRead[r] = mb[0];

  Serial.printf("[rot %u] library reports %dx%d | MADCTL written by driver 0x%02X | read back 0x%02X%s | fill %s\n",
                r, w, h, ROT_MADCTL[r], mb[0],
                (mb[0] == ROT_MADCTL[r]) ? " (match)" :
                ((uint8_t)((mb[0] << 1) | (mb[1] >> 7)) == ROT_MADCTL[r]) ? " (match after 1-bit shift: dummy clock on 8-bit reads)" : " (MISMATCH)",
                ROT_BGN[r]);

  tft.fillScreen(ROT_BG[r]);
  tft.drawRect(0, 0, w, h, C_WHITE);
  tft.drawRect(1, 1, w - 2, h - 2, C_WHITE);
  tft.drawRect(2, 2, w - 4, h - 4, C_WHITE);

  tft.setFont(nullptr);
  tft.setTextSize(2);
  tft.setTextColor(C_WHITE);
  tft.setCursor(6, 6);          tft.print("TL");
  tft.setCursor(w - 30, 6);     tft.print("TR");
  tft.setCursor(6, h - 22);     tft.print("BL");
  tft.setCursor(w - 30, h - 22); tft.print("BR");

  char big[2] = { (char)('0' + r), 0 };
  centerText(big, w / 2, h / 2 - 10, &FreeSansBold24pt7b, 3, C_WHITE);

  char cap[48];
  snprintf(cap, sizeof(cap), "rot %u  %dx%d  MADCTL %02X", r, w, h, ROT_MADCTL[r]);
  centerText(cap, w / 2, h - 40, &FreeSansBold12pt7b, 1, C_YELLOW);

  delay(HOLD_ROT_MS);
}

// ===========================================================================
// Phase 3 — labelled ruler
// ===========================================================================
static void showRuler(uint8_t r) {
  tft.setRotation(r);
  int w = tft.width(), h = tft.height();
  Serial.printf("[ruler rot %u] %dx%d: lines every 20 px, labels at both ends; last row %d and last column %d in red\n",
                r, w, h, h - 1, w - 1);

  tft.fillScreen(C_BLACK);
  tft.setFont(nullptr);
  tft.setTextSize(1);

  for (int y = 0; y < h; y += 20) {
    uint16_t c = (y % 100 == 0) ? C_YELLOW : C_GREY;
    tft.drawFastHLine(0, y, w, c);
    int ty = (y + 10 < h) ? y + 2 : y - 9;
    tft.setTextColor(c);
    tft.setCursor(2, ty);      tft.print(y);
    tft.setCursor(w - 24, ty); tft.print(y);
  }
  for (int x = 40; x < w; x += 20) {
    tft.drawFastVLine(x, 0, 6, (x % 100 == 0) ? C_YELLOW : C_GREY);
    if (x % 40 == 0) { tft.setTextColor(C_GREY); tft.setCursor(x + 2, 8); tft.print(x); }
  }
  tft.drawFastHLine(0, h - 1, w, C_RED);
  tft.drawFastVLine(w - 1, 0, h, C_RED);
  char lab[8];
  snprintf(lab, sizeof(lab), "%d", h - 1);
  smallText(w / 2 - 8, h - 10, C_RED, lab);

  char cap[40];
  snprintf(cap, sizeof(cap), "RULER rot %u  %dx%d", r, w, h);
  centerText(cap, w / 2, h / 2, &FreeSansBold12pt7b, 1, C_CYAN);

  delay(HOLD_RULER_MS);
}

// ===========================================================================
// Phase 4 — raw address-window tests
// ===========================================================================
static uint8_t fillIdx = 0;

static void printReadback(const char* what, uint16_t x, uint16_t y, uint16_t expect) {
  uint8_t rb[4];
  uint16_t got = readGram(x, y, rb);
  Serial.printf("       GRAM @(%u,%u) %s: %02X %02X %02X %02X -> 0x%04X, expected 0x%04X  %s\n",
                x, y, what, rb[0], rb[1], rb[2], rb[3], got, expect,
                (got == expect) ? "OK (write landed at this address)" :
                (allSame(rb, 4) ? "unreadable" : "MISMATCH (this address did not take the write)"));
}

// One full-frame test: MADCTL, window, fill, origin/far markers, readback.
static void rawTest(const char* id, uint8_t madctl, uint16_t x0, uint16_t x1,
                    uint16_t y0, uint16_t y1, const char* note) {
  uint16_t fill = FILL_C[fillIdx % NFILL];
  const char* fname = FILL_N[fillIdx % NFILL];
  fillIdx++;

  setMadctl(madctl);
  rawWindowFill(x0, x1, y0, y1, fill);
  rawWindowFill(x0, x0 + 29, y0, y0 + 29, C_WHITE);    // window origin
  rawWindowFill(x1 - 29, x1, y1 - 29, y1, C_YELLOW);   // window far corner

  Serial.printf("  %s MADCTL 0x%02X  CASET %u..%u  PASET %u..%u  fill %s, WHITE block at origin, YELLOW block at far corner\n",
                id, madctl, x0, x1, y0, y1, fname);
  printReadback("far corner", x1, y1, C_YELLOW);
  printReadback("bottom-middle of window", (uint16_t)((x0 + x1) / 2), y1, fill);
  Serial.printf("       %s\n", note);
  delay(HOLD_RAW_MS);
}

// Offset test: paint a known base frame first so an accepted-but-clamped
// window, a rejected window and a genuinely offset window all look different.
static void rawOffsetTest(const char* id, uint8_t madctl, bool transposed, uint16_t off) {
  uint16_t base = FILL_C[fillIdx % NFILL];
  const char* bname = FILL_N[fillIdx % NFILL];
  fillIdx++;
  uint16_t fill = FILL_C[fillIdx % NFILL];
  const char* fname = FILL_N[fillIdx % NFILL];
  fillIdx++;

  setMadctl(madctl);
  uint16_t bx1 = transposed ? 319 : 239, by1 = transposed ? 239 : 319;
  rawWindowFill(0, bx1, 0, by1, base);           // known-good full frame

  uint16_t x0, x1, y0, y1;
  if (transposed) { x0 = off; x1 = off + 319; y0 = 0;   y1 = 239; }
  else            { x0 = 0;   x1 = 239;       y0 = off; y1 = off + 319; }
  rawWindowFill(x0, x1, y0, y1, fill);
  if (transposed) {
    rawWindowFill(x0, x0 + 19, 0, 239, C_WHITE);       // first 20 columns of window
    rawWindowFill(x1 - 19, x1, 0, 239, C_YELLOW);      // last 20 columns of window
  } else {
    rawWindowFill(0, 239, y0, y0 + 19, C_WHITE);       // first 20 rows of window
    rawWindowFill(0, 239, y1 - 19, y1, C_YELLOW);      // last 20 rows of window
  }

  Serial.printf("  %s MADCTL 0x%02X  base %s over CASET 0..%u PASET 0..%u, then %s over CASET %u..%u PASET %u..%u, WHITE stripe at window start, YELLOW stripe at window end\n",
                id, madctl, bname, bx1, by1, fname, x0, x1, y0, y1);
  if (transposed) {
    printReadback("start of frame", 0, 120, base);
    printReadback("window start", x0, 120, C_WHITE);
    printReadback("window end", x1, 120, C_YELLOW);
  } else {
    printReadback("start of frame", 120, 0, base);
    printReadback("window start", 120, y0, C_WHITE);
    printReadback("window end", 120, y1, C_YELLOW);
  }
  Serial.println("       LOOK: if the YELLOW stripe reaches the physical bottom of the glass only with this offset, the panel window is offset in GRAM.");
  Serial.println("             If the whole glass turned the fill colour with no stripes, the controller rejected the out-of-range window.");
  delay(HOLD_RAW_MS);
}

static void rawPhase() {
  Serial.println();
  Serial.println("=== PHASE 4: raw CASET/PASET/RAMWR writes (library clipping and cache bypassed) ===");
  Serial.println("For every test note: (a) did the fill colour cover the WHOLE glass, (b) where the WHITE and YELLOW");
  Serial.println("blocks landed physically, (c) whether a band of the PREVIOUS colour survived and where.");
  Serial.println("'portrait window' = CASET 0..239 PASET 0..319; 'transposed window' = CASET 0..319 PASET 0..239.");
  Serial.println();

  rawTest("R01", 0x48, 0, 239, 0, 319, "Board A's mode (rot 0) + portrait window. Standard ILI9341: whole glass. If a band survives here, the controller is not in portrait addressing at MADCTL 0x48.");
  rawTest("R02", 0x48, 0, 319, 0, 239, "rot 0 + transposed window. Standard ILI9341: deliberately leaves the bottom 80 rows -> shows what the symptom looks like when caused by an addressing mismatch.");
  rawTest("R03", 0xE8, 0, 319, 0, 239, "rot 3 (the B/C/D setting) + transposed window = what fillScreen() sends at rotation 3. Standard ILI9341: whole glass, landscape. If a band survives on B/C/D, this is the cup sketch's failure reproduced raw.");
  rawTest("R04", 0xE8, 0, 239, 0, 319, "rot 3 + portrait window. Standard ILI9341: leaves 80 columns of the long axis. If THIS fills the whole glass on B/C/D, the controller is in portrait addressing at 0xE8 (MV inverted) and the fix is a driver/rotation mismatch, not an offset.");
  rawTest("R05", 0x88, 0, 239, 0, 319, "rot 2 + portrait window.");
  rawTest("R06", 0x28, 0, 319, 0, 239, "rot 1 + transposed window.");

  Serial.println();
  Serial.println("--- candidate row offsets, portrait addressing (MADCTL 0x48) ---");
  rawOffsetTest("R07", 0x48, false, 20);
  rawOffsetTest("R08", 0x48, false, 40);
  rawOffsetTest("R09", 0x48, false, 80);
  rawOffsetTest("R10", 0x48, false, 160);

  Serial.println();
  Serial.println("--- candidate offsets along the long axis, transposed addressing (MADCTL 0xE8) ---");
  rawOffsetTest("R11", 0xE8, true, 20);
  rawOffsetTest("R12", 0xE8, true, 40);
  rawOffsetTest("R13", 0xE8, true, 80);
  rawOffsetTest("R14", 0xE8, true, 160);
}

// ===========================================================================
// Phase 5 — ST7789 LCMCTRL test (gated on the ID read)
// ===========================================================================
static void lcmctrlPhase() {
  if (!(detectedST7789 || FORCE_LCMCTRL_TEST)) {
    Serial.println();
    Serial.println("=== PHASE 5 skipped: no ST7789 signature in Phase 1 (set FORCE_LCMCTRL_TEST 1 to run it anyway) ===");
    return;
  }
  Serial.println();
  Serial.println("=== PHASE 5: ST7789 LCMCTRL (0xC0) restored to datasheet default 0x2C ===");
  Serial.println("The Adafruit ILI9341 init wrote 0xC0 = 0x23 here (XMV=1, XGS=1). With 0x2C the MV bit means what the driver thinks it means.");
  uint8_t v = 0x2C;
  tft.sendCommand(0xC0, &v, 1);
  rawTest("R15", 0x48, 0, 239, 0, 319, "LCMCTRL 0x2C + rot 0 + portrait window. If this now fills the whole glass in PORTRAIT on B/C/D, the ILI9341 init's 0xC0 write is the root cause.");
  rawTest("R16", 0xE8, 0, 319, 0, 239, "LCMCTRL 0x2C + rot 3 + transposed window. Expected: whole glass, LANDSCAPE.");
  v = 0x23;
  tft.sendCommand(0xC0, &v, 1);   // back to what the library's init left behind
  Serial.println("  LCMCTRL restored to 0x23 (the value the library's init table set).");
}

// ===========================================================================
// Summary screen (loop)
// ===========================================================================
static void drawSummary(uint8_t r) {
  tft.setRotation(r);
  int w = tft.width(), h = tft.height();
  tft.fillScreen(C_BLACK);
  tft.drawRect(0, 0, w, h, C_GREY);

  char line[48], s1[16], s2[20];
  hexStr(idPre, 3, s1, sizeof(s1));
  hexStr(idPost, 3, s2, sizeof(s2));

  centerText("PANEL PROBE", w / 2, 24, &FreeSansBold12pt7b, 1, C_YELLOW);
  int y = 52;
  smallText(8, y, C_WHITE, "controller:");            y += 10;
  smallText(8, y, C_CYAN, detectedName);              y += 14;
  snprintf(line, sizeof(line), "RDDID pre-init : %s", s1);  smallText(8, y, C_WHITE, line); y += 10;
  snprintf(line, sizeof(line), "RDDID post-init: %s", s2);  smallText(8, y, C_WHITE, line); y += 10;
  hexStr(id4Post, 5, s2, sizeof(s2));
  snprintf(line, sizeof(line), "RDID4 post-init: %s", s2);  smallText(8, y, C_WHITE, line); y += 14;
  for (uint8_t i = 0; i < 4; i++) {
    snprintf(line, sizeof(line), "rot %u: lib %dx%d  MADCTL %02X  read %02X",
             i, rotW[i], rotH[i], ROT_MADCTL[i], rotMadRead[i]);
    smallText(8, y, (i == r) ? C_GREEN : C_WHITE, line);
    y += 10;
  }
  y += 6;
  snprintf(line, sizeof(line), "showing rotation %u  (%dx%d)", r, w, h);
  smallText(8, y, C_YELLOW, line); y += 10;
  smallText(8, y, C_GREY, "short-press BOOT to run the probe again");
  tft.drawRect(0, 0, w, h, C_GREY);
  tft.drawRect(1, 1, w - 2, h - 2, C_GREY);
}

// ===========================================================================
static void runProbe() {
  fillIdx = 0;
  detectedST7789 = false;
  strncpy(detectedName, "unrecognised", sizeof(detectedName));

  Serial.println();
  Serial.println("================ CYD PANEL PROBE ================");
#ifdef ESP_ARDUINO_VERSION_MAJOR
  Serial.printf("ESP32 Arduino core %d.%d.%d, SPI %lu Hz for drawing, %lu Hz for reads\n",
                ESP_ARDUINO_VERSION_MAJOR, ESP_ARDUINO_VERSION_MINOR, ESP_ARDUINO_VERSION_PATCH,
                (unsigned long)PROBE_SPI_HZ, (unsigned long)READ_SPI_HZ);
#endif

  // Phase 1a: reset by hand and read the IDs before the ILI9341 init table
  // has had a chance to rewrite anything.
  Serial.println();
  Serial.println("=== PHASE 1: controller identification ===");
  rawCommand(CMD_SWRESET, nullptr, 0);
  delay(150);
  identify("after a bare SWRESET, BEFORE tft.begin()", true);

  // Phase 1b: the exact init the cup sketch performs, then read again.
  tft.begin(PROBE_SPI_HZ);
  tft.invertDisplay(INVERT);
  poisonAddrCache();
  identify("AFTER tft.begin() (Adafruit ILI9341 init table)", false);
  if (memcmp(idPre, idPost, 3) != 0)
    Serial.println("  !! RDDID CHANGED across tft.begin(). The init table's 0xC1 write landed in an ID register:\n"
                   "     this is not an ILI9341 register map (ST7789V: 0xC1 = IDSET).");
  else
    Serial.println("  RDDID unchanged across tft.begin().");

  // Phase 2
  Serial.println();
  Serial.println("=== PHASE 2: rotation walk (border = extreme edges of what the driver believes the panel to be) ===");
  Serial.println("LOOK: which rotation shows an upright, non-mirrored number; whether the border is portrait or landscape;");
  Serial.println("      whether the border reaches every physical edge; where TL/TR/BL/BR land.");
  for (uint8_t r = 0; r < 4; r++) showRotation(r);

  // Phase 3
  Serial.println();
  Serial.println("=== PHASE 3: labelled ruler ===");
  Serial.println("LOOK: the largest y label that is actually visible on the glass in each rotation, and whether the red last row/column is there.");
  for (uint8_t r = 0; r < 4; r++) showRuler(r);

  // Phase 4 + 5
  rawPhase();
  lcmctrlPhase();

  // Back to a library-consistent state for the summary screen.
  poisonAddrCache();
  tft.setRotation(0);
  poisonAddrCache();

  Serial.println();
  Serial.println("=== PROBE DONE ===");
  Serial.println("Report back: this whole log for each board, plus photos of Phase 2 (each rotation), Phase 3 (each");
  Serial.println("rotation), and any Phase 4/5 test where the fill did NOT cover the whole glass or the blocks landed");
  Serial.println("somewhere unexpected. Short-press BOOT to run again.");
  Serial.println();
  Serial.println("How to read it:");
  Serial.println("  ID4/RDDID  00 93 41 = ILI9341   85 85 52 (or 10 85 52 after init) = ST7789V   00 93 42 = ILI9342C   00 97 96 = ST7796");
  Serial.println("  Board B/C/D showing an ST7789 signature + R04 filling the whole glass while R03 leaves a band = driver/rotation mismatch (MV inverted).");
  Serial.println("  ILI9341 signature on B/C/D + a YELLOW stripe reaching the bottom only in R07..R14 = GRAM row offset.");
  Serial.println("  ILI9341 signature + every full-frame test covering the glass = the band is not an addressing problem at all; revisit the sketch's SW/SH.");
}

// ===========================================================================
void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(LED_R, OUTPUT); digitalWrite(LED_R, HIGH);
  pinMode(LED_G, OUTPUT); digitalWrite(LED_G, HIGH);
  pinMode(LED_B, OUTPUT); digitalWrite(LED_B, HIGH);
  pinMode(BOOT_BTN, INPUT_PULLUP);

  pinMode(TFT_CS, OUTPUT); digitalWrite(TFT_CS, HIGH);
  pinMode(TFT_DC, OUTPUT); digitalWrite(TFT_DC, HIGH);
  SPI.begin(TFT_SCLK, TFT_MISO, TFT_MOSI, TFT_CS);   // same call as ddm_cup.ino
  backlightInit();

  runProbe();
}

void loop() {
  static uint8_t  shown  = 0;
  static uint32_t tShown = 0;
  static bool     down   = false;
  static uint32_t downAt = 0;
  uint32_t now = millis();

  if (now - tShown >= 4000 || tShown == 0) {
    tShown = now;
    drawSummary(shown);
    shown = (shown == 0) ? 3 : 0;
  }

  bool pressed = (digitalRead(BOOT_BTN) == LOW);
  if (pressed && !down) {
    down = true; downAt = now;
  } else if (!pressed && down) {
    down = false;
    if (now - downAt > 30) {
      runProbe();
      tShown = 0;
    }
  }
}
