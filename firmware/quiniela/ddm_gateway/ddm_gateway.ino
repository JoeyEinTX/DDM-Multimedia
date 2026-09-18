/*
 * ddm_gateway.ino — La Quiniela ESP-NOW gateway
 *
 * Plain ESP32 WROOM-32 dev board. No display. USB serial to DevPi, or to a
 * laptop for bench work.
 *
 * Role: the bridge between the cups (ESP-NOW) and DevPi (USB serial).
 *   - Broadcasts the shared DdmStatePacket to every cup twice a second, once
 *     something has told it what to broadcast (see "Boot behaviour" below).
 *   - Collects telemetry and HELLOs from the cups and reports every packet
 *     up the serial line as one JSON object per line.
 *   - Accepts full state snapshots, the MAC-to-ID roster and a debug toggle
 *     from DevPi as JSON lines.
 *
 * Serial line protocol: 115200 8N1, one compact JSON object per line, at
 * most DDM_LINE_MAX bytes per line in either direction. Documented in
 * ../README.md ("Serial line protocol"); the key names are a contract with
 * the DevPi bridge, implement them exactly.
 *   up:   hello, status, telem, cup_hello, err
 *   down: state, roster, debug
 * Every line this sketch prints that is not JSON starts with "# " so DevPi
 * can drop it. The hand-typed bench commands still work: type `help`.
 *
 * Boot behaviour: a default build (DDM_AUTO_DEMO 0) is silent. It sends
 * hello and status, receives and reports cup traffic and acks HELLOs, but
 * broadcasts no state at all until a JSON state line arrives or a person
 * types state/horse/scratch/demo. Cups hold their last number meanwhile.
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
// Bump it when a line's keys or their meaning change.
#define DDM_LINE_PROTO_VERSION 1

// Boot default of the human-readable output: the per-packet TELEM lines and
// the 5-second summary table. 0 = JSON lines only. Flip it at runtime with
// the JSON line {"t":"debug","on":true} or the typed command `debug on`.
// JSON lines are emitted whatever this says.
#define DDM_DEBUG_TEXT 0

// 0 = party build: the gateway broadcasts NOTHING over ESP-NOW until a valid
//     JSON state line arrives or a person types state/horse/scratch/demo.
//     There is no timeout and no fallback. A power blip reboots the gateway
//     in a second and DevPi in a minute; for that minute the cups must hold
//     their last number, not walk demo horses across real tokens.
// 1 = bench build: boot straight into demo mode and broadcast at once, as
//     the original bench sketch did. A JSON state line still takes over.
// The value is printed in the boot banner so a bench build left on the party
// gateway is obvious in any serial log.
#define DDM_AUTO_DEMO 0

// ===========================================================================
// KNOWN_CUPS — bench-mode identity table. Paste real cup MACs here.
//
// Index in this table == cup ID (0-based, the wire value). Collect the MACs
// from each cup's waiting screen (big gold text) or from this gateway's
// serial log: any cup not listed here gets the next free ID at runtime and
// its MAC is printed as a NEWCUP line formatted for pasting into this table.
//
// This table and the runtime assignments only apply until DevPi sends its
// first roster line; from then on DevPi owns identity (see jsonRoster()).
// Runtime assignments are RAM-only — they reshuffle on gateway reboot. Once
// a MAC is pasted here its ID is stable across reboots.
// ===========================================================================
struct KnownCup { uint8_t mac[6]; };

const KnownCup KNOWN_CUPS[] = {
  // { { 0xA4, 0xCF, 0x12, 0x00, 0x00, 0x00 } },  // cup 0 — example, replace
  // { { 0xA4, 0xCF, 0x12, 0x00, 0x00, 0x01 } },  // cup 1
};
const int KNOWN_CUPS_N = sizeof(KNOWN_CUPS) / sizeof(KNOWN_CUPS[0]);

// ---------------------------------------------------------------------------
// Timing and sizes
// ---------------------------------------------------------------------------
#define BROADCAST_MS   500     // state broadcast cadence
#define DEMO_STEP_MS  3000     // demo horse walk cadence
#define STALE_MS      3000     // silent longer than this -> STALE, and not counted in status "cups"
#define SUMMARY_MS    5000     // debug summary table cadence
#define STATUS_MS     5000     // JSON status heartbeat cadence
#define HELLO_MS      2000     // JSON hello repeat cadence until the first state line
#define DDM_LINE_MAX      1024     // longest serial line, both directions, excluding the newline
#define RX_QUEUE_LEN    32     // ESP-NOW packets that can wait for loop()
#define ERR_EXCERPT     40     // characters of a rejected line echoed in the err line
#define ACK_QUEUE_LEN   (DDM_MAX_CUPS + 4)   // hello-acks waiting to go out, one entry per MAC
#define ACK_SEND_TIMEOUT_MS 100              // give up waiting for an ack's send callback after this

static const uint8_t BCAST[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

// ---------------------------------------------------------------------------
// Cup roster — fixed array, index == cup ID (0-based, the wire value)
// ---------------------------------------------------------------------------
struct CupSlot {
  bool     used;
  uint8_t  mac[6];
  uint32_t lastSeenMs;   // millis() of last packet; 0 = never heard from
  uint32_t lastSeq;      // last state seq the cup reported seeing
  uint32_t dropped;      // cup-reported drop count
  int8_t   rssi;         // cup-reported RSSI of gateway->cup packets (downlink)
  int8_t   upRssi;       // gateway-measured RSSI of cup->gateway packets (uplink)
  uint16_t tokenCount;
};

CupSlot roster[DDM_MAX_CUPS];
CupSlot rosterOld[DDM_MAX_CUPS];   // the previous table while a roster line is applied

// The packet we broadcast. Mutated by state lines, serial commands and demo mode.
DdmStatePacket statePkt;

uint32_t versionRejects = 0;                     // packets with the wrong DDM_PROTO_VERSION
bool     demoMode       = (DDM_AUTO_DEMO != 0);
bool     broadcasting   = (DDM_AUTO_DEMO != 0);  // once true, stays true until reboot
bool     debugText      = (DDM_DEBUG_TEXT != 0);
bool     helloActive    = true;                  // hello repeats until the first valid state line
uint32_t stateRev       = 0;                     // rev of the last applied state line, 0 = none yet
uint32_t rosterRev      = 0;                     // rev of the last applied roster line, 0 = DevPi has not spoken
uint32_t demoStep       = 0;
char     gwMac[18];                              // this board's MAC, formatted once at boot

uint32_t tBroadcast = 0, tDemo = 0, tSummary = 0, tStatus = 0, tHello = 0;

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

static char outBuf[DDM_LINE_MAX + 1];                // JSON lines are built here, from loop() only

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
// MAC helpers
// ---------------------------------------------------------------------------
static void macFmt(const uint8_t* m, char* out /* >= 18 bytes */) {
  snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
           m[0], m[1], m[2], m[3], m[4], m[5]);
}

