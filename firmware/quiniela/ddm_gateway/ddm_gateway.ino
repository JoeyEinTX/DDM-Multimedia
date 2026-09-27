/*
 * ddm_gateway.ino — La Quiniela ESP-NOW gateway (protocol v2)
 *
 * Plain ESP32 WROOM-32 dev board. No display. USB serial to DevPi, or to a
 * laptop for bench work.
 *
 * Role: the bridge between the cups (ESP-NOW) and DevPi (USB serial).
 *   - Broadcasts the shared DdmStatePacket to every cup twice a second, once
 *     something has told it what to broadcast (see "Boot behaviour" below).
 *     The packet is keyed by horse number: race state, scratched bits,
 *     renumber pairs and the three results. No cup IDs, no MAC table.
 *   - Hears every cup that talks (HELLO or telemetry, same payload), keeps a
 *     small table of them (MAC, the horse the cup says it is, tokens, signal,
 *     age) and reports every packet up the serial line as one JSON object
 *     per line. A cup is a cup: nothing is gated, nothing is assigned, and
 *     the table's indexing never leaves this sketch.
 *   - Accepts full state snapshots and a debug toggle from DevPi as JSON
 *     lines.
 *
 * Serial line protocol: 115200 8N1, one compact JSON object per line, at
 * most DDM_LINE_MAX bytes per line down and for most lines up, with one
 * exception: the lines that carry the cup table (`status` every 5 s and the
 * up `state` snapshot typed `json`) run to about 2.3 KB with a full table
 * and have their own buffer, BIG_LINE_MAX. Documented in ../README.md
 * ("Serial line protocol"); the key names are a contract with the DevPi
 * bridge, implement them exactly.
 *   up:   hello, status, telem, err, state (only when asked: `json`)
 *   down: state, debug
 * The up and down "state" lines share a type name and nothing else: the
 * down line is DevPi's snapshot for the gateway to apply, the up line is
 * the gateway's report. This sketch never parses its own output.
 * Every line this sketch prints that is not JSON starts with "# " so DevPi
 * can drop it. The hand-typed bench commands still work: type `help`.
 *
 * Boot behaviour: a default build (DDM_AUTO_DEMO 0) is silent. It sends
 * hello and status, receives and reports cup traffic, but broadcasts no
 * state at all until a JSON state line arrives or a person types
 * state/scratch/renum/results/demo. Cups show their own number meanwhile:
 * the number lives on the cup, not here.
 *
 * ESP-NOW: the broadcast address is the only peer, ever. The state packet
 * is one frame for all cups; receiving needs no peer entry. Nothing is
 * unicast from here and no peer is added or removed at runtime, so the
 * fleet size is bounded by DDM_MAX_CUPS (the table), not by ESP-NOW's peer
 * limit. The receive callback only queues; every byte of serial output
 * comes from loop().
 *
 * No WiFi association, no AP, no MQTT, no OTA. ESP-NOW only, pinned to
 * DDM_ESPNOW_CHANNEL from ddm_common.h (symlinked into this folder — see
 * ../README.md).
 *
 * LIBRARIES (Tools -> Manage Libraries): ArduinoJson 7.x
 *
 * BOARD: ESP32 Dev Module
 */

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_idf_version.h>
#include <ArduinoJson.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <stdarg.h>
#include <limits.h>
#include "ddm_common.h"

// ===========================================================================
// Build-time switches
// ===========================================================================

// Version of the serial line protocol, reported as "v" in the hello line.
// Bump it when a line's keys or their meaning change. 2 = protocol v2: telem
// keyed by MAC and horse, status carrying the cup table, no roster line, the
// down state line keyed by horse.
#define DDM_LINE_PROTO_VERSION 2

// Boot default of the human-readable output: the per-packet TELEM lines and
// the 5-second summary table. 0 = JSON lines only. Flip it at runtime with
// the JSON line {"t":"debug","on":true} or the typed command `debug on`.
// JSON lines are emitted whatever this says. (The table also pauses while
// `json 1` runs: the state line carries the same numbers every second.)
#define DDM_DEBUG_TEXT 0

// 0 = party build: the gateway broadcasts NOTHING over ESP-NOW until a valid
//     JSON state line arrives or a person types state/scratch/renum/results/
//     demo. There is no timeout and no fallback. A power blip reboots the
//     gateway in a second and DevPi in a minute; for that minute the cups
//     keep showing their own number and must not see a demo walk.
// 1 = bench build: boot straight into demo mode and broadcast at once. A
//     JSON state line still takes over.
// The value is printed in the boot banner so a bench build left on the party
// gateway is obvious in any serial log.
#define DDM_AUTO_DEMO 0

// ---------------------------------------------------------------------------
// Timing and sizes
// ---------------------------------------------------------------------------
#define BROADCAST_MS   500     // state broadcast cadence
#define DEMO_STEP_MS  3000     // demo results walk cadence
#define STALE_MS      3000     // silent longer than this -> STALE in the table
#define FORGET_MS   600000     // silent longer than this -> dropped from the table (10 min)
#define SUMMARY_MS    5000     // debug summary table cadence
#define STATUS_MS     5000     // JSON status heartbeat cadence
#define HELLO_MS      2000     // JSON hello repeat cadence until the first state line
#define DDM_LINE_MAX      1024     // longest serial line down, and up for every line but the two below
#define STATE_LINE_MS   1000     // `json 1` state snapshot cadence
#define BIG_LINE_MAX    2560     // the status and up state lines: ~110-byte head + DDM_MAX_CUPS entries
                                 // of at most 90 bytes + "]}" = 2272 worst case (+ NUL), see cupsJson()
