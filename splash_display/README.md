# DDM Splash Display

A Flask-driven TV splash slideshow for the **Derby de Mayo** annual party.
Runs on a dedicated Raspberry Pi 4B (Pi OS Bookworm), boots straight into a
Chromium kiosk pointed at `http://localhost:5001/display`, and rotates through
splash pages and trivia cards on a weighted, freshly-shuffled cycle.

This is **Phase 1**: file-based content, no remote upload. Phase 2 will add a
`/upload` endpoint and integration with the Pi 5 dashboard.

---

## Architecture at a glance

```
splash_display/
├── server.py                # Flask app on :5001 (pi5's dashboard keeps :5000)
├── config.py                # All tunables
├── quiniela.py              # La Quiniela: pi5 link (HTTP client), relayed model, SSE
├── requirements.txt
├── tests/
│   └── test_quiniela.py     # python -m unittest -v tests.test_quiniela
├── tools/
│   └── fake_pi5.py          # dev aid: the real server against a synthetic pi5
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
| `GET /api/quiniela` | La Quiniela betting model as JSON, relayed from pi5 (see [La Quiniela live board](#la-quiniela-live-board)) |
| `GET /api/quiniela/stream` | The same model as Server-Sent Events, on every change |
| `POST /api/quiniela/cmd` | Forwards the body to pi5's `/api/quiniela/cmd` and relays its answer |

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
curl -fsS http://localhost:5001/api/slides | python3 -m json.tool | head -40
```

Or hit `http://<pi-ip>:5001/display` from any browser on the LAN.

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
knows every cup's horse, token count and health. It plugs into **DevPi**,
where pi5's bridge (`pi5/la_quiniela/bridge.py`) owns the USB port, keeps
the betting model and serves it. This app is an HTTP client of pi5 and
re-serves the model to the TV, so the board keeps talking to its own origin:

```
gateway ──USB──> pi5 dashboard (:5000)              ──HTTP──> splash (:5001)
                 owns the port; roster + telemetry              quiniela.Pi5Link follows
                 -> betting model                               pi5's stream (polls while
                 GET  /api/quiniela                             it is down) and re-serves
                 GET  /api/quiniela/stream   (SSE)              the same three routes to
                 POST /api/quiniela/cmd                         the TV board (/display)
```

Nothing here is required for the slideshow: with pi5 unreachable the link
keeps retrying, `/api/quiniela` reports `"link_ok": false` with an empty
`board_states`, and the board stays hidden. pyserial is no longer needed on
this Pi: only pi5 opens the USB port, and no serial device is ever touched
here.

### The pi5 link

`quiniela.Pi5Link`, a daemon thread started with the app (`start_link()`),
follows pi5 at `config.PI5_URL`:

- **Stream first.** It opens `GET PI5_URL/api/quiniela/stream` and hands
  every unnamed `data:` event (the full model as JSON) to the relay; pi5's
  `event: ping` (sent after every 5 s of silence) counts as contact. A
  stream silent for 15 s is dead (read timeout).
- **Poll fallback.** When the stream ends or fails (pi5 down or restarting,
  network gone) it logs one WARNING (`pi5 stream lost (...); polling
  /api/quiniela`, then DEBUG for repeats) and polls `GET PI5_URL/api/quiniela`
  once a second for 10 s, then tries the stream again; forever. A failed
  poll sleeps a backoff of 1 s doubling to 10 s (any success resets it),
  so an unreachable pi5 costs two connection attempts every 10 s. Every
  exception is caught and logged; the thread never dies.
- **`link_ok`** as served to the TV = pi5 heard (a model or a ping) within
  the last 7.5 s AND pi5's own `link_ok` (its gateway alive). A 1 Hz ticker
  flips it off when pi5 goes quiet and publishes that change; the rest of
  the model keeps its last values. The next model or ping flips it back.
  7.5 s clears one ping period with margin (pi5 pings after 5 s of silence
  measured from its previous chunk, so a window of exactly 5 s flickered on
  a healthy pi5) and stays under the page's own 10 s NO LINK rule.
- **One client per process.** With `config.DEBUG = True` the dev server's
  reloader parent does not start the link; only the child that serves
  requests does (a second client would just hold a useless request thread
  open on pi5). Under systemd there is one process.

### Ports

pi5's dashboard binds **5000**, this app **5001** (`config.FLASK_PORT`);
sharing DevPi is fine. Only pi5 opens the gateway's USB port. The kiosk
URL (`deploy/kiosk.sh`) and the curls below use 5001; the systemd unit is
unchanged (it runs `server.py`, which reads the port from `config.py`).

