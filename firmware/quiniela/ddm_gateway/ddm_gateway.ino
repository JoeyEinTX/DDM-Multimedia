/*
 * ddm_gateway.ino — La Quiniela ESP-NOW gateway (bench test)
 *
 * Plain ESP32 WROOM-32 dev board. No display. USB serial to DevPi (or a
 * laptop, or nothing — boots straight into demo mode so a USB brick is
 * enough to run the range test).
 *
 * Role: broadcast the shared DdmStatePacket to every cup twice a second and
 * collect telemetry back. Signal quality is the point of this build — the
 * 5-second summary table is the range-test readout.
 *
 * No WiFi association, no AP, no MQTT, no OTA. ESP-NOW only, pinned to
 * DDM_ESPNOW_CHANNEL from ddm_common.h (symlinked into this folder — see
 * ../README.md).
 *
 * Serial: 115200, newline-terminated commands. Type `help`.
 *
 * BOARD: ESP32 Dev Module
 */

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include "ddm_common.h"

// ===========================================================================
// KNOWN_CUPS — paste real cup MACs here.
//
// Index in this table == cup ID. Collect the MACs from each cup's waiting
// screen (big gold text) or from this gateway's serial log: any cup not
// listed here gets the next free ID at runtime and its MAC is printed as a
// NEWCUP line formatted for pasting straight into this table.
//
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
// Timing
// ---------------------------------------------------------------------------
#define BROADCAST_MS   500     // state broadcast cadence
#define SUMMARY_MS    5000     // summary table cadence
#define STALE_MS      3000     // silent longer than this -> STALE
#define DEMO_STEP_MS  3000     // demo horse walk cadence

static const uint8_t BCAST[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

// ---------------------------------------------------------------------------
// Cup roster — fixed array, index == cup ID
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

// The packet we broadcast. Mutated by serial commands and demo mode.
DdmStatePacket statePkt;

uint32_t versionRejects = 0;
bool     demoMode       = true;   // on at boot: standalone off a USB brick
uint32_t demoStep       = 0;

uint32_t tBroadcast = 0, tSummary = 0, tDemo = 0;

// ---------------------------------------------------------------------------
static void macFmt(const uint8_t* m, char* out /* >= 18 bytes */) {
  snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
           m[0], m[1], m[2], m[3], m[4], m[5]);
}

static int findCup(const uint8_t* mac) {
  for (int i = 0; i < DDM_MAX_CUPS; i++)
    if (roster[i].used && memcmp(roster[i].mac, mac, 6) == 0) return i;
  return -1;
}

static void addPeer(const uint8_t* mac) {
  if (esp_now_is_peer_exist(mac)) return;
  esp_now_peer_info_t p = {};
  memcpy(p.peer_addr, mac, 6);
  p.channel = DDM_ESPNOW_CHANNEL;
  p.ifidx   = WIFI_IF_STA;
  p.encrypt = false;
  esp_now_add_peer(&p);
}

// Assign the next free ID to a new MAC. Returns -1 if the roster is full.
static int addCup(const uint8_t* mac) {
  for (int i = 0; i < DDM_MAX_CUPS; i++) {
    if (!roster[i].used) {
      roster[i] = {};
      roster[i].used = true;
      memcpy(roster[i].mac, mac, 6);
      addPeer(mac);

      char m[18]; macFmt(mac, m);
      Serial.printf("NEWCUP id=%d mac=%s\n", i, m);
      Serial.printf("  paste into KNOWN_CUPS[]:  { { 0x%02X, 0x%02X, 0x%02X, 0x%02X, 0x%02X, 0x%02X } },  // cup %d\n",
                    mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], i);
      return i;
    }
  }
  Serial.println("ERR roster full, cup ignored");
  return -1;
}

// ---------------------------------------------------------------------------
// Hello ack — gateway -> cup, unicast.
//
// PROTOCOL NOTE: ddm_common.h defines no packet that tells a cup its own ID,
// so the ack reuses the DdmTelemetryPacket layout in the reverse direction
// with msgType = DDM_MSG_HELLO and cupId = the assigned ID. All other fields
// are zero. Byte-compatible with the shared header; candidate for a proper
// assignment packet in protocol v2.
// ---------------------------------------------------------------------------
static void sendHelloAck(int id) {
  DdmTelemetryPacket ack = {};
  ack.version = DDM_PROTO_VERSION;
  ack.msgType = DDM_MSG_HELLO;
  ack.cupId   = (uint8_t)id;
  esp_now_send(roster[id].mac, (const uint8_t*)&ack, sizeof(ack));
}