#define RX_QUEUE_LEN    32     // ESP-NOW packets that can wait for loop()
#define ERR_EXCERPT     40     // characters of a rejected line echoed in the err line
#define FULL_TABLE_LOG_MS 5000 // "table full" complaint at most this often

static const uint8_t BCAST[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

// ---------------------------------------------------------------------------
// The cup table — every MAC heard from, in the order it was first heard.
// Internal only: the index is never printed or sent.
// ---------------------------------------------------------------------------
struct CupTrack {
  bool     used;
  uint8_t  mac[6];
  uint8_t  horse;        // the horse the cup last claimed, 0 = none set
  uint32_t lastSeenMs;   // millis() of last packet
  uint32_t lastSeq;      // last state seq the cup reported seeing
  uint32_t dropped;      // cup-reported drop count
  int8_t   rssi;         // cup-reported RSSI of gateway->cup packets (downlink)
  int8_t   upRssi;       // gateway-measured RSSI of cup->gateway packets (uplink)
  uint16_t tokenCount;
  bool     hello;        // the last packet was a HELLO (the cup has no gateway MAC yet)
};

CupTrack cups[DDM_MAX_CUPS];

// The packet we broadcast. Mutated by state lines, serial commands and demo mode.
DdmStatePacket statePkt;

uint32_t versionRejects = 0;                     // packets with the wrong DDM_PROTO_VERSION
uint32_t tableFull      = 0;                     // packets from a 25th cup while every slot was fresh
bool     demoMode       = (DDM_AUTO_DEMO != 0);
bool     broadcasting   = (DDM_AUTO_DEMO != 0);  // once true, stays true until reboot
bool     debugText      = (DDM_DEBUG_TEXT != 0);
bool     jsonAuto       = false;                 // `json 1`: a state line every STATE_LINE_MS and no summary table; boot default off
bool     helloActive    = true;                  // hello repeats until the first valid state line
uint32_t stateRev       = 0;                     // rev of the last applied state line, 0 = none yet
uint32_t demoStep       = 0;
char     gwMac[18];                              // this board's MAC, formatted once at boot

uint32_t tBroadcast = 0, tDemo = 0, tSummary = 0, tStatus = 0, tHello = 0, tState = 0, tFullLog = 0;

// ---------------------------------------------------------------------------
// ESP-NOW -> loop() handoff. The receive callback runs in the WiFi task; it
// copies the packet into this queue and does nothing else. loop() drains the
// queue and does all the work, so every byte of serial output comes from
// loop() and lines can never interleave.
// ---------------------------------------------------------------------------
struct RxItem {
  uint8_t mac[6];
  int8_t  rssi;                                  // gateway-side RSSI of this packet
  uint8_t len;                                   // real length; data holds at most sizeof(DdmTelemetryPacket)
  uint8_t data[sizeof(DdmTelemetryPacket)];
};

static QueueHandle_t     rxQueue      = nullptr;
static volatile uint32_t rxQueueDrops = 0;       // packets lost because loop() fell behind

static char outBuf[DDM_LINE_MAX + 1];            // JSON lines are built here, from loop() only
static char bigBuf[BIG_LINE_MAX + 1];            // except status and the up state line, which carry the table

// ===========================================================================
// Serial output. Everything leaves through these two, from loop() (or from
// setup(), before loop() starts); never from the ESP-NOW callback.
// ===========================================================================

// One finished line, "\n"-terminated (not println(): that adds "\r\n").
static void outLine(const char* s) {
  Serial.write((const uint8_t*)s, strlen(s));
  Serial.write('\n');
}

// One human-readable line: "# " prefix, printf-style body, "\n". Any line
// that does not start with "{" is not protocol and DevPi drops it, so every
// non-JSON line goes through here.
static void textf(const char* fmt, ...) {
  char b[256];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(b, sizeof(b), fmt, ap);
  va_end(ap);
  if (n < 0) return;
  if (n > (int)sizeof(b) - 1) n = (int)sizeof(b) - 1;
  Serial.write((const uint8_t*)"# ", 2);
  Serial.write((const uint8_t*)b, n);
  Serial.write('\n');
}

// ---------------------------------------------------------------------------
// MAC helper
// ---------------------------------------------------------------------------
static void macFmt(const uint8_t* m, char* out /* >= 18 bytes */) {
  snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
           m[0], m[1], m[2], m[3], m[4], m[5]);
}

// ---------------------------------------------------------------------------
// Cup table helpers. Indices are internal; -1 = not tracked.
// ---------------------------------------------------------------------------
static int findCup(const uint8_t* mac) {
  for (int i = 0; i < DDM_MAX_CUPS; i++)
    if (cups[i].used && memcmp(cups[i].mac, mac, 6) == 0) return i;
  return -1;
}

// The entry for a MAC, made if needed: a free slot first, else the slot of
// the cup that has been silent longest, provided it is STALE (a fleet of 24
// fresh cups plus a 25th is the one case that is refused, and counted).
static int trackCup(const uint8_t* mac, uint32_t now) {
  int i = findCup(mac);
  if (i >= 0) return i;
  int stalest = -1;
  uint32_t stalestAge = 0;
  for (i = 0; i < DDM_MAX_CUPS; i++) {
    if (!cups[i].used) { stalest = i; stalestAge = 0xFFFFFFFFu; break; }
    uint32_t age = now - cups[i].lastSeenMs;
    if (age > stalestAge) { stalestAge = age; stalest = i; }
  }
  if (stalest < 0 || (cups[stalest].used && stalestAge <= STALE_MS)) {
    tableFull++;
    if (now - tFullLog >= FULL_TABLE_LOG_MS) {
      tFullLog = now;
      char m[18]; macFmt(mac, m);
      textf("ERR cup table full (%d fresh cups), %s ignored", DDM_MAX_CUPS, m);
    }
    return -1;
  }
  cups[stalest] = {};
  cups[stalest].used = true;
  memcpy(cups[stalest].mac, mac, 6);
  return stalest;
}

