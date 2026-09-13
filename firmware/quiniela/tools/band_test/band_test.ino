/*
 * band_test.ino — does the bottom quarter of the CYD follow what we draw?
 *
 * Fallback diagnostic for the "bottom band never repaints" symptom. Flash
 * like ddm_cup.ino (ESP32 Dev Module, Adafruit GFX + Adafruit ILI9341, hold
 * BOOT to upload), serial monitor at 115200, rotation 0 like the cup.
 *
 * It runs numbered steps, ~5 s each. Each step paints a colour and says on
 * serial (and in the strip along the top of the glass, which always works)
 * which colour the BOTTOM QUARTER should now be. Report, per step, the colour
 * you actually see there and whether the label text at the top is upright.
 *
 * Before every step it reads the status register (0x09) and prints whether
 * the panel is in vertical-scroll mode (bit D15). The Adafruit ILI9341 init
 * table sends a Vertical Scrolling Start Address (0x37), which switches
 * this panel into scroll mode; steps 2 and 8 send Normal Display Mode ON
 * (0x13) to leave it. If the band repaints only after those, the fix in
 * ddm_cup.ino (panelNormalMode) is the right one.
 *
 * Steps 9..12 poke LCMCTRL (0xC0). On an ST7789 that register is a set of
 * XOR flags laid over MADCTL, and the ILI9341 init table writes 0x23 into it
 * (XMV set) believing it is Power Control 1. They show whether the picture
 * goes sideways only once the panel leaves scroll mode, and whether clearing
 * XBGR puts the colour order right.
 */

#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>

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

#define HOLD_MS     5000
#define READ_SPI_HZ 2000000

Adafruit_ILI9341 tft = Adafruit_ILI9341(TFT_CS, TFT_DC, TFT_RST);

#define C_BLACK   0x0000
#define C_WHITE   0xFFFF
#define C_BLUE    0x001F
#define C_GREEN   0x07E0
#define C_RED     0xF800
#define C_YELLOW  0xFFE0
#define C_MAGENTA 0xF81F
#define C_CYAN    0x07FF
#define C_ORANGE  0xFD20
#define C_GREY    0x7BEF