// ---------------------------------------------------------------------------
// Receive path. Core 3.x hands us esp_now_recv_info_t (with per-packet RSSI);
// core 2.x hands us just the MAC. Same guard pattern as the LEDC handling in
// the cup sketch.
// ---------------------------------------------------------------------------
static void handlePacket(const uint8_t* mac, int8_t upRssi,
                         const uint8_t* data, int len) {
  if (len < 2) return;
  if (data[0] != DDM_PROTO_VERSION) { versionRejects++; return; }
  if (len != (int)sizeof(DdmTelemetryPacket)) return;

  const DdmTelemetryPacket* p = (const DdmTelemetryPacket*)data;
  int id = findCup(mac);

  if (p->msgType == DDM_MSG_HELLO) {
    if (id < 0) id = addCup(mac);
    if (id < 0) return;
    roster[id].lastSeenMs = millis();
    roster[id].upRssi     = upRssi;
    sendHelloAck(id);
    char m[18]; macFmt(mac, m);
    Serial.printf("HELLO cup=%d mac=%s up_rssi=%d\n", id, m, upRssi);

  } else if (p->msgType == DDM_MSG_TELEMETRY) {
    if (id < 0) {                  // gateway rebooted, cup still has old ID:
      id = addCup(mac);            // re-adopt it and re-ack so it re-syncs
      if (id < 0) return;
      sendHelloAck(id);
    }
    CupSlot& c = roster[id];
    c.lastSeenMs = millis();
    c.lastSeq    = p->seq;
    c.dropped    = p->dropped;
    c.rssi       = p->rssi;
    c.upRssi     = upRssi;
    c.tokenCount = p->tokenCount;

    // One line per packet, parseable, for DevPi later
    Serial.printf("TELEM cup=%u horse=%u seq=%lu dropped=%lu rssi=%d up_rssi=%d tokens=%u\n",
                  (unsigned)id, statePkt.horseForCup[id],
                  (unsigned long)p->seq, (unsigned long)p->dropped,
                  p->rssi, upRssi, p->tokenCount);
  }
}

#if ESP_ARDUINO_VERSION_MAJOR >= 3
static void onDataRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
  int8_t rssi = info->rx_ctrl ? info->rx_ctrl->rssi : 0;
  handlePacket(info->src_addr, rssi, data, len);
}
#else
static void onDataRecv(const uint8_t* mac, const uint8_t* data, int len) {
  handlePacket(mac, 0, data, len);   // core 2.x recv path exposes no RSSI
}
#endif

// ---------------------------------------------------------------------------
// Serial commands
// ---------------------------------------------------------------------------
static void printHelp() {
  Serial.println("Commands (newline-terminated):");
  Serial.println("  state <0-6>              set raceState  (0 PRE_RACE 1 BETTING_OPEN 2 FINAL_CALL");
  Serial.println("                           3 AT_THE_POST 4 RUNNING 5 WINNER 6 AFTER_PARTY)");
  Serial.println("  horse <cupId> <0-20>     assign horse to cup (0 = unassigned)");
  Serial.println("  scratch <cupId> <0|1>    set/clear scratched flag");
  Serial.println("  roster                   dump MAC-to-ID table");
  Serial.println("  demo                     toggle demo mode (horse walk every 3s)");
  Serial.println("  help                     this text");
  Serial.println("Any state/horse/scratch command turns demo mode OFF.");
}

static void printRoster() {
  Serial.println("ROSTER id mac               source");
  for (int i = 0; i < DDM_MAX_CUPS; i++) {
    if (!roster[i].used) continue;
    char m[18]; macFmt(roster[i].mac, m);
    Serial.printf("ROSTER %2d %s %s\n", i, m,
                  (i < KNOWN_CUPS_N) ? "KNOWN_CUPS[]" : "runtime");
  }
}

static void demoOff(const char* why) {
  if (demoMode) {
    demoMode = false;
    Serial.printf("[demo] off (%s)\n", why);
  }
}

static void handleCommand(char* line) {
  while (*line == ' ') line++;
  if (*line == 0) return;

  int a = -1, b = -1;

  if (strncmp(line, "state ", 6) == 0 && sscanf(line + 6, "%d", &a) == 1) {
    if (a < DDM_PRE_RACE || a > DDM_AFTER_PARTY) { Serial.println("ERR state 0-6"); return; }
    demoOff("state command");
    statePkt.raceState = (uint8_t)a;
    Serial.printf("OK state=%d\n", a);

  } else if (strncmp(line, "horse ", 6) == 0 && sscanf(line + 6, "%d %d", &a, &b) == 2) {
    if (a < 0 || a >= DDM_MAX_CUPS) { Serial.println("ERR cupId 0-19"); return; }
    if (b < 0 || b > 20)            { Serial.println("ERR horse 0-20"); return; }
    demoOff("horse command");
    statePkt.horseForCup[a] = (uint8_t)b;
    Serial.printf("OK horse cup=%d -> %d\n", a, b);

  } else if (strncmp(line, "scratch ", 8) == 0 && sscanf(line + 8, "%d %d", &a, &b) == 2) {
    if (a < 0 || a >= DDM_MAX_CUPS) { Serial.println("ERR cupId 0-19"); return; }
    if (b != 0 && b != 1)           { Serial.println("ERR scratch 0|1"); return; }
    demoOff("scratch command");
    statePkt.scratched[a] = (uint8_t)b;
    Serial.printf("OK scratch cup=%d -> %d\n", a, b);

  } else if (strcmp(line, "roster") == 0) {
    printRoster();

  } else if (strcmp(line, "demo") == 0) {
    demoMode = !demoMode;
    Serial.printf("[demo] %s\n", demoMode ? "on" : "off");

  } else if (strcmp(line, "help") == 0) {
    printHelp();

  } else {
    Serial.println("ERR unknown command, try: help");
  }
}