// Drop entries that have been silent for FORGET_MS, so a cup that went home
// does not sit in every status line with a growing age.
static void forgetStale(uint32_t now) {
  for (int i = 0; i < DDM_MAX_CUPS; i++)
    if (cups[i].used && now - cups[i].lastSeenMs > FORGET_MS) {
      if (debugText) { char m[18]; macFmt(cups[i].mac, m); textf("cup %s silent %lu s, forgotten", m, (unsigned long)(FORGET_MS / 1000)); }
      cups[i] = {};
    }
}

// Cups heard from within STALE_MS: the summary table's OK count.
static int cupsHeard(uint32_t now) {
  int n = 0;
  for (int i = 0; i < DDM_MAX_CUPS; i++)
    if (cups[i].used && now - cups[i].lastSeenMs <= STALE_MS) n++;
  return n;
}

// ===========================================================================
// Up: JSON lines to DevPi, built with snprintf. Key names are the contract.
// ===========================================================================
static void emitHello() {
  snprintf(outBuf, sizeof(outBuf), "{\"t\":\"hello\",\"v\":%d,\"proto\":%d,\"mac\":\"%s\"}",
           DDM_LINE_PROTO_VERSION, DDM_PROTO_VERSION, gwMac);
  outLine(outBuf);
}

// The cup table as a JSON array, appended at buf + n. Returns the new n, or
// -1 if it would not fit (the caller then prints nothing: never a cut line).
// One entry: {"mac":"A0:B7:65:12:34:56","horse":7,"tok":23,"rssi":-63,"up":-61,"age":180}
static int cupsJson(char* buf, int size, int n, uint32_t now) {
  int k = snprintf(buf + n, size - n, "[");
  if (k < 0 || n + k >= size) return -1;
  n += k;
  bool first = true;
  for (int i = 0; i < DDM_MAX_CUPS; i++) {
    const CupTrack& c = cups[i];
    if (!c.used) continue;
    char m[18]; macFmt(c.mac, m);
    k = snprintf(buf + n, size - n,
                 "%s{\"mac\":\"%s\",\"horse\":%u,\"tok\":%u,\"rssi\":%d,\"up\":%d,\"age\":%lu}",
                 first ? "" : ",", m, (unsigned)c.horse, (unsigned)c.tokenCount,
                 (int)c.rssi, (int)c.upRssi, (unsigned long)(now - c.lastSeenMs));
    if (k < 0 || n + k >= size - 3) return -1;
    n += k;
    first = false;
  }
  k = snprintf(buf + n, size - n, "]");
  if (k < 0 || n + k >= size) return -1;
  return n + k;
}

// The scratched bits as a JSON array of horse numbers, the renum pairs as an
// array of [from,to], the results as [w,p,s]. Appended at buf + n; -1 if it
// would not fit.
static int packetJson(char* buf, int size, int n) {
  int k = snprintf(buf + n, size - n, "\"st\":%u,\"scr\":[", (unsigned)statePkt.raceState);
  if (k < 0 || n + k >= size) return -1;
  n += k;
  bool first = true;
  for (int h = 1; h <= DDM_MAX_HORSE; h++) {
    if (!ddmIsScratched(statePkt.scratched, (uint8_t)h)) continue;
    k = snprintf(buf + n, size - n, "%s%d", first ? "" : ",", h);
    if (k < 0 || n + k >= size) return -1;
    n += k;
    first = false;
  }
  k = snprintf(buf + n, size - n, "],\"renum\":[");
  if (k < 0 || n + k >= size) return -1;
  n += k;
  first = true;
  for (int i = 0; i < DDM_RENUM_SLOTS; i++) {
    if (statePkt.renum[i][0] == 0) continue;
    k = snprintf(buf + n, size - n, "%s[%u,%u]", first ? "" : ",",
                 (unsigned)statePkt.renum[i][0], (unsigned)statePkt.renum[i][1]);
    if (k < 0 || n + k >= size) return -1;
    n += k;
    first = false;
  }
  k = snprintf(buf + n, size - n, "],\"res\":[%u,%u,%u]",
               (unsigned)statePkt.results[0], (unsigned)statePkt.results[1], (unsigned)statePkt.results[2]);
  if (k < 0 || n + k >= size) return -1;
  return n + k;
}

// Heartbeat every STATUS_MS, and the acknowledgement of every applied
// state/debug line. There is no separate ack line. Carries the cup table.
static void emitStatus() {
  uint32_t now = millis();
  int n = snprintf(bigBuf, sizeof(bigBuf),
                   "{\"t\":\"status\",\"gseq\":%lu,\"phase\":%u,\"state_rev\":%lu,\"cups\":",
                   (unsigned long)statePkt.seq, (unsigned)statePkt.raceState, (unsigned long)stateRev);
  if (n < 0 || n >= (int)sizeof(bigBuf)) return;
  n = cupsJson(bigBuf, sizeof(bigBuf), n, now);
  if (n < 0) { textf("ERR status line over %d bytes, not sent", BIG_LINE_MAX); return; }
  int k = snprintf(bigBuf + n, sizeof(bigBuf) - n, ",\"rejects\":%lu,\"up_s\":%lu}",
                   (unsigned long)versionRejects, (unsigned long)(now / 1000));
  if (k < 0 || n + k >= (int)sizeof(bigBuf)) { textf("ERR status line over %d bytes, not sent", BIG_LINE_MAX); return; }
  outLine(bigBuf);
  tStatus = now;
}