static int hexVal(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// "A0:B7:65:12:34:56", either case, exactly 17 characters.
static bool parseMac(const char* s, uint8_t* out) {
  if (strlen(s) != 17) return false;
  for (int i = 0; i < 6; i++) {
    int hi = hexVal(s[i * 3]), lo = hexVal(s[i * 3 + 1]);
    if (hi < 0 || lo < 0) return false;
    if (i < 5 && s[i * 3 + 2] != ':') return false;
    out[i] = (uint8_t)(hi * 16 + lo);
  }
  return true;
}

// ---------------------------------------------------------------------------
// Roster helpers
// ---------------------------------------------------------------------------
static int findCup(const uint8_t* mac) {
  for (int i = 0; i < DDM_MAX_CUPS; i++)
    if (roster[i].used && memcmp(roster[i].mac, mac, 6) == 0) return i;
  return -1;
}

static int findCupOld(const uint8_t* mac) {
  for (int i = 0; i < DDM_MAX_CUPS; i++)
    if (rosterOld[i].used && memcmp(rosterOld[i].mac, mac, 6) == 0) return i;
  return -1;
}

// ---------------------------------------------------------------------------
// ESP-NOW peers. The broadcast address is the only permanent peer: ESP-NOW
// holds ESP_NOW_MAX_TOTAL_PEER_NUM (20) peers in all, broadcast included, so
// registering every roster cup would cap the fleet at 19. Receiving needs no
// peer entry at all; only the unicast hello-ack does, and it gets one for the
// few milliseconds the frame is in flight (see the ack queue below).
// ---------------------------------------------------------------------------
static bool addPeer(const uint8_t* mac) {
  if (esp_now_is_peer_exist(mac)) return true;
  esp_now_peer_info_t p = {};
  memcpy(p.peer_addr, mac, 6);
  p.channel = DDM_ESPNOW_CHANNEL;
  p.ifidx   = WIFI_IF_STA;
  p.encrypt = false;
  esp_err_t e = esp_now_add_peer(&p);
  if (e != ESP_OK) {
    char m[18]; macFmt(mac, m);
    textf("ERR esp_now_add_peer %s failed (%d)", m, (int)e);
    return false;
  }
  return true;
}

static void delPeer(const uint8_t* mac) {
  if (memcmp(mac, BCAST, 6) == 0) return;      // the broadcast peer carries the state packets: never
  if (esp_now_is_peer_exist(mac)) esp_now_del_peer(mac);
}

// Bench mode only (rosterRev == 0): assign the next free ID to a new MAC.
// Returns -1 if the roster is full.
static int addCup(const uint8_t* mac) {
  for (int i = 0; i < DDM_MAX_CUPS; i++) {
    if (!roster[i].used) {
      roster[i] = {};
      roster[i].used = true;
      memcpy(roster[i].mac, mac, 6);

      char m[18]; macFmt(mac, m);
      textf("NEWCUP id=%d mac=%s", i, m);
      textf("  paste into KNOWN_CUPS[]:  { { 0x%02X, 0x%02X, 0x%02X, 0x%02X, 0x%02X, 0x%02X } },  // cup %d",
            mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], i);
      return i;
    }
  }
  textf("ERR roster full, cup ignored");
  return -1;
}

