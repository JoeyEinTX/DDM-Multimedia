/*
 * DDM La Quiniela — HX711 calibration sketch
 *
 * Standalone. No ESP-NOW, no display drawing beyond a status line.
 * Streams raw HX711 counts over serial and walks through a token calibration.
 *
 * WIRING (HX711 -> CN1 on the CYD):
 *     VCC -> 3.3V     GND -> GND     DT -> IO27     SCK -> IO22
 *
 * LIBRARY:  "HX711 Arduino Library" by Bogdan Necula (bogde/HX711)
 *           Tools -> Manage Libraries -> search "HX711" -> author bogde
 *
 * BOARD:    ESP32 Dev Module.  Serial Monitor at 115200.
 *
 * COMMANDS (type in Serial Monitor, newline-terminated):
 *     t         tare (zero) with the plate empty
 *     c         start token calibration walk
 *     +         (during calibration) "I just added one token"
 *     d         finish calibration, print results
 *     r         toggle raw streaming on/off
 *     s         print current stats without streaming
 *     ?         help
 *
 * TYPICAL SESSION:
 *     1. Let it sit 60s after power-up (HX711 warms up, drift settles)
 *     2. Type  t   with the plate empty
 *     3. Type  c   then drop one token, type  +  and wait for the "captured"
 *        line. Repeat for 8-10 tokens.
 *     4. Type  d   — it prints counts-per-token, the spread, and the
 *        constant to paste into ddm_cup.ino.
 *
 * MEASURED 2026-09 with the current token print (ten tokens, sd 102 = 1.6%):
 *     #define COUNTS_PER_TOKEN   6212L
 *     #define TOKEN_THRESHOLD    3106L
 * Those are what ddm_cup.ino carries. Only re-run this if the print changes.
 */

#include <Arduino.h>
#include <HX711.h>

#define HX711_DT   27
#define HX711_SCK  22

// How many samples to average per reading. The HX711 runs at ~10 SPS in
// its default jumper setting, so 10 samples is about one second.
#define AVG_SAMPLES   10

HX711 scale;

long     tareOffset   = 0;
bool     streaming    = true;
uint32_t lastStream   = 0;

// calibration walk
bool     calibrating  = false;
int      calCount     = 0;
long     calSteps[32];
long     calLastBase  = 0;

// rolling noise estimate: last N raw readings
#define NOISE_N 20
long     ring[NOISE_N];
int      ringIdx = 0;
bool     ringFull = false;

void pushRing(long v) {
  ring[ringIdx] = v;
  ringIdx = (ringIdx + 1) % NOISE_N;
  if (ringIdx == 0) ringFull = true;
}

void ringStats(long &mn, long &mx, float &sd) {
  int n = ringFull ? NOISE_N : ringIdx;
  if (n < 2) { mn = mx = 0; sd = 0; return; }
  mn = ring[0]; mx = ring[0];
  double sum = 0;
  for (int i = 0; i < n; i++) {
    if (ring[i] < mn) mn = ring[i];
    if (ring[i] > mx) mx = ring[i];
    sum += ring[i];
  }
  double mean = sum / n, ss = 0;
  for (int i = 0; i < n; i++) { double d = ring[i] - mean; ss += d * d; }
  sd = sqrt(ss / (n - 1));
}

long readAvg() {
  return scale.read_average(AVG_SAMPLES);
}

void doTare() {
  Serial.println("\n[tare] hold still, averaging 30 samples...");
  tareOffset = scale.read_average(30);
  ringIdx = 0; ringFull = false;
  Serial.printf("[tare] offset = %ld  (this is 'empty')\n\n", tareOffset);
}

void printHelp() {
  Serial.println(
    "\n  t  tare with plate empty\n"
    "  c  start token calibration\n"
    "  +  I just added one token (during calibration)\n"
    "  d  done calibrating, print results\n"
    "  r  toggle raw streaming\n"
    "  s  stats snapshot\n"
    "  ?  this help\n");
}

void startCal() {
  calibrating = true;
  calCount    = 0;
  calLastBase = readAvg() - tareOffset;
  Serial.printf("\n[cal] started. baseline = %ld counts above tare.\n", calLastBase);
  Serial.println("[cal] drop ONE token, wait a beat, then type  +\n");
}