// One per packet from a cup, HELLO or telemetry alike. "horse" is what the
// cup says it is (0 = none set yet); "hello":1 marks a HELLO (the cup is
// still broadcasting, it has no gateway MAC yet).
static void emitTelem(const char* mac, const DdmTelemetryPacket* p, int8_t upRssi, bool hello) {
  int n = snprintf(outBuf, sizeof(outBuf),
                   "{\"t\":\"telem\",\"mac\":\"%s\",\"horse\":%u,\"raw\":%ld,\"count\":%u,"
                   "\"seq\":%lu,\"drop\":%lu,\"rssi\":%d,\"up\":%d",
                   mac, (unsigned)p->horse, (long)p->rawWeight, (unsigned)p->tokenCount,
                   (unsigned long)p->seq, (unsigned long)p->dropped, (int)p->rssi, (int)upRssi);
  if (n < 0 || n >= (int)sizeof(outBuf) - 16) return;
  if (hello) n += snprintf(outBuf + n, sizeof(outBuf) - n, ",\"hello\":1");
  snprintf(outBuf + n, sizeof(outBuf) - n, "}");
  outLine(outBuf);
}

// First ERR_EXCERPT characters of a rejected line, made safe for a JSON
// string: '"' and '\' escaped, control characters dropped, bytes >= 0x80
// written as \u00XX so the err line is always plain-ASCII JSON.
static void jsonExcerpt(const char* line, char* out, size_t outSz) {
  size_t o = 0;
  for (int i = 0; i < ERR_EXCERPT && line[i]; i++) {
    unsigned char c = (unsigned char)line[i];
    char piece[8];
    if (c == '"')                   { piece[0] = '\\'; piece[1] = '"';  piece[2] = 0; }
    else if (c == '\\')             { piece[0] = '\\'; piece[1] = '\\'; piece[2] = 0; }
    else if (c < 0x20 || c == 0x7F) continue;
    else if (c >= 0x80)             snprintf(piece, sizeof(piece), "\\u%04X", (unsigned)c);
    else                            { piece[0] = (char)c; piece[1] = 0; }
    size_t l = strlen(piece);
    if (o + l >= outSz) break;
    memcpy(out + o, piece, l);
    o += l;
  }
  out[o] = 0;
}

// A rejected downlink line. msg: "parse" (not JSON), "invalid" (JSON that
// failed validation), "overflow" (longer than DDM_LINE_MAX; no excerpt).
static void emitErr(const char* msg, const char* line) {
  if (line == nullptr) {
    snprintf(outBuf, sizeof(outBuf), "{\"t\":\"err\",\"msg\":\"%s\"}", msg);
  } else {
    char ex[256];
    jsonExcerpt(line, ex, sizeof(ex));
    snprintf(outBuf, sizeof(outBuf), "{\"t\":\"err\",\"msg\":\"%s\",\"line\":\"%s\"}", msg, ex);
  }
  outLine(outBuf);
}

// The whole gateway in one line, for a machine reader: typed `json` prints
// one, `json 1` one every STATE_LINE_MS. Not the down-link {"t":"state"}
// (jsonState below): same type name, opposite direction, different keys.
// The broadcast packet's contents (st, scr, renum, res) and the cup table.
static void emitState() {
  uint32_t now = millis();
  int n = snprintf(bigBuf, sizeof(bigBuf),
                   "{\"t\":\"state\",\"seq\":%lu,\"demo\":%d,\"mac\":\"%s\",",
                   (unsigned long)statePkt.seq, demoMode ? 1 : 0, gwMac);
  if (n < 0 || n >= (int)sizeof(bigBuf)) return;
  n = packetJson(bigBuf, sizeof(bigBuf), n);
  if (n >= 0) {
    int k = snprintf(bigBuf + n, sizeof(bigBuf) - n, ",\"cups\":");
    n = (k < 0 || n + k >= (int)sizeof(bigBuf)) ? -1 : n + k;
  }
  if (n >= 0) n = cupsJson(bigBuf, sizeof(bigBuf), n, now);
  if (n < 0 || n + 2 >= (int)sizeof(bigBuf)) { textf("ERR state line over %d bytes, not sent", BIG_LINE_MAX); return; }
  snprintf(bigBuf + n, sizeof(bigBuf) - n, "}");
  outLine(bigBuf);
  tState = now;
}

// ===========================================================================
// Receive path. Core 3.x hands us esp_now_recv_info_t (with per-packet RSSI);
// core 2.x hands us just the MAC. Same guard pattern as the LEDC handling in
// the cup sketch. The callback only queues; see drainRx().
// ===========================================================================
static void enqueueRx(const uint8_t* mac, int8_t rssi, const uint8_t* data, int len) {
  RxItem it;
  memcpy(it.mac, mac, 6);
  it.rssi = rssi;
  if (len < 0) len = 0;
  it.len = (uint8_t)(len > 255 ? 255 : len);
  int n = len;
  if (n > (int)sizeof(it.data)) n = (int)sizeof(it.data);
  memcpy(it.data, data, n);
  if (rxQueue == nullptr || xQueueSend(rxQueue, &it, 0) != pdTRUE) rxQueueDrops = rxQueueDrops + 1;
}

#if ESP_ARDUINO_VERSION_MAJOR >= 3
static void onDataRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
  int8_t rssi = info->rx_ctrl ? info->rx_ctrl->rssi : 0;
  enqueueRx(info->src_addr, rssi, data, len);
}
#else
static void onDataRecv(const uint8_t* mac, const uint8_t* data, int len) {
  enqueueRx(mac, 0, data, len);   // core 2.x recv path exposes no RSSI
}
#endif