// ---------------------------------------------------------------------------
// Hello ack — gateway -> cup, unicast.
//
// PROTOCOL NOTE: ddm_common.h defines no packet that tells a cup its own ID,
// so the ack reuses the DdmTelemetryPacket layout in the reverse direction
// with msgType = DDM_MSG_HELLO and cupId = the assigned ID. All other fields
// are zero. Byte-compatible with the shared header; candidate for a proper
// assignment packet in protocol v2. The cup adopts whatever ID a fresh ack
// carries, at any time, so this is also how a cup is moved to a new slot.
// The cup only accepts an ack unicast to its own MAC, never a broadcast.
//
// Acks are queued and sent one at a time from loop() (ackTick): the cup's
// MAC becomes an ESP-NOW peer, the frame goes out, and the peer is deleted
// once the send callback has reported on it or ACK_SEND_TIMEOUT_MS has
// passed. Never straight after esp_now_send(): the send is asynchronous and
// pulling the peer from under it loses the frame. Queued acks are deduped by
// MAC, newest ID wins. A full queue drops the new ack, and the cup's next
// HELLO or its claim-mismatch telemetry brings it back. No retries here.
// ---------------------------------------------------------------------------
struct AckEntry { uint8_t mac[6]; uint8_t cupId; };

static AckEntry ackQueue[ACK_QUEUE_LEN];         // ring buffer, no heap
static int      ackHead = 0, ackCount = 0;       // head = oldest entry

static volatile bool ackInFlight = false;
static uint8_t       ackMac[6];                  // the transient peer while an ack is in flight
static uint8_t       ackId       = 0;
static uint32_t      ackSentAt   = 0;
static volatile bool ackDone     = false;        // set by the send callback for ackMac
static volatile bool ackOk       = false;

static void queueAckTo(const uint8_t* mac, uint8_t cupId) {
  for (int i = 0; i < ackCount; i++) {
    AckEntry& e = ackQueue[(ackHead + i) % ACK_QUEUE_LEN];
    if (memcmp(e.mac, mac, 6) == 0) { e.cupId = cupId; return; }   // already queued: newest ID wins
  }
  if (ackCount >= ACK_QUEUE_LEN) {
    char m[18]; macFmt(mac, m);
    textf("ERR ack queue full, ack to cup %u %s dropped", (unsigned)cupId, m);
    return;
  }
  AckEntry& e = ackQueue[(ackHead + ackCount) % ACK_QUEUE_LEN];
  memcpy(e.mac, mac, 6);
  e.cupId = cupId;
  ackCount++;
}