// --- raw status read (own transaction, slow clock; never inside startWrite)
static void rawRead(uint8_t cmd, uint8_t* buf, uint8_t n) {
  SPI.beginTransaction(SPISettings(READ_SPI_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(TFT_CS, LOW);
  digitalWrite(TFT_DC, LOW);
  SPI.transfer(cmd);
  digitalWrite(TFT_DC, HIGH);
  for (uint8_t i = 0; i < n; i++) buf[i] = SPI.transfer(0x00);
  digitalWrite(TFT_CS, HIGH);
  SPI.endTransaction();
}

// RDDST comes back after one dummy clock, so realign by one bit and decode.
static void printStatus(const char* when) {
  uint8_t r[5];
  rawRead(0x09, r, 5);
  uint32_t st = 0;
  for (uint8_t i = 0; i < 4; i++)
    st = (st << 8) | (uint8_t)((r[i] << 1) | (r[i + 1] >> 7));
  Serial.printf("  status %s: 0x%08lX  scroll mode %s | normal mode %s | partial %s | display %s | MADCTL MY%u MX%u MV%u\n",
                when, (unsigned long)st,
                (st & (1UL << 15)) ? "ON " : "off",
                (st & (1UL << 16)) ? "on" : "OFF",
                (st & (1UL << 18)) ? "ON" : "off",
                (st & (1UL << 10)) ? "on" : "OFF",
                (unsigned)((st >> 30) & 1), (unsigned)((st >> 29) & 1), (unsigned)((st >> 28) & 1));
}

static void normalMode() {
  static const uint8_t vscrdef[6] = { 0x00, 0x00, 0x01, 0x40, 0x00, 0x00 };
  static const uint8_t vscsad[2]  = { 0x00, 0x00 };
  tft.sendCommand(0x33, vscrdef, 6);
  tft.sendCommand(0x37, vscsad, 2);
  tft.sendCommand(0x13);
}

// LCMCTRL (0xC0) on an ST7789: XOR flags applied on top of MADCTL. Bit 5 XBGR
// flips the colour order, bit 1 XMV inverts the MV (row/column exchange) bit,
// bit 0 XGS flips the gate scan direction. The Adafruit ILI9341 init table
// writes 0x23 = XBGR | XMV | XGS here, thinking it is Power Control 1.
static void lcmctrl(uint8_t v) {
  tft.sendCommand(0xC0, &v, 1);
  Serial.printf("  LCMCTRL (0xC0) <- 0x%02X\n", v);
}

// Label strip at the very top: rows 0..23 always repaint, so this is readable
// whatever the bottom does.
static void label(uint8_t step, const char* colour) {
  tft.fillRect(0, 0, tft.width(), 24, C_BLACK);
  tft.setFont(nullptr);
  tft.setTextSize(2);
  tft.setTextColor(C_WHITE);
  tft.setCursor(4, 4);
  tft.printf("%u: bottom=%s?", step, colour);
}

static void ask(uint8_t step, const char* colour, const char* what) {
  Serial.printf("STEP %u  %s\n", step, what);
  Serial.printf("  LOOK: is the bottom quarter of the glass now %s? Name the colour you actually see.\n", colour);
  Serial.println("  LOOK: is the label text along the TOP edge, upright and readable?");
  label(step, colour);
  delay(HOLD_MS);
}

static void rowByRow(uint16_t colour) {
  tft.startWrite();
  for (int y = 0; y < tft.height(); y++) {
    tft.setAddrWindow(0, y, tft.width(), 1);
    tft.writeColor(colour, tft.width());
  }
  tft.endWrite();
}

static void runTest() {
  Serial.println();
  Serial.println("================ CYD BAND TEST ================");

  tft.begin();                 // exactly what ddm_cup.ino does
  tft.setRotation(0);
  tft.invertDisplay(false);
  printStatus("after tft.begin()");

  tft.fillScreen(C_BLUE);
  ask(1, "BLUE", "fillScreen(BLUE) straight after the driver init, no fix. Expected on an affected board: bottom quarter NOT blue.");

  normalMode();
  printStatus("after NORON");
  tft.fillScreen(C_GREEN);
  ask(2, "GREEN", "sent scroll area = full panel + Normal Display Mode ON, then fillScreen(GREEN). If the band is green now, the fix works.");

  rowByRow(C_MAGENTA);
  ask(3, "MAGENTA", "320 single-row window writes (the raw loop from the bench notes, done inside startWrite).");

  tft.fillRect(0, 240, 240, 80, C_CYAN);
  ask(4, "CYAN", "one window covering only rows 240..319.");

  tft.fillRect(0, 160, 240, 160, C_YELLOW);
  ask(5, "YELLOW", "one window crossing row 240 (rows 160..319).");

  static const uint8_t vscsad[2] = { 0x00, 0x00 };
  tft.sendCommand(0x37, vscsad, 2);   // back into scroll mode on purpose
  printStatus("after re-sending 0x37");
  tft.fillScreen(C_RED);
  ask(6, "RED", "re-sent the scroll start command (scroll mode on again), then fillScreen(RED). Expected on an affected board: band NOT red.");

  tft.begin();                        // the library init again: it sends 0x37 itself
  tft.setRotation(0);
  tft.invertDisplay(false);
  printStatus("after a second tft.begin()");
  tft.fillScreen(C_ORANGE);
  ask(7, "ORANGE", "ran tft.begin() again (its init table sends 0x37), then fillScreen(ORANGE). Expected on an affected board: band NOT orange.");

  normalMode();
  printStatus("after NORON again");
  tft.fillScreen(C_WHITE);
  ask(8, "WHITE", "Normal Display Mode ON again, then fillScreen(WHITE). Expected: band white.");

  // --- LCMCTRL trials, all in normal mode ------------------------------------
  // Board A, 2026-09-12: with the init's 0x23 the picture goes sideways once
  // scroll mode ends (XMV); with XMV cleared it is flipped top-to-bottom
  // (ddm_cup.ino corrects that with MADCTL MY|MX); with XBGR cleared red is
  // red again. Step 12 asks whether XGS is the remaining flip.
  lcmctrl(0x21);
  tft.fillScreen(C_BLUE);
  ask(9, "BLUE", "LCMCTRL 0x21 (the init's 0x23 minus XMV), then fillScreen(BLUE). Expected: band blue, picture flipped top-to-bottom, so the label sits along the BOTTOM edge upside down.");

  lcmctrl(0x23);
  tft.fillScreen(C_GREEN);
  ask(10, "GREEN", "LCMCTRL back to the init's 0x23, then fillScreen(GREEN). Expected: sideways again. If 9 was flipped but not sideways and this is sideways, XMV is the rotation culprit.");

  lcmctrl(0x01);
  tft.fillScreen(C_RED);
  ask(11, "RED", "LCMCTRL 0x01 (XMV and XBGR cleared), what ddm_cup.ino sets, then fillScreen(RED). Expected: still flipped top-to-bottom, band red, and the fill really RED now, not blue.");

  lcmctrl(0x00);
  tft.fillScreen(C_YELLOW);
  ask(12, "YELLOW", "LCMCTRL 0x00 (no XOR flags at all) with the library's own rotation-0 MADCTL, then fillScreen(YELLOW). If the label is now upright along the TOP, not mirrored, and the fill is yellow, XGS was the last flip and the glass runs on plain ILI9341 settings.");

  lcmctrl(0x01);                      // leave the panel the way ddm_cup.ino runs it

  Serial.println("=== BAND TEST DONE — for each of steps 1..12 report the colour of the bottom quarter and whether the label text is upright. Short-press BOOT to run again. ===");
  tft.fillRect(0, 0, tft.width(), 24, C_BLACK);
  tft.setCursor(4, 4);
  tft.setTextColor(C_GREY);
  tft.print("done - BOOT reruns");
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(LED_R, OUTPUT); digitalWrite(LED_R, HIGH);
  pinMode(LED_G, OUTPUT); digitalWrite(LED_G, HIGH);
  pinMode(LED_B, OUTPUT); digitalWrite(LED_B, HIGH);
  pinMode(BOOT_BTN, INPUT_PULLUP);
  pinMode(TFT_CS, OUTPUT); digitalWrite(TFT_CS, HIGH);
  pinMode(TFT_DC, OUTPUT); digitalWrite(TFT_DC, HIGH);
  SPI.begin(TFT_SCLK, TFT_MISO, TFT_MOSI, TFT_CS);
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(TFT_BL, 5000, 8);
  ledcWrite(TFT_BL, 255);
#else
  ledcSetup(0, 5000, 8);
  ledcAttachPin(TFT_BL, 0);
  ledcWrite(0, 255);
#endif
  runTest();
}

void loop() {
  static bool down = false;
  static uint32_t downAt = 0;
  uint32_t now = millis();
  bool pressed = (digitalRead(BOOT_BTN) == LOW);
  if (pressed && !down) { down = true; downAt = now; }
  else if (!pressed && down) {
    down = false;
    if (now - downAt > 30) runTest();
  }
}