// Runs in loop() for every queued packet. HELLO and telemetry carry the same
// payload and are handled the same way; the only difference reported is the
// "hello" flag, which says the cup is still broadcasting.
static void handlePacket(const uint8_t* mac, int8_t upRssi, const uint8_t* data, int len) {
  if (len < 2) return;
  if (data[0] != DDM_PROTO_VERSION) { versionRejects++; return; }
  if (len != (int)sizeof(DdmTelemetryPacket)) return;
  if (data[1] != DDM_MSG_HELLO && data[1] != DDM_MSG_TELEMETRY) return;

  const DdmTelemetryPacket* p = (const DdmTelemetryPacket*)data;
  uint32_t now = millis();
  char m[18]; macFmt(mac, m);
  bool hello = (p->msgType == DDM_MSG_HELLO);

  int i = trackCup(mac, now);
  if (i >= 0) {
    CupTrack& c = cups[i];
    c.lastSeenMs = now;
    c.horse      = (p->horse <= DDM_MAX_HORSE) ? p->horse : 0;
    c.lastSeq    = p->seq;
    c.dropped    = p->dropped;
    c.rssi       = p->rssi;
    c.upRssi     = upRssi;
    c.tokenCount = p->tokenCount;
    c.hello      = hello;
  }
  emitTelem(m, p, upRssi, hello);
  if (debugText)
    textf("%s mac=%s horse=%u seq=%lu dropped=%lu rssi=%d up_rssi=%d tokens=%u",
          hello ? "HELLO" : "TELEM", m, (unsigned)p->horse,
          (unsigned long)p->seq, (unsigned long)p->dropped,
          (int)p->rssi, (int)upRssi, (unsigned)p->tokenCount);
}

static void drainRx() {
  RxItem it;
  int budget = RX_QUEUE_LEN;   // bounded per pass so the broadcast timer stays on time
  while (budget-- > 0 && xQueueReceive(rxQueue, &it, 0) == pdTRUE)
    handlePacket(it.mac, it.rssi, it.data, it.len);
}

// ===========================================================================
// Broadcast and mode control
// ===========================================================================
static void demoOff(const char* why) {
  if (demoMode) {
    demoMode = false;
    textf("[demo] off (%s)", why);
  }
}

// Silent-boot rule: the state broadcast starts on the first JSON state line
// or typed state/scratch/renum/results/demo command, and never stops again.
static void startBroadcast(const char* why) {
  if (!broadcasting) {
    broadcasting = true;
    textf("[bcast] state broadcast on (%s)", why);
  }
  tBroadcast = millis() - BROADCAST_MS;   // the next loop() pass sends at once
}

static void setDebug(bool on, const char* why) {
  debugText = on;
  textf("[debug] text %s (%s)", on ? "on" : "off", why);
}

// `json 1` / `json 0`. While it runs the summary table stays off whatever the
// debug flag says (the line carries the same numbers); `json 0` hands the
// table back to the flag. TELEM text lines are not affected.
static void setJsonAuto(bool on) {
  jsonAuto = on;
  textf("[json] state line every %d ms %s (%s)", STATE_LINE_MS, on ? "on" : "off",
        on ? "summary table off" : "summary table follows the debug flag");
  if (on) tState = millis() - STATE_LINE_MS;   // the first line on the next loop() pass, not a second from now
}

// Set, replace or remove the renum pair for `from`. Returns false when there
// is no free slot for a new pair.
static bool setRenum(uint8_t from, uint8_t to) {
  int slot = -1, freeSlot = -1;
  for (int i = 0; i < DDM_RENUM_SLOTS; i++) {
    if (statePkt.renum[i][0] == from) slot = i;
    else if (statePkt.renum[i][0] == 0 && freeSlot < 0) freeSlot = i;
  }
  if (to == 0) {                                           // remove
    if (slot >= 0) { statePkt.renum[slot][0] = 0; statePkt.renum[slot][1] = 0; }
    return true;
  }
  if (slot < 0) slot = freeSlot;
  if (slot < 0) return false;
  statePkt.renum[slot][0] = from;
  statePkt.renum[slot][1] = to;
  return true;
}

// ===========================================================================
// Hand-typed serial commands (replies always print, prefixed "# ")
// ===========================================================================
static void printHelp() {
  textf("Commands (newline-terminated; every reply starts with '# '):");
  textf("  state <0-6>              set raceState  (0 PRE_RACE 1 BETTING_OPEN 2 FINAL_CALL");
  textf("                           3 AT_THE_POST 4 RUNNING 5 WINNER 6 AFTER_PARTY)");
  textf("  scratch <horse> <0|1>    set/clear horse 1-%d scratched (no replacement)", DDM_MAX_HORSE);
  textf("  renum <from> <to>        cups at horse <from> become <to> (1-%d); <to> 0 removes the pair; %d pairs at most",
        DDM_MAX_HORSE, DDM_RENUM_SLOTS);
  textf("  results <w> <p> <s>      the WIN, PLACE and SHOW horses (0 = not yet); results 0 0 0 clears");
  textf("  cups                     dump the cup table (MAC, horse, tokens, signal, age)");
  textf("  demo                     toggle demo mode (WINNER with the results walking 1-%d every 3s)", DDM_MAX_HORSE);
  textf("  debug on|off             human-readable TELEM lines and 5s summary table");
  textf("  json                     full gateway state as one JSON line, now");
  textf("  json 1|0                 that line every 1s (summary table off) / back to normal");
  textf("  help                     this text");
  textf("Any state/scratch/renum/results command turns demo mode OFF. Any of those, or demo,");
  textf("starts the state broadcast if the gateway is still silent from boot.");
  textf("JSON lines ({\"t\":\"state\"...}, {\"t\":\"debug\"...}) are DevPi's: see README.");
  textf("A cup's horse number is set ON THE CUP (touch menu HORSE, or serial n<N> there).");
}