void calStep() {
  if (!calibrating) { Serial.println("[cal] not calibrating — type  c  first"); return; }
  if (calCount >= 32) { Serial.println("[cal] buffer full, type  d"); return; }

  Serial.println("[cal] settling...");
  delay(3000);   // a token lands ~3% high and settles over about a second; 800 ms was too short
  long now  = readAvg() - tareOffset;
  long step = now - calLastBase;
  calSteps[calCount++] = step;
  calLastBase = now;
  Serial.printf("[cal] token %2d  step = %+ld counts   total = %ld\n", calCount, step, now);
}

void finishCal() {
  calibrating = false;
  if (calCount < 2) { Serial.println("[cal] need at least 2 tokens"); return; }

  double sum = 0;
  long mn = calSteps[0], mx = calSteps[0];
  for (int i = 0; i < calCount; i++) {
    sum += calSteps[i];
    if (calSteps[i] < mn) mn = calSteps[i];
    if (calSteps[i] > mx) mx = calSteps[i];
  }
  double mean = sum / calCount, ss = 0;
  for (int i = 0; i < calCount; i++) { double d = calSteps[i] - mean; ss += d * d; }
  double sd = sqrt(ss / (calCount - 1));

  long nmn, nmx; float nsd;
  ringStats(nmn, nmx, nsd);

  Serial.println("\n================ CALIBRATION RESULT ================");
  Serial.printf("tokens measured      : %d\n", calCount);
  Serial.printf("counts per token     : %.0f   (mean)\n", mean);
  Serial.printf("token spread         : %ld .. %ld   sd %.0f  (%.1f%% of a token)\n",
                mn, mx, sd, 100.0 * sd / mean);
  Serial.printf("idle noise (avg'd)   : sd %.0f counts  (%.1f%% of a token)\n",
                nsd, 100.0 * nsd / mean);
  Serial.printf("signal / noise       : %.0f : 1\n", mean / (nsd > 0 ? nsd : 1));
  Serial.println("----------------------------------------------------");
  Serial.println("paste into ddm_cup.ino:");
  Serial.printf("#define COUNTS_PER_TOKEN   %ldL\n", (long)llround(mean));
  Serial.printf("#define TOKEN_THRESHOLD    %ldL   // half a token\n", (long)llround(mean * 0.5));
  Serial.println("====================================================\n");

  if (sd / mean > 0.10)
    Serial.println("NOTE: token-to-token spread is over 10%. Check token print consistency\n"
                   "      and that the stack isn't touching the cup wall.\n");
  if (nsd / mean > 0.05)
    Serial.println("NOTE: idle noise is over 5% of a token. Check wiring, keep the cell\n"
                   "      leads short, and make sure nothing is vibrating the bench.\n");
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nDDM HX711 calibration");

  scale.begin(HX711_DT, HX711_SCK);
  scale.set_gain(128);

  Serial.print("waiting for HX711");
  uint32_t t0 = millis();
  while (!scale.is_ready()) {
    delay(100);
    Serial.print('.');
    if (millis() - t0 > 5000) {
      Serial.println("\n\n!! HX711 not responding. Check DT->27, SCK->22, VCC->3.3V, GND.");
      while (1) delay(1000);
    }
  }
  Serial.println(" ok");

  Serial.println("let it warm up ~60s before taring. streaming raw counts.");
  printHelp();
  tareOffset = scale.read_average(10);
}

void loop() {
  // ---- serial commands ----------------------------------------------------
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r' || c == '\n' || c == ' ') continue;
    switch (c) {
      case 't': doTare(); break;
      case 'c': startCal(); break;
      case '+': calStep(); break;
      case 'd': finishCal(); break;
      case 'r': streaming = !streaming;
                Serial.println(streaming ? "[stream on]" : "[stream off]"); break;
      case 's': {
        long mn, mx; float sd;
        ringStats(mn, mx, sd);
        Serial.printf("[stats] tare=%ld  ring n=%d  min=%ld max=%ld  sd=%.0f\n",
                      tareOffset, ringFull ? NOISE_N : ringIdx, mn, mx, sd);
        break;
      }
      case '?': printHelp(); break;
      default:  break;
    }
  }

  // ---- streaming ----------------------------------------------------------
  if (millis() - lastStream >= 1000) {
    lastStream = millis();
    if (scale.is_ready()) {
      long raw = readAvg();
      long net = raw - tareOffset;
      pushRing(net);

      if (streaming) {
        long mn, mx; float sd;
        ringStats(mn, mx, sd);
        Serial.printf("raw %8ld   net %+8ld   noise sd %5.0f   p-p %5ld%s\n",
                      raw, net, sd, mx - mn, calibrating ? "   [CAL]" : "");
      }
    }
  }
}