### API (relayed)

| Route | Behavior |
| --- | --- |
| `GET /api/quiniela` | The betting model as last received from pi5 |
| `GET /api/quiniela/stream` | Server-Sent Events: the full model on every change (the same bytes pi5's stream carries) |
| `POST /api/quiniela/cmd` | The JSON body is forwarded to pi5's `/api/quiniela/cmd`; pi5 validates the command and its status and answer are relayed unchanged |

The betting model, as `/api/quiniela` returns it and the stream carries it.
pi5 builds it; the splash serves it untouched apart from `link_ok`:

```json
{
  "link_ok": true,
  "race_state": 1, "race_state_name": "BETTING_OPEN",
  "token_value": 1.0,
  "pot": 147.0, "total_tokens": 147,
  "horses": {
    "1": {"tokens": 0, "share": 0, "scratched": false, "online": false, "cup": null},
    "7": {"tokens": 23, "share": 0.1565, "scratched": false, "online": true, "cup": 7}
  },
  "leader": 7,
  "events": [ {"horse": 7, "delta": 1, "ts": 1695400000.0} ],
  "updated": 1695400000.0,
  "board_states": [1, 2, 3, 4]
}
```

- `horses` has every horse `"1"`..`"20"`; a horse with no cup assigned looks
  like the `"1"` entry above. `cup` is pi5's **1-based** cup number (the
  old splash exposed the gateway's 0-based wire slot); the board only tests
  it for `null`.
- `share` = tokens / total_tokens (0 when the pot is empty). `leader` is the
  horse with most tokens (lowest number on a tie), `null` when nobody has
  bet. `online` = pi5 heard the cup within the last 6 s.
- `events` are the last 8 bets, newest first, each `{"horse", "delta",
  "ts"}`.
- `board_states` are the race states in which the board owns the TV; pi5
  decides them. Until pi5 has been heard the splash serves an empty model:
  `link_ok` false, race state 0, `token_value` 1.0, every horse unassigned,
  `board_states` `[]` (so the board stays hidden).

```bash
curl -s localhost:5001/api/quiniela | python3 -m json.tool

# SSE. First a `data:` event with the current model, then one per change.
# After every 5 s of silence: a ": heartbeat" comment and an "event: ping"
# carrying {"ts": <unix time>}. An EventSource cannot see comments, so the
# page listens for "ping" and shows NO LINK after 10 s without one.
curl -sN localhost:5001/api/quiniela/stream

curl -s -X POST localhost:5001/api/quiniela/cmd \
     -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'
# pi5's answer, relayed as is:
#   200 {"ok": true, "gateway_online": true, ...}      applied and sent to the gateway
#   200 {"ok": true, "gateway_online": false, ...}     applied, gateway offline (pi5 re-sends on the next hello)
#   400 {"ok": false, "error": "..."}                  pi5 rejected the command
#   503 {"ok": false, "error": "bridge not initialised"} pi5 has no bridge
# and from the splash itself:
#   503 {"ok": false, "error": "pi5 not reachable: ..."}
#   502 {"ok": false, "error": "non-JSON reply from pi5 (HTTP 200)"}  a 2xx that is not a JSON object
# (a non-JSON error page, such as Flask's HTML 404 or 500, keeps its status
#  with that same JSON shape; pi5's own route always answers JSON)
```

Commands (pi5 validates; cup numbers are 1-based, as everywhere on DevPi):

| `cmd` | Meaning |
| --- | --- |
| `state N` | Race state 0..6 (0 PRE_RACE, 1 BETTING_OPEN, 2 FINAL_CALL, 3 AT_THE_POST, 4 RUNNING, 5 WINNER, 6 AFTER_PARTY) |
| `horse C H` | Cup C (1..20) carries horse H (1..20; 0 = no horse) |
| `scratch C 1` / `scratch C 0` | Scratch / unscratch the horse on cup C |
| `roster` | Answer with pi5's roster (20 MACs or null, `roster_rev`, `has_roster`); nothing is written |
| `demo`, `json ...` | Rejected by pi5 (400): its bridge never forwards them to the gateway |

The first word must be one of `state horse scratch demo roster json`, one
line, at most 200 characters, case-sensitive; anything else is a 400 from
pi5 (`"command not allowed: ..."`, `"empty command"`, ...).

Stream headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`,
`X-Accel-Buffering: no`, `Connection: keep-alive` (Flask's dev server, which
the systemd unit runs, rewrites that last one to `close`; the stream still
runs until one side hangs up). Event names: the model arrives as unnamed
`data:` events (`onmessage`), the keep-alive as `ping`. The splash keeps
pinging the TV while pi5 is unreachable; only `link_ok` changes.

### Config

| Key | Default | Meaning |
| --- | --- | --- |
| `FLASK_PORT` | `5001` | This app's port (pi5's dashboard owns 5000 on DevPi) |
| `PI5_URL` | `"http://joeydevpi.local:5000"` | pi5's dashboard: the betting model (`/api/quiniela*`) and the race roster (`/api/race`) both come from it |

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
`board_states` (the model's copy of pi5's `config.QUINIELA_BOARD_STATES`,
in `pi5/config.py`; the page never hard-codes it) contains `race_state`:

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

### Dev aid: fake pi5

`tools/fake_pi5.py` serves a synthetic pi5 (the three `/api/quiniela`
routes, SSE with pings) on `127.0.0.1:5078` and runs the **real** splash
server (`server.py`, every route and template, the real `Pi5Link`) against
it on `127.0.0.1:5077`. No serial port, the dashboard poller stays off, so
it is safe on any machine:

```bash
cd splash_display
python tools/fake_pi5.py --phase open              # BETTING OPEN, 20 cups, leader, a scratch, an offline cup, recent events
python tools/fake_pi5.py --phase final             # FINAL CALL
python tools/fake_pi5.py --phase closed            # AT_THE_POST: BETTING CLOSED, board frozen
python tools/fake_pi5.py --phase running           # RUNNING: BETTING CLOSED, still frozen
python tools/fake_pi5.py --phase winner            # state 5: the playlist is back
python tools/fake_pi5.py --phase idle              # state 0, link up: plain slideshow
python tools/fake_pi5.py --phase cycle             # idle -> open -> final -> closed -> running -> winner, ~15 s each, forever
python tools/fake_pi5.py --phase open --stop-feed-after 3   # board up, then pi5 gone: NO LINK mark (0 would stop it before the splash's first request)
# --port 5077 (the splash), --pi5-port 5078 (the fake), --period 15 (cycle),
# --host 127.0.0.1, --no-splash (the fake alone; point a splash at it)
```

Open the printed URL (`http://127.0.0.1:5077/display`) in a browser. The
fake's `POST /api/quiniela/cmd` answers `{"ok": true, "echo": "<cmd>"}` and
`state N` switches its phase, so a curl through the real relay drives the
takeover:

```bash
curl -s -X POST localhost:5077/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'   # board up
curl -s -X POST localhost:5077/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 5"}'   # board down
```

For automated 1080p frames drive headless Chrome over the DevTools protocol
(`--remote-debugging-port`, `Page.navigate`, wait a few seconds of real
time, `Page.captureScreenshot`): the plain `--screenshot` flags do not
wait for the stream. `--virtual-time-budget` never expires because the
open SSE request keeps headless virtual time paused (Chrome hangs), and
`--timeout` fires before the board's first fetch has rendered.

### First end-to-end test

On DevPi with the gateway plugged in and both apps running (pi5's
dashboard on 5000, this service on 5001):

```bash
# pi5 owns the port and hears the gateway
curl -s localhost:5000/api/lq/snapshot | python3 -c "import json,sys; s=json.load(sys.stdin); print(s['link']['port_open'], s['link']['gateway_online'])"
# pi5's betting model: True BETTING_OPEN (or whatever state the gateway is in)
curl -s localhost:5000/api/quiniela | python3 -c "import json,sys; m=json.load(sys.stdin); print(m['link_ok'], m['race_state_name'])"
# the splash relays it: same two values
curl -s localhost:5001/api/quiniela | python3 -c "import json,sys; m=json.load(sys.stdin); print(m['link_ok'], m['race_state_name'])"
# cup 1 carries horse 7, then betting opens: the TV shows the board
curl -s -X POST localhost:5001/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"horse 1 7"}'
curl -s -X POST localhost:5001/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'
# drop a token into cup 1: horse 7 ticks to 1 on the TV
# WINNER: the board yields to the playlist
curl -s -X POST localhost:5001/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 5"}'
```

Unit tests, no port and no network beyond loopback needed:

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
- No direct access to the cup gateway. pi5 owns its USB port and the
  betting model; this app only reads pi5 over HTTP (`config.PI5_URL`) and
  can share DevPi with it (pi5 on 5000, this app on 5001).