static void printCups() {
  uint32_t now = millis();
  textf("CUPS bcast=%s demo=%s rev=%lu", broadcasting ? "on" : "off", demoMode ? "on" : "off",
        (unsigned long)stateRev);
  textf("CUPS mac               horse  tok  rssi  up_rssi  age_ms  status");
  bool any = false;
  for (int i = 0; i < DDM_MAX_CUPS; i++) {
    if (!cups[i].used) continue;
    any = true;
    char m[18]; macFmt(cups[i].mac, m);
    uint32_t age = now - cups[i].lastSeenMs;
    textf("CUPS %s %5u %4u  %4d     %4d %7lu  %s%s", m, (unsigned)cups[i].horse, (unsigned)cups[i].tokenCount,
          cups[i].rssi, cups[i].upRssi, (unsigned long)age,
          (age > STALE_MS) ? "STALE" : "OK", cups[i].hello ? " (hello: no gateway MAC yet)" : "");
  }
  if (!any) textf("CUPS (none heard yet)");
}

static void handleCommand(char* line) {
  while (*line == ' ') line++;
  if (*line == 0) return;

  int a = -1, b = -1, c = -1;

  if (strncmp(line, "state ", 6) == 0 && sscanf(line + 6, "%d", &a) == 1) {
    if (a < DDM_PRE_RACE || a > DDM_AFTER_PARTY) { textf("ERR state 0-6"); return; }
    demoOff("state command");
    statePkt.raceState = (uint8_t)a;
    startBroadcast("state command");
    textf("OK state=%d", a);

  } else if (strncmp(line, "scratch ", 8) == 0 && sscanf(line + 8, "%d %d", &a, &b) == 2) {
    if (a < 1 || a > DDM_MAX_HORSE) { textf("ERR horse 1-%d", DDM_MAX_HORSE); return; }
    if (b != 0 && b != 1)           { textf("ERR scratch 0|1"); return; }
    demoOff("scratch command");
    if (b) statePkt.scratched |=  (1u << a);
    else   statePkt.scratched &= ~(1u << a);
    startBroadcast("scratch command");
    textf("OK scratch horse=%d -> %d", a, b);

  } else if (strncmp(line, "renum ", 6) == 0 && sscanf(line + 6, "%d %d", &a, &b) == 2) {
    if (a < 1 || a > DDM_MAX_HORSE)              { textf("ERR from 1-%d", DDM_MAX_HORSE); return; }
    if (b < 0 || b > DDM_MAX_HORSE || b == a)    { textf("ERR to 0-%d, not %d", DDM_MAX_HORSE, a); return; }
    demoOff("renum command");
    if (!setRenum((uint8_t)a, (uint8_t)b)) { textf("ERR no free renum slot (%d in use)", DDM_RENUM_SLOTS); return; }
    startBroadcast("renum command");
    if (b) textf("OK renum %d -> %d", a, b);
    else   textf("OK renum %d removed", a);

  } else if (strncmp(line, "results ", 8) == 0 && sscanf(line + 8, "%d %d %d", &a, &b, &c) == 3) {
    if (a < 0 || a > DDM_MAX_HORSE || b < 0 || b > DDM_MAX_HORSE || c < 0 || c > DDM_MAX_HORSE) {
      textf("ERR results 0-%d each", DDM_MAX_HORSE); return;
    }
    demoOff("results command");
    statePkt.results[0] = (uint8_t)a;
    statePkt.results[1] = (uint8_t)b;
    statePkt.results[2] = (uint8_t)c;
    startBroadcast("results command");
    textf("OK results win=%d place=%d show=%d", a, b, c);

  } else if (strcmp(line, "cups") == 0) {
    printCups();

  } else if (strcmp(line, "demo") == 0) {
    demoMode = !demoMode;
    textf("[demo] %s", demoMode ? "on" : "off");
    if (demoMode) startBroadcast("demo command");

  } else if (strcmp(line, "debug on") == 0) {
    setDebug(true, "command");

  } else if (strcmp(line, "debug off") == 0) {
    setDebug(false, "command");

  } else if (strcmp(line, "json") == 0) {
    emitState();

  } else if (strcmp(line, "json 1") == 0) {
    setJsonAuto(true);

  } else if (strcmp(line, "json 0") == 0) {
    setJsonAuto(false);

  } else if (strcmp(line, "help") == 0) {
    printHelp();

  } else {
    textf("ERR unknown command, try: help");
  }
}

// ===========================================================================
// Down: JSON lines from DevPi, parsed with ArduinoJson v7. A rejected line
// changes nothing and gets an err line; an applied line gets a status line.
// Unknown "t" values and unknown keys are ignored silently (forward
// compatibility).
// ===========================================================================

// v is an integer (not a bool, float, string or null) within [lo, hi].
static bool jsonIntIn(JsonVariantConst v, long lo, long hi, long* out) {
  if (!v.is<long>()) return false;
  long x = v.as<long>();
  if (x < lo || x > hi) return false;
  *out = x;
  return true;
}

// "scr": an array of horse numbers 1..DDM_MAX_HORSE (any length, repeats
// allowed) -> a bitmask.
static bool jsonScratched(JsonVariantConst v, uint32_t* out) {
  JsonArrayConst a = v.as<JsonArrayConst>();
  if (a.isNull()) return false;
  uint32_t bits = 0;
  for (JsonVariantConst e : a) {
    long h;
    if (!jsonIntIn(e, 1, DDM_MAX_HORSE, &h)) return false;
    bits |= (1u << h);
  }
  *out = bits;
  return true;
}