static void queueAck(int id) { queueAckTo(roster[id].mac, (uint8_t)id); }

// Send callback, WiFi task: flags the ack in flight and nothing else. Core
// 3.3 (IDF 5.5) hands over a tx-info struct, older cores the bare MAC.
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
static void onDataSent(const esp_now_send_info_t* info, esp_now_send_status_t status) {
  const uint8_t* mac = info->des_addr;
#else
static void onDataSent(const uint8_t* mac, esp_now_send_status_t status) {
#endif
  if (ackInFlight && memcmp(mac, ackMac, 6) == 0) {
    ackOk   = (status == ESP_NOW_SEND_SUCCESS);
    ackDone = true;
  }
}

// One ack in flight at a time. Called every loop() pass; never blocks.
static void ackTick(uint32_t now) {
  if (ackInFlight) {
    if (!ackDone && now - ackSentAt < ACK_SEND_TIMEOUT_MS) return;   // still waiting for the callback
    if (debugText) {
      char m[18]; macFmt(ackMac, m);
      textf("ack to cup %u %s %s", (unsigned)ackId, m,
            ackDone ? (ackOk ? "delivered" : "not delivered") : "timed out, no send callback");
    }
    delPeer(ackMac);
    ackInFlight = false;
  }
  if (ackCount == 0) return;

  AckEntry e = ackQueue[ackHead];
  ackHead = (ackHead + 1) % ACK_QUEUE_LEN;
  ackCount--;
  memcpy(ackMac, e.mac, 6);
  ackId = e.cupId;

  if (!addPeer(ackMac)) { delPeer(ackMac); return; }   // reported by addPeer; next entry next pass

  DdmTelemetryPacket ack = {};
  ack.version = DDM_PROTO_VERSION;
  ack.msgType = DDM_MSG_HELLO;
  ack.cupId   = ackId;
  ackDone     = false;
  ackOk       = false;
  ackSentAt   = now;
  ackInFlight = true;                     // before the send: the callback can run before it returns
  esp_err_t err = esp_now_send(ackMac, (const uint8_t*)&ack, sizeof(ack));
  if (err != ESP_OK) {
    char m[18]; macFmt(ackMac, m);
    textf("ERR esp_now_send ack to cup %u %s failed (%d)", (unsigned)ackId, m, (int)err);
    ackInFlight = false;
    delPeer(ackMac);
  }
}

// ===========================================================================
// Up: JSON lines to DevPi, built with snprintf. Key names are the contract.
// ===========================================================================
static void emitHello() {
  snprintf(outBuf, sizeof(outBuf), "{\"t\":\"hello\",\"v\":%d,\"proto\":%d,\"mac\":\"%s\"}",
           DDM_LINE_PROTO_VERSION, DDM_PROTO_VERSION, gwMac);
  outLine(outBuf);
}

// Roster cups heard from within STALE_MS: the same threshold as the summary
// table's STALE flag.
static int cupsHeard(uint32_t now) {
  int n = 0;
  for (int i = 0; i < DDM_MAX_CUPS; i++)
    if (roster[i].used && roster[i].lastSeenMs != 0 && now - roster[i].lastSeenMs <= STALE_MS) n++;
  return n;
}

// Heartbeat every STATUS_MS, and the acknowledgement of every applied
// state/roster/debug line. There is no separate ack line.
static void emitStatus() {
  uint32_t now = millis();
  snprintf(outBuf, sizeof(outBuf),
           "{\"t\":\"status\",\"gseq\":%lu,\"phase\":%u,\"state_rev\":%lu,\"roster_rev\":%lu,"
           "\"cups\":%d,\"rejects\":%lu,\"up_s\":%lu}",
           (unsigned long)statePkt.seq, (unsigned)statePkt.raceState,
           (unsigned long)stateRev, (unsigned long)rosterRev,
           cupsHeard(now), (unsigned long)versionRejects, (unsigned long)(now / 1000));
  outLine(outBuf);
  tStatus = now;
}

static void emitCupHello(int id, const char* mac) {
  snprintf(outBuf, sizeof(outBuf), "{\"t\":\"cup_hello\",\"cup\":%d,\"mac\":\"%s\"}", id, mac);
  outLine(outBuf);
}

// One per telemetry packet. "cup" is the roster slot of the sender MAC (-1 if
// unknown); "claim" is added only when the ID the cup believes it has differs
// from that, so DevPi can spot a cup running on a stale ID.
static void emitTelem(int id, const char* mac, const DdmTelemetryPacket* p, int8_t upRssi) {
  int n = snprintf(outBuf, sizeof(outBuf),
                   "{\"t\":\"telem\",\"cup\":%d,\"mac\":\"%s\",\"raw\":%ld,\"count\":%u,"
                   "\"seq\":%lu,\"drop\":%lu,\"rssi\":%d,\"up\":%d",
                   id, mac, (long)p->rawWeight, (unsigned)p->tokenCount,
                   (unsigned long)p->seq, (unsigned long)p->dropped, (int)p->rssi, (int)upRssi);
  if (n < 0 || n >= (int)sizeof(outBuf) - 24) return;
  if ((int)p->cupId != id)
    n += snprintf(outBuf + n, sizeof(outBuf) - n, ",\"claim\":%u", (unsigned)p->cupId);
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

// Runs in loop() for every queued packet.
static void handlePacket(const uint8_t* mac, int8_t upRssi, const uint8_t* data, int len) {
  if (len < 2) return;
  if (data[0] != DDM_PROTO_VERSION) { versionRejects++; return; }
  if (len != (int)sizeof(DdmTelemetryPacket)) return;

  const DdmTelemetryPacket* p = (const DdmTelemetryPacket*)data;
  uint32_t now = millis();
  int id = findCup(mac);
  char m[18]; macFmt(mac, m);

  if (p->msgType == DDM_MSG_HELLO) {
    // Roster ownership: before DevPi has sent a roster (rosterRev == 0) an
    // unknown MAC gets the next free slot, as on the bench. After that DevPi
    // owns identity: no slot, no ack, and the cup stays on its MAC screen
    // until a roster line includes it.
    if (id < 0 && rosterRev == 0) id = addCup(mac);
    if (id >= 0) {
      roster[id].lastSeenMs = now;
      roster[id].upRssi     = upRssi;
      queueAck(id);
    }
    emitCupHello(id, m);
    if (debugText) textf("HELLO cup=%d mac=%s up_rssi=%d", id, m, upRssi);

  } else if (p->msgType == DDM_MSG_TELEMETRY) {
    bool reack = false;
    if (id < 0 && rosterRev == 0) {   // gateway rebooted, cup still has its old ID:
      id = addCup(mac);               // re-adopt it and re-ack so it re-syncs (bench)
      reack = (id >= 0);
    }
    if (id >= 0) {
      CupSlot& c = roster[id];
      c.lastSeenMs = now;
      c.lastSeq    = p->seq;
      c.dropped    = p->dropped;
      c.rssi       = p->rssi;
      c.upRssi     = upRssi;
      c.tokenCount = p->tokenCount;
      // The cup reports the ID it believes it has. If that is not its roster
      // slot (a roster line moved it and the re-ack was lost, a stale ID from
      // before a gateway reboot, or the cup took a stray HELLO for an ack),
      // send the ack again: the cup adopts a fresh ack at any time. This is
      // the same ack path as HELLO; steady state costs nothing.
      if (reack || (int)p->cupId != id) queueAck(id);
    }
    emitTelem(id, m, p, upRssi);
    if (debugText)
      textf("TELEM cup=%d horse=%u seq=%lu dropped=%lu rssi=%d up_rssi=%d tokens=%u",
            id, (unsigned)(id >= 0 ? statePkt.horseForCup[id] : 0),
            (unsigned long)p->seq, (unsigned long)p->dropped,
            (int)p->rssi, (int)upRssi, (unsigned)p->tokenCount);
  }
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
// or typed state/horse/scratch/demo command, and never stops again.
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

// ===========================================================================
// Hand-typed serial commands (replies always print, prefixed "# ")
// ===========================================================================
static void printHelp() {
  textf("Commands (newline-terminated; every reply starts with '# '):");
  textf("  state <0-6>              set raceState  (0 PRE_RACE 1 BETTING_OPEN 2 FINAL_CALL");
  textf("                           3 AT_THE_POST 4 RUNNING 5 WINNER 6 AFTER_PARTY)");
  textf("  horse <cupId> <0-20>     assign horse to cup (0 = unassigned); cupId is 0-based");
  textf("  scratch <cupId> <0|1>    set/clear scratched flag");
  textf("  roster                   dump MAC-to-ID table");
  textf("  demo                     toggle demo mode (horse walk every 3s)");
  textf("  debug on|off             human-readable TELEM lines and 5s summary table");
  textf("  help                     this text");
  textf("Any state/horse/scratch command turns demo mode OFF. Any state/horse/scratch/demo");
  textf("command starts the state broadcast if the gateway is still silent from boot.");
  textf("JSON lines ({\"t\":\"state\"...}, {\"t\":\"roster\"...}, {\"t\":\"debug\"...}) are DevPi's: see README.");
}

static void printRoster() {
  textf("ROSTER rev=%lu owner=%s bcast=%s demo=%s",
        (unsigned long)rosterRev,
        rosterRev ? "DevPi (roster line)" : "KNOWN_CUPS[] + runtime HELLO",
        broadcasting ? "on" : "off", demoMode ? "on" : "off");
  textf("ROSTER id mac               source");
  for (int i = 0; i < DDM_MAX_CUPS; i++) {
    if (!roster[i].used) continue;
    char m[18]; macFmt(roster[i].mac, m);
    textf("ROSTER %2d %s %s", i, m,
          rosterRev ? "roster line" : (i < KNOWN_CUPS_N) ? "KNOWN_CUPS[]" : "runtime");
  }
}

static void handleCommand(char* line) {
  while (*line == ' ') line++;
  if (*line == 0) return;

  int a = -1, b = -1;

  if (strncmp(line, "state ", 6) == 0 && sscanf(line + 6, "%d", &a) == 1) {
    if (a < DDM_PRE_RACE || a > DDM_AFTER_PARTY) { textf("ERR state 0-6"); return; }
    demoOff("state command");
    statePkt.raceState = (uint8_t)a;
    startBroadcast("state command");
    textf("OK state=%d", a);

  } else if (strncmp(line, "horse ", 6) == 0 && sscanf(line + 6, "%d %d", &a, &b) == 2) {
    if (a < 0 || a >= DDM_MAX_CUPS) { textf("ERR cupId 0-%d", DDM_MAX_CUPS - 1); return; }
    if (b < 0 || b > 20)            { textf("ERR horse 0-20"); return; }
    demoOff("horse command");
    statePkt.horseForCup[a] = (uint8_t)b;
    startBroadcast("horse command");
    textf("OK horse cup=%d -> %d", a, b);

  } else if (strncmp(line, "scratch ", 8) == 0 && sscanf(line + 8, "%d %d", &a, &b) == 2) {
    if (a < 0 || a >= DDM_MAX_CUPS) { textf("ERR cupId 0-%d", DDM_MAX_CUPS - 1); return; }
    if (b != 0 && b != 1)           { textf("ERR scratch 0|1"); return; }
    demoOff("scratch command");
    statePkt.scratched[a] = (uint8_t)b;
    startBroadcast("scratch command");
    textf("OK scratch cup=%d -> %d", a, b);

  } else if (strcmp(line, "roster") == 0) {
    printRoster();

  } else if (strcmp(line, "demo") == 0) {
    demoMode = !demoMode;
    textf("[demo] %s", demoMode ? "on" : "off");
    if (demoMode) startBroadcast("demo command");

  } else if (strcmp(line, "debug on") == 0) {
    setDebug(true, "command");

  } else if (strcmp(line, "debug off") == 0) {
    setDebug(false, "command");

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

// v is an array of exactly DDM_MAX_CUPS integers, each within [lo, hi].
static bool jsonU8Array(JsonVariantConst v, long lo, long hi, uint8_t* out) {
  JsonArrayConst a = v.as<JsonArrayConst>();
  if (a.isNull() || a.size() != DDM_MAX_CUPS) return false;
  int i = 0;
  for (JsonVariantConst e : a) {
    long x;
    if (!jsonIntIn(e, lo, hi, &x)) return false;
    out[i++] = (uint8_t)x;
  }
  return true;
}

// {"t":"state","rev":42,"phase":1,"horse":[20 x 0..20],"scr":[20 x 0|1]}
// A full snapshot, never a delta. Idempotent: the same line twice is normal.
static void jsonState(JsonObjectConst root, const char* line) {
  long    rev, phase;
  uint8_t horse[DDM_MAX_CUPS], scr[DDM_MAX_CUPS];

  if (!jsonIntIn(root["rev"], 1, LONG_MAX, &rev) ||
      !jsonIntIn(root["phase"], DDM_PRE_RACE, DDM_AFTER_PARTY, &phase) ||
      !jsonU8Array(root["horse"], 0, 20, horse) ||
      !jsonU8Array(root["scr"], 0, 1, scr)) {
    emitErr("invalid", line);
    return;
  }

  // 1. Everything into the broadcast packet in one step. The broadcast also
  //    runs from loop(), so no packet can go out half-applied.
  statePkt.raceState = (uint8_t)phase;
  memcpy(statePkt.horseForCup, horse, DDM_MAX_CUPS);
  memcpy(statePkt.scratched,   scr,   DDM_MAX_CUPS);
  // 2.
  stateRev = (uint32_t)rev;
  // 3.
  demoOff("state line");
  startBroadcast("state line");
  // 4.
  helloActive = false;
  // 5.
  if (debugText) textf("[state] rev %lu applied: phase %ld", (unsigned long)stateRev, phase);
  emitStatus();
}

// {"t":"roster","rev":7,"macs":[20 x "" or "A0:B7:65:12:34:56"]}
// Index = cup ID. Replaces the RAM roster entirely; DevPi owns identity from
// the first roster line on.
static void jsonRoster(JsonObjectConst root, const char* line) {
  long    rev;
  bool    nUsed[DDM_MAX_CUPS];
  uint8_t nMac[DDM_MAX_CUPS][6];

  if (!jsonIntIn(root["rev"], 1, LONG_MAX, &rev)) { emitErr("invalid", line); return; }
  JsonArrayConst a = root["macs"].as<JsonArrayConst>();
  if (a.isNull() || a.size() != DDM_MAX_CUPS)     { emitErr("invalid", line); return; }

  int i = 0;
  for (JsonVariantConst e : a) {
    const char* s = e.as<const char*>();
    if (s == nullptr) { emitErr("invalid", line); return; }         // not a string
    if (s[0] == 0) {
      nUsed[i] = false;
      memset(nMac[i], 0, 6);
    } else {
      if (!parseMac(s, nMac[i]))        { emitErr("invalid", line); return; }
      if (memcmp(nMac[i], BCAST, 6) == 0) { emitErr("invalid", line); return; }  // never a cup
      for (int j = 0; j < i; j++)                                              // no MAC twice
        if (nUsed[j] && memcmp(nMac[j], nMac[i], 6) == 0) { emitErr("invalid", line); return; }
      nUsed[i] = true;
    }
    i++;
  }

  // 1. Replace the table. A slot keeps its stats only if the same MAC stays
  //    in the same slot; every other slot starts from zero.
  memcpy(rosterOld, roster, sizeof(roster));
  bool reack[DDM_MAX_CUPS];
  int  kept = 0, moved = 0, added = 0, left = 0;
  for (i = 0; i < DDM_MAX_CUPS; i++) {
    reack[i] = false;
    if (!nUsed[i]) { roster[i] = {}; continue; }
    int was = findCupOld(nMac[i]);
    if (was == i) { kept++; continue; }
    roster[i] = {};
    roster[i].used = true;
    memcpy(roster[i].mac, nMac[i], 6);
    if (was >= 0) { moved++; reack[i] = true; }   // a newly added MAC is still sending HELLO
    else          { added++; }                    // and gets acked on the next one
  }
  for (i = 0; i < DDM_MAX_CUPS; i++)              // MACs that left the roster, for the report line
    if (rosterOld[i].used && findCup(rosterOld[i].mac) < 0) left++;

  // 2.
  rosterRev = (uint32_t)rev;

  // 3. Every MAC whose slot changed gets a fresh ack with its new ID queued
  //    now, through the ordinary ack path (the cup adopts it at once). If
  //    the ack is lost, the cup's next telemetry carries the old ID and
  //    handlePacket() re-acks it.
  for (i = 0; i < DDM_MAX_CUPS; i++)
    if (reack[i]) queueAck(i);

  // 4.
  textf("[roster] rev %lu applied: %d kept, %d moved (re-acked), %d added, %d left",
        (unsigned long)rosterRev, kept, moved, added, left);
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

  if      (strcmp(t, "state")  == 0) jsonState(root, line);
  else if (strcmp(t, "roster") == 0) jsonRoster(root, line);
  else if (strcmp(t, "debug")  == 0) jsonDebug(root, line);
  // any other "t": not ours, ignored without a word
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
  textf("---- CUPS seq=%lu state=%u demo=%s rejects=%lu ----",
        (unsigned long)statePkt.seq, statePkt.raceState,
        demoMode ? "on" : "off", (unsigned long)versionRejects);
  if (!broadcasting)
    textf("  (state broadcast OFF: waiting for a JSON state line or a typed command)");
  if (rxQueueDrops)
    textf("  (rx queue dropped %lu packet(s))", (unsigned long)rxQueueDrops);
  textf(" id mac                age_ms   drop  rssi  up_rssi  status");
  bool any = false;
  for (int i = 0; i < DDM_MAX_CUPS; i++) {
    if (!roster[i].used) continue;
    any = true;
    char m[18]; macFmt(roster[i].mac, m);
    if (roster[i].lastSeenMs == 0) {
      textf(" %2d %s       -      -     -        -  NEVER", i, m);
    } else {
      uint32_t age = now - roster[i].lastSeenMs;
      textf(" %2d %s %7lu %6lu  %4d     %4d  %s",
            i, m, (unsigned long)age,
            (unsigned long)roster[i].dropped,
            roster[i].rssi, roster[i].upRssi,
            (age > STALE_MS) ? "STALE" : "OK");
    }
  }
  if (!any) textf("  (no cups yet - waiting for HELLO)");
}

// ---------------------------------------------------------------------------
void setup() {
  // Both buffer sizes must be set before begin(). The default 256-byte RX
  // buffer is smaller than a roster line (over 400 bytes); the TX buffer
  // lets a burst of telem lines drain in the background instead of stalling
  // loop() at 115200 baud.
  Serial.setRxBufferSize(DDM_LINE_MAX);
  Serial.setTxBufferSize(2048);
  Serial.begin(115200);
  delay(300);

  textf("DDM La Quiniela gateway - ESP-NOW <-> serial JSON bridge");
  textf("proto v%d, line proto v%d, channel %d, max cups %d",
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
  esp_now_register_send_cb(onDataSent);
  addPeer(BCAST);                          // the one permanent peer

  // Seed the roster from the compile-time table (bench mode, until DevPi's
  // first roster line replaces it)
  for (int i = 0; i < KNOWN_CUPS_N && i < DDM_MAX_CUPS; i++) {
    roster[i] = {};
    roster[i].used = true;
    memcpy(roster[i].mac, KNOWN_CUPS[i].mac, 6);
  }
  textf("roster seeded with %d known cup(s)", KNOWN_CUPS_N);

  // Initial broadcast state. Nothing is sent until broadcasting starts.
  statePkt = {};
  statePkt.version   = DDM_PROTO_VERSION;
  statePkt.msgType   = DDM_MSG_STATE;
  statePkt.raceState = DDM_BETTING_OPEN;

#if DDM_AUTO_DEMO
  textf("[demo] on (DDM_AUTO_DEMO build): broadcasting from boot; a JSON state line takes over");
#else
  textf("silent: no state broadcast until a JSON state line or a typed state/horse/scratch/demo command");
#endif

  emitHello();
  tHello = millis();
}

void loop() {
  uint32_t now = millis();

  pollSerial();
  drainRx();
  ackTick(now);

  if (demoMode && now - tDemo >= DEMO_STEP_MS) {
    tDemo = now;
    demoStep++;
    // Walk every slot through a different horse so any cup that hears us
    // visibly reacts, roster or not.
    for (int i = 0; i < DDM_MAX_CUPS; i++)
      statePkt.horseForCup[i] = (uint8_t)(((demoStep + i * 5) % 20) + 1);
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
    emitStatus();                       // sets tStatus
  }

  if (debugText && now - tSummary >= SUMMARY_MS) {
    tSummary = now;
    printSummary();
  }
}
