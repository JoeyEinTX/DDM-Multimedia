# DDM Splash Display

A Flask-driven TV splash slideshow for the **Derby de Mayo** annual party.
Runs on a dedicated Raspberry Pi 4B (Pi OS Bookworm), boots straight into a
Chromium kiosk pointed at `http://localhost:5000/display`, and rotates through
splash pages and trivia cards on a weighted, freshly-shuffled cycle.

This is **Phase 1**: file-based content, no remote upload. Phase 2 will add a
`/upload` endpoint and integration with the Pi 5 dashboard.

---

## Architecture at a glance

```
splash_display/
├── server.py                # Flask app on :5000
├── config.py                # All tunables
├── quiniela.py              # La Quiniela: gateway serial link, betting model, SSE
├── requirements.txt
├── tests/
│   └── test_quiniela.py     # python -m unittest -v tests.test_quiniela
├── tools/
│   └── fake_gateway.py      # dev aid: the real server fed by a synthetic gateway
├── logs/                    # quiniela_YYYY-MM-DD.jsonl event log (git-ignored)
├── content/
│   ├── trivia.json          # Trivia cards by category
│   └── splash_pages.json    # Splash templates with timing
├── templates/
│   ├── base.html
│   ├── slideshow.html       # Master slideshow (kiosk URL target)
│   ├── splash/{countdown,la_subasta,la_quiniela,derby_dash,ddm_brand}.html
│   ├── splash/quiniela_live.html   # the live board layer (not a playlist slide)
│   └── trivia/{fact_card,qa_reveal}.html
├── static/
│   ├── css/ddm_style.css
│   ├── css/quiniela_board.css
│   ├── js/quiniela_board.js        # SSE client + board renderer
│   ├── img/                 # DROP YOUR LOGOS HERE
│   └── fonts/
└── deploy/
    ├── kiosk.sh
    ├── splash_display.service
    └── autostart_setup.md
```

### Routes