// "renum": an array of at most DDM_RENUM_SLOTS [from, to] pairs, both
// 1..DDM_MAX_HORSE, from != to, no from twice.
static bool jsonRenum(JsonVariantConst v, uint8_t out[][2]) {
  JsonArrayConst a = v.as<JsonArrayConst>();
  if (a.isNull() || a.size() > DDM_RENUM_SLOTS) return false;
  for (int i = 0; i < DDM_RENUM_SLOTS; i++) { out[i][0] = 0; out[i][1] = 0; }
  int i = 0;
  for (JsonVariantConst e : a) {
    JsonArrayConst pair = e.as<JsonArrayConst>();
    if (pair.isNull() || pair.size() != 2) return false;
    long from, to;
    if (!jsonIntIn(pair[0], 1, DDM_MAX_HORSE, &from) || !jsonIntIn(pair[1], 1, DDM_MAX_HORSE, &to)) return false;
    if (from == to) return false;
    for (int j = 0; j < i; j++) if (out[j][0] == from) return false;
    out[i][0] = (uint8_t)from;
    out[i][1] = (uint8_t)to;
    i++;
  }
  return true;
}

// "res": exactly DDM_RESULT_SLOTS integers 0..DDM_MAX_HORSE.
static bool jsonResults(JsonVariantConst v, uint8_t* out) {
  JsonArrayConst a = v.as<JsonArrayConst>();
  if (a.isNull() || a.size() != DDM_RESULT_SLOTS) return false;
  int i = 0;
  for (JsonVariantConst e : a) {
    long h;
    if (!jsonIntIn(e, 0, DDM_MAX_HORSE, &h)) return false;
    out[i++] = (uint8_t)h;
  }
  return true;
}

// {"t":"state","rev":42,"st":1,"scr":[9,15],"renum":[[9,22]],"res":[19,1,22]}
// A full snapshot, never a delta. Idempotent: the same line twice is normal.
static void jsonState(JsonObjectConst root, const char* line) {
  long     rev, st;
  uint32_t scr;
  uint8_t  renum[DDM_RENUM_SLOTS][2];
  uint8_t  res[DDM_RESULT_SLOTS];

  if (!jsonIntIn(root["rev"], 1, LONG_MAX, &rev) ||
      !jsonIntIn(root["st"], DDM_PRE_RACE, DDM_AFTER_PARTY, &st) ||
      !jsonScratched(root["scr"], &scr) ||
      !jsonRenum(root["renum"], renum) ||
      !jsonResults(root["res"], res)) {
    emitErr("invalid", line);
    return;
  }

  // 1. Everything into the broadcast packet in one step. The broadcast also
  //    runs from loop(), so no packet can go out half-applied.
  statePkt.raceState = (uint8_t)st;
  statePkt.scratched = scr;
  memcpy(statePkt.renum,   renum, sizeof(statePkt.renum));
  memcpy(statePkt.results, res,   sizeof(statePkt.results));
  // 2.
  stateRev = (uint32_t)rev;
  // 3.
  demoOff("state line");
  startBroadcast("state line");
  // 4.
  helloActive = false;
  // 5.
  if (debugText) textf("[state] rev %lu applied: st %ld", (unsigned long)stateRev, st);
  emitStatus();
}

// {"t":"debug","on":true}
static void jsonDebug(JsonObjectConst root, const char* line) {
  JsonVariantConst on = root["on"];
  if (!on.is<bool>()) { emitErr("invalid", line); return; }
  setDebug(on.as<bool>(), "debug line");
  emitStatus();
}

static void handleJsonLine(const char* line) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, line);   // const char*: the line is left intact
  if (err) { emitErr("parse", line); return; }

  JsonObjectConst root = doc.as<JsonObjectConst>();
  const char* t = root.isNull() ? nullptr : root["t"].as<const char*>();
  if (t == nullptr) { emitErr("invalid", line); return; }

  if      (strcmp(t, "state") == 0) jsonState(root, line);
  else if (strcmp(t, "debug") == 0) jsonDebug(root, line);
  // any other "t" (a v1 "roster", say): not ours, ignored without a word
}

// ===========================================================================
// Line reader: non-blocking, DDM_LINE_MAX bytes, dispatch on '\n'.
// ===========================================================================
static void dispatchLine(char* line) {
  if (line[0] == '{') handleJsonLine(line);
  else                handleCommand(line);
}

static void pollSerial() {
  static char lineBuf[DDM_LINE_MAX + 1];
  static int  n = 0;
  static bool overflow = false;      // discarding up to the next '\n'

  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      if (overflow) { overflow = false; n = 0; continue; }   // err already sent
      if (n > 0 && lineBuf[n - 1] == '\r') n--;             // strip a trailing CR
      lineBuf[n] = 0;
      if (n > 0) dispatchLine(lineBuf);                     // empty lines are ignored
      n = 0;
    } else if (overflow) {
      // discarding
    } else if (n < DDM_LINE_MAX) {
      lineBuf[n++] = c;
    } else {
      overflow = true;
      n = 0;
      emitErr("overflow", nullptr);
    }
  }
}

