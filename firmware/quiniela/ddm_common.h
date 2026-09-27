/*
 * ddm_common.h — La Quiniela ESP-NOW wire protocol, version 2
 *
 * SHARED FILE. This header is included by BOTH ddm_gateway and ddm_cup.
 * The structs below are the literal bytes that go over the air: ESP-NOW hands
 * the receiver a raw buffer which is cast straight into one of these structs
 * with no length check, no schema, and no validation. If the two sides disagree
 * about a single field, you do not get an error — you get silently wrong horse
 * numbers and garbage weights.
 *
 * Therefore:
 *   - Any change to this file requires REFLASHING EVERY DEVICE (gateway + all cups).
 *   - Any change that alters a struct's layout must also bump DDM_PROTO_VERSION.
 *   - The static_asserts at the bottom exist so a careless field edit breaks the
 *     build loudly instead of corrupting data at runtime. Do not "fix" a failing
 *     static_assert by editing the expected size — fix the struct, or bump the
 *     version and reflash everything.
 *
 * Version 2 (2026-09-27): the cup owns its horse number. It is set on the cup
 * (touch menu HORSE, or serial n<N>), saved in the cup's NVS and reported in
 * every packet the cup sends. There are no cup IDs, no MAC roster and no
 * per-cup table anywhere on the air: the state packet is keyed by horse
 * number (scratched bits, renumber pairs, the three results) and every cup
 * reads out of it what applies to its own horse. Whatever the gateway keeps
 * per cup (MAC, last horse, tokens, signal) is the gateway's business and
 * never crosses the radio.
 */

#ifndef DDM_COMMON_H
#define DDM_COMMON_H

#include <stdint.h>

/* ---------------------------------------------------------------------------
 * Protocol version
 *
 * First byte of every packet. A receiver MUST reject any packet whose version
 * does not match its own compiled-in value, and MUST count the rejection so a
 * half-flashed fleet shows up as a rising counter instead of erratic behavior.
 * Bump this whenever a struct's layout or field meaning changes.
 * ------------------------------------------------------------------------- */
#define DDM_PROTO_VERSION 2

/* ---------------------------------------------------------------------------
 * Radio channel
 *
 * ESP-NOW is not routed and does not scan: both ends must be on the SAME
 * channel or they simply never hear each other. There is no error, no retry,
 * no association — just silence. This value is pinned here so gateway and cups
 * cannot drift apart.
 *
 * Set this to a quiet 2.4GHz channel at the venue. Channels 1, 6, and 11 are
 * the non-overlapping ones; pick whichever is least congested where the cups
 * will actually sit, and reflash every device when you change it.
 * ------------------------------------------------------------------------- */
#define DDM_ESPNOW_CHANNEL 6

/* How many cups the gateway keeps track of at once (its internal table: MAC,
 * last horse, tokens, signal). Twenty on the mantle plus spares. Nothing on
 * the air depends on this number. */
#define DDM_MAX_CUPS 24

/* Highest horse (program) number a cup can carry. 1..20 is the field; 21..24
 * are the also-eligibles, which keep their own program number when they draw
 * in (a replacement scratch renumbers the cup: the cup that was 9 becomes 22,
 * through the renum pairs below). 0 means "no horse set". A uint8_t holds it;
 * the scratched bitmask below has room for it (bits 1..24 of 32). The gateway
 * validates against it; the cup's cloth table goes up to it; pi5 mirrors it
 * (la_quiniela/protocol.py MAX_HORSE). */
#define DDM_MAX_HORSE 24

/* Renumber pairs and results carried by every state packet. */
#define DDM_RENUM_SLOTS  4
#define DDM_RESULT_SLOTS 3

/* ---------------------------------------------------------------------------
 * ESP-NOW payload ceiling
 *
 * The ESP-NOW API refuses to send more than 250 bytes in a single frame.
 * ------------------------------------------------------------------------- */
#define DDM_ESPNOW_MAX_PAYLOAD 250

/* ---------------------------------------------------------------------------
 * Message types — the second byte of every packet.
 * ------------------------------------------------------------------------- */
enum DdmMsgType : uint8_t {
  DDM_MSG_STATE     = 1,  /* gateway -> all cups, broadcast                          */
  DDM_MSG_TELEMETRY = 2,  /* cup -> gateway, unicast once the gateway's MAC is known  */
  DDM_MSG_HELLO     = 3   /* cup -> gateway, broadcast until then; same payload      */
};

/* ---------------------------------------------------------------------------
 * Race states — the phase of the evening the whole room is in.
 * ------------------------------------------------------------------------- */