static void pollSerial() {
  static char buf[64];
  static int  n = 0;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      buf[n] = 0;
      handleCommand(buf);
      n = 0;
    } else if (n < (int)sizeof(buf) - 1) {
      buf[n++] = c;
    }
  }
}

// ---------------------------------------------------------------------------
// Summary — the range-test readout
// ---------------------------------------------------------------------------
static void printSummary() {
  uint32_t now = millis();
  Serial.printf("---- CUPS seq=%lu state=%u demo=%s rejects=%lu ----\n",
                (unsigned long)statePkt.seq, statePkt.raceState,
                demoMode ? "on" : "off", (unsigned long)versionRejects);
  Serial.println(" id mac                age_ms   drop  rssi  up_rssi  status");
  bool any = false;
  for (int i = 0; i < DDM_MAX_CUPS; i++) {
    if (!roster[i].used) continue;
    any = true;
    char m[18]; macFmt(roster[i].mac, m);
    if (roster[i].lastSeenMs == 0) {
      Serial.printf(" %2d %s       -      -     -        -  NEVER\n", i, m);
    } else {
      uint32_t age = now - roster[i].lastSeenMs;
      Serial.printf(" %2d %s %7lu %6lu  %4d     %4d  %s\n",
                    i, m, (unsigned long)age,
                    (unsigned long)roster[i].dropped,
                    roster[i].rssi, roster[i].upRssi,
                    (age > STALE_MS) ? "STALE" : "OK");
    }
  }
  if (!any) Serial.println("  (no cups yet — waiting for HELLO)");
}

// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("DDM La Quiniela gateway — ESP-NOW bench test");
  Serial.printf("proto v%d, channel %d, max cups %d\n",
                DDM_PROTO_VERSION, DDM_ESPNOW_CHANNEL, DDM_MAX_CUPS);

  // ESP-NOW only: STA mode, never associated, pinned channel
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);
  esp_wifi_set_channel(DDM_ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);

  Serial.print("gateway MAC: ");
  Serial.println(WiFi.macAddress());

  if (esp_now_init() != ESP_OK) {
    Serial.println("FATAL esp_now_init failed");
    while (true) delay(1000);
  }
  esp_now_register_recv_cb(onDataRecv);
  addPeer(BCAST);

  // Seed roster from the compile-time table
  for (int i = 0; i < KNOWN_CUPS_N && i < DDM_MAX_CUPS; i++) {
    roster[i] = {};
    roster[i].used = true;
    memcpy(roster[i].mac, KNOWN_CUPS[i].mac, 6);
    addPeer(roster[i].mac);
  }
  Serial.printf("roster seeded with %d known cup(s)\n", KNOWN_CUPS_N);

  // Initial broadcast state
  statePkt = {};
  statePkt.version   = DDM_PROTO_VERSION;
  statePkt.msgType   = DDM_MSG_STATE;
  statePkt.raceState = DDM_BETTING_OPEN;

  Serial.println("[demo] on (boot default — type 'demo' to toggle, 'help' for commands)");
}

void loop() {
  uint32_t now = millis();

  pollSerial();

  if (demoMode && now - tDemo >= DEMO_STEP_MS) {
    tDemo = now;
    demoStep++;
    // Walk every slot through a different horse so any cup that hears us
    // visibly reacts, roster or not.
    for (int i = 0; i < DDM_MAX_CUPS; i++)
      statePkt.horseForCup[i] = (uint8_t)(((demoStep + i * 5) % 20) + 1);
  }

  if (now - tBroadcast >= BROADCAST_MS) {
    tBroadcast = now;
    statePkt.seq++;
    // Repeat-broadcast, no acks: a cup that misses one gets the next in 500ms
    esp_now_send(BCAST, (const uint8_t*)&statePkt, sizeof(statePkt));
  }

  if (now - tSummary >= SUMMARY_MS) {
    tSummary = now;
    printSummary();
  }
}