| Route | Behavior |
| --- | --- |
| `GET /` | 302 to `/display` |
| `GET /display` | Renders the master slideshow page (kiosk URL) |
| `GET /api/slides` | Returns a freshly shuffled JSON playlist (~35 slides) |
| `GET /api/slide/<id>` | Returns a single slide as an HTML fragment (debugging / Phase 2 hook) |
| `GET /api/quiniela` | La Quiniela betting model as JSON (see [La Quiniela live board](#la-quiniela-live-board)) |
| `GET /api/quiniela/stream` | The same model as Server-Sent Events, on every change |
| `POST /api/quiniela/cmd` | Forwards one whitelisted command line to the cup gateway |

### How the slideshow renders

The slideshow does **not** request `/api/slide/<id>` per slide in normal
operation. Instead, `slideshow.html` server-renders every splash and trivia
template once, stashes them in a hidden `<template>` cache on page load, then
clones + populates them on the client for each playlist entry. This keeps the
crossfade buttery smooth and dodges per-slide network round-trips on the
flaky party Wi-Fi. `/api/slide/<id>` is still implemented as a fallback /
debugging surface and as a clean drop-in point for Phase 2 remote content.

### Weight source-of-truth

`config.SPLASH_WEIGHTS` and `config.TRIVIA_WEIGHTS` are the runtime source of
truth. The `weight` field inside `content/splash_pages.json` is kept for human
reference but is **ignored at runtime** — edit `config.py` to change the mix.

---

## Setup on a fresh Pi 4B (Bookworm)

```bash
sudo apt update
sudo apt install -y python3-pip git chromium-browser

cd ~
git clone <your-repo-url> DDM-Multimedia
cd DDM-Multimedia/splash_display

# Bookworm pip needs --break-system-packages
pip install -r requirements.txt --break-system-packages
```

### Drop in the logos

Place these PNGs in `static/img/` (transparent backgrounds preferred):

- `ddm_master.png`
- `la_subasta.png`
- `la_quiniela.png`
- `derby_dash.png`

If a file is missing the splash template renders a dashed-outline
placeholder labeled with the splash name instead of breaking the slide.

### Install the systemd service + kiosk autostart

See `deploy/autostart_setup.md` for the full Bookworm walk-through (covers
Wayfire/Wayland, labwc, and LXDE/X11). Short version:

```bash
sudo cp deploy/splash_display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now splash_display.service

chmod +x deploy/kiosk.sh
# Wire deploy/kiosk.sh into your desktop autostart per the doc.
```

### Verify

From the Pi:

```bash
curl -fsS http://localhost:5000/api/slides | python3 -m json.tool | head -40
```

Or hit `http://<pi-ip>:5000/display` from any browser on the LAN.

---

## Updating content

Edit `content/trivia.json` or `content/splash_pages.json`, then:

- **Code changes**: `sudo systemctl restart splash_display.service`
- **Content-only changes**: a browser refresh is enough — the playlist is
  rebuilt on every `/api/slides` call, so no service restart needed.

To tune the rotation mix (category weights, timing defaults, target playlist
length, transition speed), edit `config.py` and restart the service.

---

## SSH workflow for remote tweaks

```bash
ssh pi@<splash-pi-ip>
cd ~/DDM-Multimedia
git pull
cd splash_display

# Content/CSS only — no service restart needed; just refresh Chromium:
pkill -f chromium && sleep 2 && ./deploy/kiosk.sh &

# Code change — full restart:
sudo systemctl restart splash_display.service
pkill -f chromium && sleep 2 && ./deploy/kiosk.sh &
```

---

## Debug overlay

Press `d` on the kiosk keyboard (or any client viewing `/display`) to toggle
a small green FPS / playlist diagnostic overlay in the corner. Press `d`
again to hide it.

---

## La Quiniela live board

The party's cup gateway (`firmware/quiniela/ddm_gateway`, a WROOM-32 on USB)
knows every cup's horse, token count and health. Plugged into this Pi it
feeds `quiniela.py`, which keeps a live betting model and serves it to the
TV. Nothing here is required for the slideshow: with no gateway plugged in
(or no pyserial installed) the link idles and `/api/quiniela` reports
`"link_ok": false`.

### Gateway link

- **Port.** `config.GATEWAY_PORT = None` auto-detects: the first
  `/dev/serial/by-id/*` entry whose name contains `CH340`, `1a86` (the
  CH34x vendor id: a CH340G is `usb-1a86_USB2.0-Serial-if00-port0`, newer
  ones `usb-1a86_USB_Serial-...`), `USB2.0-Serial`, `USB_Serial`, `CP210`
  or `ESP32`, case-insensitively. Set it explicitly (`"/dev/ttyUSB0"`)
  when more than one USB-serial device is attached. 115200 8N1.
- **DTR/RTS are never touched.** The port is built unopened, configured,
  taken exclusively and only then opened: the recipe from pi5's bridge
  (`pi5/la_quiniela/bridge.py`). Anything else pulses the ESP32's auto-reset
  circuit, so this way restarting the service does not reboot the gateway.
- **Handshake.** On connect the link writes `json 1`; from then on the
  gateway prints one `{"t":"state",...}` line (up to about 2 KB, every
  roster slot) whenever its picture of the cups changes. Only lines that
  start with `{` and carry `"t":"state"` are parsed. Everything else on the
  port (`# ` text, `hello`, `status`, `telem`, `cup_hello`, `err`) is
  discarded at DEBUG.
- **Re-send rules.** Auto-emit is not persisted on the gateway, so `json 1`
  goes out again when the gateway rebooted, and whenever the port is open
  but no state line has arrived for 5 s (at most once per 5 s). A reboot
  is a `{"t":"hello"}` line while the state stream is silent: the gateway
  prints hello at boot and repeats it every 2 s until it gets a downlink
  state line (pi5's; this display never sends one), so a hello that
  arrives while state lines are still flowing (the last one under 1.5 s
  ago; they come every second once auto-emit is on) is a repeat and is
  dropped. A real reboot stops the state lines, so the next hello re-sends
  `json 1` (at most once per second); the 5 s silence rule is the backstop.
- **Reconnect.** Any serial error, or the port disappearing, closes the
  link; it retries with a 1 s backoff doubling to a 10 s cap, forever. "No
  port found" is logged once at INFO, then at DEBUG.
- **One owner.** Only one process can hold the USB port. pi5's bridge also
  opens the gateway when its `LQ_SERIAL_PORT` is set, so a gateway is wired
  to one host or the other; whichever opens second keeps failing. For the
  same reason, with `config.DEBUG = True` the dev server's reloader parent
  does not start the link; only the child process that serves requests
  does.
- `link_ok` is true while a state line has arrived in the last 5 s. A 1 Hz
  ticker notices the gateway going silent and publishes that change; the
  rest of the model keeps its last values.

### API

| Route | Behavior |
| --- | --- |
| `GET /api/quiniela` | The betting model as JSON |
| `GET /api/quiniela/stream` | Server-Sent Events: the full model on every change |
| `POST /api/quiniela/cmd` | Forward one whitelisted command line to the gateway |

The betting model, as `/api/quiniela` returns it and the stream carries it:

```json
{
  "link_ok": true,
  "race_state": 1, "race_state_name": "BETTING_OPEN",
  "token_value": 1.0,
  "pot": 147.0, "total_tokens": 147,
  "horses": {
    "1": {"tokens": 0, "share": 0, "scratched": false, "online": false, "cup": null},
    "7": {"tokens": 23, "share": 0.1565, "scratched": false, "online": true, "cup": 0}
  },
  "leader": 7,
  "events": [ {"horse": 7, "delta": 1, "ts": 1695400000.0} ],
  "updated": 1695400000.0,
  "board_states": [1, 2, 3, 4]
}
```

- `horses` has every horse `"1"`..`"20"`; a horse with no cup assigned looks
  like the `"1"` entry above.
- `share` = tokens / total_tokens (0 when the pot is empty). `leader` is the
  horse with most tokens (lowest number on a tie), `null` when nobody has
  bet.
- `online` = the cup reported within the last 6 s (`age` 0..6000 ms).
- `events` are token deltas between consecutive state lines, newest first,
  last 8. The first state line after start-up is the baseline and adds none.
- Two cups claiming one horse: the lowest cup id wins (one WARNING logged).
  Horse values outside 1..20 are ignored.
- `board_states` = `config.QUINIELA_BOARD_STATES`, so the page does not
  hard-code them.

```bash
curl -s localhost:5000/api/quiniela | python3 -m json.tool

# SSE. First a `data:` event with the current model, then one per change.
# After every 5 s of silence: a ": heartbeat" comment and an "event: ping"
# carrying {"ts": <unix time>}. An EventSource cannot see comments, so the
# page listens for "ping" and shows NO LINK after 10 s without one.
curl -sN localhost:5000/api/quiniela/stream

curl -s -X POST localhost:5000/api/quiniela/cmd \
     -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'
# 200 {"ok": true}                                    written to the gateway
# 200 {"ok": false}                                   port open, write failed
# 400 {"ok": false, "error": "..."}                   empty, over 200 chars, more
#                                                     than one line, or the first
#                                                     word is not one of
#                                                     state horse scratch demo roster json
# 503 {"ok": false, "error": "gateway not connected"}
```

Stream headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`,
`X-Accel-Buffering: no`, `Connection: keep-alive` (Flask's dev server, which
the systemd unit runs, rewrites that last one to `close`; the stream still
runs until one side hangs up). Event names: the model arrives as unnamed
`data:` events (`onmessage`), the keep-alive as `ping`.

### Event log

With `config.QUINIELA_LOG = True` every model change (a horse's token count
or scratch flag, or the race state; not `online` / `link_ok` flips) appends
one compact line to `logs/quiniela_YYYY-MM-DD.jsonl` (local date; the
directory is created on first write and is git-ignored):

```json
{"ts":1695400000.0,"race_state":1,"changes":[{"horse":7,"tokens":[22,23]}],"total_tokens":147}
```

`changes` entries are `{"horse": h, "tokens": [old, new]}`,
`{"horse": h, "scratched": [old, new]}` or `{"race_state": [old, new]}`.
A write failure logs one WARNING and disables the log until restart; it
never touches the link or the TV.

### Config

| Key | Default | Meaning |
| --- | --- | --- |
| `GATEWAY_PORT` | `None` | Serial port of the gateway; `None` = auto-detect `/dev/serial/by-id` |
| `TOKEN_VALUE` | `1.00` | Dollars per token; `pot = total_tokens x TOKEN_VALUE` |
| `QUINIELA_LOG` | `True` | Write the JSONL event log |
| `QUINIELA_BOARD_STATES` | `[1, 2, 3, 4]` | Race states in which the board owns the TV (exposed as `board_states`) |

Race states: 0 PRE_RACE, 1 BETTING_OPEN, 2 FINAL_CALL, 3 AT_THE_POST,
4 RUNNING, 5 WINNER, 6 AFTER_PARTY.

### The board

The TV board lives in `templates/splash/quiniela_live.html`, rendered once
into `slideshow.html` as a fixed full-screen layer above the two slide
layers (it is **not** a playlist slide and is not in `splash_pages.json`).
`static/js/quiniela_board.js` drives it; `static/css/quiniela_board.css`
styles it. Vanilla JS, no CDN, relative URLs only, so the kiosk can be a
different machine from the server. With no gateway plugged in the layer
stays hidden and the slideshow is indistinguishable from before.

**Takeover rule.** On load the page fetches `/api/quiniela` once and
subscribes to `/api/quiniela/stream` (reconnecting on error with a 1 s
backoff doubling to 30 s, reset by the next message). Whenever
`board_states` (the model's copy of `config.QUINIELA_BOARD_STATES`; the
page never hard-codes it) contains `race_state`:

- the playlist pauses in place (`window.ddmSlideshow.hold()` clears the
  slide timers, the current slide stays where it is) and the board
  crossfades in full-screen, over the same `TRANSITION_FADE_MS`;
- when the state leaves the set the board crossfades out and
  `release()` restarts the same slide's timers from zero — same slide,
  fresh dwell. The spacebar pause still wins: a paused show stays paused.
- a dead stream never hides the board. It stays up with its last data;
  only a model saying the state is outside `board_states` takes it down.

**Banner** (top right), by `race_state`:

| State | Banner | Tiles, pot, ticker |
| --- | --- | --- |
| 1 `BETTING_OPEN` | BETTING OPEN (green) | live |
| 2 `FINAL_CALL` | FINAL CALL (scarlet, pulsing — the only looping animation) | live |
| 3 `AT_THE_POST`, 4 `RUNNING` | BETTING CLOSED | **frozen** at the values shown when the state was entered; back in 1 or 2 they go live again |

The freeze only holds while the board is up. A board coming up already in
3 or 4 — a page loaded mid-race, or the server restarted during the race —
paints the live model first, so it never shows an earlier hidden paint of
an empty model (POT $0, every count 0).

**Layout** (1920x1080, built for a TV across the room; serape band top
and bottom, the countdown's dark panel look):

- Header: `la_quiniela.png` left; the pot centered and big — `POT $147`
  (whole dollars while `TOKEN_VALUE` is a whole number, else two
  decimals); the banner right.
- Grid: 20 horse tiles, 5 wide x 4 tall, even gutters, filling the width.
  Each tile: the post position in its saddle-cloth colored block on the
  left; the token count on the right, the largest thing on the tile; a
  thin bar along the bottom whose width is the horse's `share` (a full
  tile is 100 % of the pot), in the saddle-cloth color (the near-black
  cloths 6, 17, 19 use their number color), so the field's distribution
  reads at a glance. Scratched: tile dimmed and the cloth greyed, count
  hidden, a red X across the number block. Cup offline (`cup` assigned,
  `online` false): a small dim dot in the tile's corner. Leader: a subtle
  yellow border.
- Ticker: one line along the bottom, the last eight `events` as `#7 +1`
  chips, newest sliding in from the right.

**Motion.** A changed count ticks to the new value over ~500 ms (a
`requestAnimationFrame` tween writing the number) and the tile pulses
once, ~400 ms (scale 1.03 plus a white overlay's opacity). Bars are a
`scaleX` transform with a 600 ms transition. Chips are keyed by
`horse:ts`, so a re-render never replays their entry. Everything is
transform / opacity only so it stays smooth on a Pi 5 in kiosk Chromium;
nothing loops except the FINAL CALL pulse.

**NO LINK mark.** A small dim `NO LINK` mark sits in the top-right corner
of the board (above the banner, never over a tile) when no SSE message of
any kind — a model or a `ping` — has arrived for 10 s, or when the latest
model says `link_ok: false` (the gateway itself has gone quiet). It hides
as soon as either condition clears. It is only ever a mark: the board
keeps its last data and never drops back to trivia on a hiccup.

### Dev aid: fake gateway

`tools/fake_gateway.py` runs the **real** server (`server.py`, every route
and template) on `127.0.0.1` with a synthetic gateway feeding
`quiniela.link.feed_line()` from a thread. No serial port is opened, the
dashboard poller stays off and the JSONL event log is disabled, so it is
safe on any machine:

```bash
cd splash_display
python tools/fake_gateway.py --phase open              # BETTING OPEN, 20 cups, leader, a scratch, an offline cup, recent events
python tools/fake_gateway.py --phase final             # FINAL CALL
python tools/fake_gateway.py --phase closed            # AT_THE_POST: BETTING CLOSED, board frozen
python tools/fake_gateway.py --phase running           # RUNNING: BETTING CLOSED, still frozen
python tools/fake_gateway.py --phase winner            # state 5: the playlist is back
python tools/fake_gateway.py --phase idle              # state 0, link up: plain slideshow
python tools/fake_gateway.py --phase cycle             # idle -> open -> final -> closed -> running -> winner, ~15 s each, forever
python tools/fake_gateway.py --phase open --stop-feed-after 0   # board up, link lost: NO LINK mark
# --port 5077 (default), --period 15 (cycle), --host 127.0.0.1
```

Open the printed URL (`http://127.0.0.1:5077/display`) in a browser. For
automated 1080p frames drive headless Chrome over the DevTools protocol
(`--remote-debugging-port`, `Page.navigate`, wait a few seconds of real
time, `Page.captureScreenshot`): the plain `--screenshot` flags do not
wait for the stream. `--virtual-time-budget` never expires because the
open SSE request keeps headless virtual time paused (Chrome hangs), and
`--timeout` fires before the board's first fetch has rendered.

### First end-to-end test

```bash
# on DevPi, gateway plugged in, splash_display service running
curl -s localhost:5000/api/quiniela | jq .link_ok          # true
# "demo" takes no argument and toggles the gateway's demo mode: the cups walk
# through horse numbers, which proves the link end to end. The next command
# turns demo off again on its own.
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"demo"}'
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"horse 0 7"}'
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'
# TV should now show the board. Drop a token in cup 0. Horse 7 ticks to 1.
```

Unit tests, no port and no network needed:

```bash
cd splash_display && python -m unittest -v tests.test_quiniela
```

---

## Phase 2 stub (planned)

A future `/upload` endpoint will accept new trivia cards / splash pages from
the Pi 5 dashboard so we can push fresh content to the TV without an SSH
session. The current code is structured around `load_trivia()` /
`load_splash_pages()` and `build_playlist()` so a remote-content source can
be slotted in without touching the route layer or the slideshow frontend.

---

## Things this project deliberately does NOT do

- No external CDN dependencies — all CSS/JS/fonts are local. Party Wi-Fi
  is flaky and we cannot rely on the open internet.
- No `localStorage` or any browser storage — server is the only source of
  truth.
- No `/upload` endpoint (Phase 2).
- No modifications to the Pi 5 dashboard at `pi5/`. Splash and dashboard are
  independent Flask apps running on independent Pis.