enum DdmRaceState : uint8_t {
  DDM_PRE_RACE      = 0,  /* idle, before betting opens */
  DDM_BETTING_OPEN  = 1,  /* cups accept tokens */
  DDM_FINAL_CALL    = 2,  /* last chance to bet */
  DDM_AT_THE_POST   = 3,  /* betting closed, horses lining up */
  DDM_RUNNING       = 4,  /* race in progress */
  DDM_WINNER        = 5,  /* winner announced */
  DDM_AFTER_PARTY   = 6   /* race over, ambient mode */
};

/* ---------------------------------------------------------------------------
 * Gateway -> all cups (broadcast), keyed by horse number.
 *
 * The gateway repeats this at a fixed cadence; it is state, not events, so a
 * dropped packet self-heals on the next one. `seq` lets each cup notice the
 * gap and report it. Every cup reads only what applies to its own horse:
 *   scratched  bit n set = horse n is scratched with no replacement (bits
 *              1..DDM_MAX_HORSE; bit 0 and 25..31 are always clear).
 *   renum      {from, to} pairs, {0, 0} = unused. A cup whose horse equals
 *              `from` adopts `to`, saves it to NVS and reports `to` from then
 *              on. A pair stays in the packet as long as pi5 sends it;
 *              adopting twice is harmless because `from` no longer matches.
 *   results    the WIN, PLACE and SHOW horse numbers, 0 = not yet known.
 * ------------------------------------------------------------------------- */
struct __attribute__((packed)) DdmStatePacket {
  uint8_t  version;                          /* == DDM_PROTO_VERSION, else reject   */
  uint8_t  msgType;                          /* == DDM_MSG_STATE                    */
  uint32_t seq;                              /* monotonic; cups detect drops via gaps */
  uint8_t  raceState;                        /* a DdmRaceState value                */
  uint32_t scratched;                        /* bit n = horse n scratched           */
  uint8_t  renum[DDM_RENUM_SLOTS][2];        /* {from, to} pairs, {0,0} = unused    */
  uint8_t  results[DDM_RESULT_SLOTS];        /* win, place, show; 0 = not yet       */
};

/* ---------------------------------------------------------------------------
 * Cup -> gateway.
 *
 * Sent every 2 s once the cup knows the gateway's MAC (unicast, TELEMETRY),
 * every 1 s as a broadcast before that (HELLO): the same payload either way,
 * so the gateway treats both alike. `horse` is the cup's own claim, 0 = none
 * set yet.
 * ------------------------------------------------------------------------- */
struct __attribute__((packed)) DdmTelemetryPacket {
  uint8_t  version;     /* == DDM_PROTO_VERSION, else reject                  */
  uint8_t  msgType;     /* DDM_MSG_TELEMETRY or DDM_MSG_HELLO                 */
  uint8_t  horse;       /* the cup's horse number, 1..DDM_MAX_HORSE, 0 = none */
  uint32_t seq;         /* last state seq this cup saw                        */
  uint32_t dropped;     /* state packets this cup detected as missed          */
  int32_t  rawWeight;   /* HX711 raw reading, uncalibrated                    */
  uint16_t tokenCount;  /* token count computed from rawWeight                */
  int8_t   rssi;        /* RSSI of the last packet heard from the gateway, dBm */
};

/* The one bit test both ends use. Horse 0 (none) is never scratched. */
static inline bool ddmIsScratched(uint32_t bits, uint8_t horse) {
  return horse >= 1 && horse <= DDM_MAX_HORSE && ((bits >> horse) & 1u) != 0;
}

/* ---------------------------------------------------------------------------
 * Layout guards. See the header comment before touching these numbers.
 * ------------------------------------------------------------------------- */
static_assert(DDM_MAX_HORSE <= 31,
              "the scratched bitmask holds horses 1..31");
static_assert(sizeof(DdmStatePacket) == 22,
              "DdmStatePacket layout changed — bump DDM_PROTO_VERSION and reflash every device");
static_assert(sizeof(DdmTelemetryPacket) == 18,
              "DdmTelemetryPacket layout changed — bump DDM_PROTO_VERSION and reflash every device");

static_assert(sizeof(DdmStatePacket) <= DDM_ESPNOW_MAX_PAYLOAD,
              "DdmStatePacket exceeds the 250-byte ESP-NOW payload limit");
static_assert(sizeof(DdmTelemetryPacket) <= DDM_ESPNOW_MAX_PAYLOAD,
              "DdmTelemetryPacket exceeds the 250-byte ESP-NOW payload limit");

#endif /* DDM_COMMON_H */
