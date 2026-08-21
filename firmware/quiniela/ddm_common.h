/*
 * ddm_common.h — La Quiniela ESP-NOW wire protocol
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
#define DDM_PROTO_VERSION 1

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

/* Maximum number of cups in the fleet. Cup IDs are 0..DDM_MAX_CUPS-1 and are
 * used directly as indices into the per-cup arrays below. Raising this grows
 * DdmStatePacket by 2 bytes per cup — watch the 250-byte ESP-NOW ceiling. */
#define DDM_MAX_CUPS 20

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
  DDM_MSG_STATE     = 1,  /* gateway -> all cups, broadcast */
  DDM_MSG_TELEMETRY = 2,  /* cup -> gateway, unicast */
  DDM_MSG_HELLO     = 3   /* cup -> gateway, unicast, announces presence */
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
 * Gateway -> all cups (broadcast).
 *
 * The gateway repeats this at a fixed cadence; it is state, not events, so a
 * dropped packet self-heals on the next one. `seq` lets each cup notice the
 * gap and report it.
 * ------------------------------------------------------------------------- */
struct __attribute__((packed)) DdmStatePacket {
  uint8_t  version;                    /* == DDM_PROTO_VERSION, else reject   */
  uint8_t  msgType;                    /* == DDM_MSG_STATE                    */
  uint32_t seq;                        /* monotonic; cups detect drops via gaps */
  uint8_t  raceState;                  /* a DdmRaceState value                */
  uint8_t  horseForCup[DDM_MAX_CUPS];  /* index = cup ID, value = horse 1..20,
                                        * 0 = unassigned                      */
  uint8_t  scratched[DDM_MAX_CUPS];    /* index = cup ID, 0 = running, 1 = scratched */
};

/* ---------------------------------------------------------------------------
 * Cup -> gateway (unicast).
 *
 * Sent on a slow cadence and on change. Also used for DDM_MSG_HELLO, where the
 * measurement fields may be zero and only version/msgType/cupId are meaningful.
 * ------------------------------------------------------------------------- */
struct __attribute__((packed)) DdmTelemetryPacket {
  uint8_t  version;     /* == DDM_PROTO_VERSION, else reject                  */
  uint8_t  msgType;     /* DDM_MSG_TELEMETRY or DDM_MSG_HELLO                 */
  uint8_t  cupId;       /* 0..DDM_MAX_CUPS-1                                  */
  uint32_t seq;         /* last state seq this cup saw                        */
  uint32_t dropped;     /* state packets this cup detected as missed          */
  int32_t  rawWeight;   /* HX711 raw reading, uncalibrated                    */
  uint16_t tokenCount;  /* token count computed from rawWeight                */
  int8_t   rssi;        /* RSSI of the last packet heard from the gateway, dBm */
};

/* ---------------------------------------------------------------------------
 * Layout guards. See the header comment before touching these numbers.
 * ------------------------------------------------------------------------- */
static_assert(sizeof(DdmStatePacket) == 47,
              "DdmStatePacket layout changed — bump DDM_PROTO_VERSION and reflash every device");
static_assert(sizeof(DdmTelemetryPacket) == 18,
              "DdmTelemetryPacket layout changed — bump DDM_PROTO_VERSION and reflash every device");

static_assert(sizeof(DdmStatePacket) <= DDM_ESPNOW_MAX_PAYLOAD,
              "DdmStatePacket exceeds the 250-byte ESP-NOW payload limit");
static_assert(sizeof(DdmTelemetryPacket) <= DDM_ESPNOW_MAX_PAYLOAD,
              "DdmTelemetryPacket exceeds the 250-byte ESP-NOW payload limit");

#endif /* DDM_COMMON_H */