// ===========================================================================
// Summary — the range-test readout (debug text only)
// ===========================================================================
static void printSummary() {
  uint32_t now = millis();
  textf("---- CUPS seq=%lu state=%u demo=%s rejects=%lu heard=%d ----",
        (unsigned long)statePkt.seq, statePkt.raceState,
        demoMode ? "on" : "off", (unsigned long)versionRejects, cupsHeard(now));
  if (!broadcasting)
    textf("  (state broadcast OFF: waiting for a JSON state line or a typed command)");
  if (rxQueueDrops)
    textf("  (rx queue dropped %lu packet(s))", (unsigned long)rxQueueDrops);
  if (tableFull)
    textf("  (cup table full: %lu packet(s) from untracked cups ignored)", (unsigned long)tableFull);
  textf(" mac               horse  age_ms   drop  rssi  up_rssi  tok  status");
  bool any = false;
  for (int i = 0; i < DDM_MAX_CUPS; i++) {
    if (!cups[i].used) continue;
    any = true;
    char m[18]; macFmt(cups[i].mac, m);
    uint32_t age = now - cups[i].lastSeenMs;
    textf(" %s %5u %7lu %6lu  %4d     %4d %4u  %s",
          m, (unsigned)cups[i].horse, (unsigned long)age,
          (unsigned long)cups[i].dropped,
          cups[i].rssi, cups[i].upRssi, (unsigned)cups[i].tokenCount,
          (age > STALE_MS) ? "STALE" : "OK");
  }
  if (!any) textf("  (no cups yet - waiting for a HELLO)");
}

// ---------------------------------------------------------------------------
void setup() {
  // Both buffer sizes must be set before begin(). The RX side takes one
  // downlink line (state lines are under 200 bytes now, the cap is kept);
  // the TX buffer lets a burst of telem lines drain in the background
  // instead of stalling loop() at 115200 baud (about 11.5 KB/s, so 2 KB
  // takes ~175 ms). 8 KB holds a full-table status line (~2.3 KB) plus 24
  // telem lines and their TELEM text (~5 KB) at once.
  Serial.setRxBufferSize(DDM_LINE_MAX);
  Serial.setTxBufferSize(8192);
  Serial.begin(115200);
  delay(300);

  textf("DDM La Quiniela gateway - ESP-NOW <-> serial JSON bridge");
  textf("proto v%d, line proto v%d, channel %d, cup table %d",
        DDM_PROTO_VERSION, DDM_LINE_PROTO_VERSION, DDM_ESPNOW_CHANNEL, DDM_MAX_CUPS);
  textf("build: DDM_AUTO_DEMO=%d DDM_DEBUG_TEXT=%d", DDM_AUTO_DEMO, DDM_DEBUG_TEXT);

  // ESP-NOW only: STA mode, never associated, pinned channel
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);
  esp_wifi_set_channel(DDM_ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);

  uint8_t mac[6];
  WiFi.macAddress(mac);
  macFmt(mac, gwMac);
  textf("gateway MAC: %s", gwMac);

  rxQueue = xQueueCreate(RX_QUEUE_LEN, sizeof(RxItem));
  if (rxQueue == nullptr || esp_now_init() != ESP_OK) {
    textf("FATAL esp_now_init failed");
    while (true) delay(1000);
  }
  esp_now_register_recv_cb(onDataRecv);

  // The broadcast address: the one and only peer, for the state packets.
  esp_now_peer_info_t p = {};
  memcpy(p.peer_addr, BCAST, 6);
  p.channel = DDM_ESPNOW_CHANNEL;
  p.ifidx   = WIFI_IF_STA;
  p.encrypt = false;
  if (esp_now_add_peer(&p) != ESP_OK) textf("ERR esp_now_add_peer broadcast failed");

  for (int i = 0; i < DDM_MAX_CUPS; i++) cups[i] = {};

  // Initial broadcast state: PRE_RACE, nobody scratched, no renumbers, no
  // results. Nothing is sent until broadcasting starts.
  statePkt = {};
  statePkt.version   = DDM_PROTO_VERSION;
  statePkt.msgType   = DDM_MSG_STATE;
  statePkt.raceState = DDM_PRE_RACE;

#if DDM_AUTO_DEMO
  textf("[demo] on (DDM_AUTO_DEMO build): broadcasting from boot; a JSON state line takes over");
#else
  textf("silent: no state broadcast until a JSON state line or a typed state/scratch/renum/results/demo command");
#endif

  emitHello();
  tHello = millis();
}

void loop() {
  uint32_t now = millis();

  pollSerial();
  drainRx();

  if (demoMode && now - tDemo >= DEMO_STEP_MS) {
    tDemo = now;
    demoStep++;
    // WINNER with the results walking through the horses, so every cup that
    // hears us shows a WIN, PLACE or SHOW banner in turn (its own number
    // stays its own; the gateway cannot and does not renumber a cup here).
    statePkt.raceState  = DDM_WINNER;
    statePkt.results[0] = (uint8_t)((demoStep     % DDM_MAX_HORSE) + 1);
    statePkt.results[1] = (uint8_t)(((demoStep + 1) % DDM_MAX_HORSE) + 1);
    statePkt.results[2] = (uint8_t)(((demoStep + 2) % DDM_MAX_HORSE) + 1);
  }

  if (broadcasting && now - tBroadcast >= BROADCAST_MS) {
    tBroadcast = now;
    statePkt.seq++;
    // Repeat-broadcast, no acks: a cup that misses one gets the next in 500ms
    esp_now_send(BCAST, (const uint8_t*)&statePkt, sizeof(statePkt));
  }

  if (helloActive && now - tHello >= HELLO_MS) {
    tHello = now;
    emitHello();
  }

  if (now - tStatus >= STATUS_MS) {
    forgetStale(now);
    emitStatus();                       // sets tStatus
  }

  if (jsonAuto && now - tState >= STATE_LINE_MS) {
    emitState();                        // sets tState
  }

  if (debugText && !jsonAuto && now - tSummary >= SUMMARY_MS) {   // `json 1` stands in for the table
    tSummary = now;
    printSummary();
  }
}
