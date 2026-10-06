# DDM Splash Display

A Flask-driven TV splash slideshow for the **Derby de Mayo** annual party.
Runs on a dedicated Raspberry Pi 4B (Pi OS Bookworm), boots straight into a
Chromium kiosk pointed at `http://localhost:5001/display`, and rotates through
splash pages and trivia cards on a weighted, freshly-shuffled cycle.

This is **Phase 1**: file-based content, no remote upload. Phase 2 will add a
`/upload` endpoint and integration with the Pi 5 dashboard.

Race night: see `RACE_NIGHT.md` at the repo root.

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
│   ├── splash/{countdown,horse_roster,la_subasta,la_quiniela,derby_dash,ddm_brand}.html
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

**No hard reload after a pull.** The page asks for every stylesheet and
script with its file's modification time as a query string
(`/static/js/quiniela_board.js?v=1790000000`, `static_url()` in
`server.py`). Flask serves static files with `Last-Modified` and no
`max-age`, and Chromium then keeps a file for a tenth of its age without
asking; a changed file is a new URL, so a restart of the page (or the
kiosk) always loads the new code. pi5's dashboard does the same.

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
here. The two race slides read the same model (see [The race
slides](#the-race-slides)): without pi5 they are simply left out of the
playlist.

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
  "pot": 150.0, "total_tokens": 154,
  "horses": {
    "1": {"tokens": 0, "share": 0, "in_field": true, "scratched": false, "online": false, "cup": null,
          "conflict": false, "cups": [], "name": "", "replaced": null, "odds": "8-1"},
    "7": {"tokens": 23, "share": 0.1494, "in_field": true, "scratched": false, "online": true,
          "cup": "A0:B7:65:12:34:56", "conflict": false, "cups": ["A0:B7:65:12:34:56"],
          "name": "DANON BOURBON", "replaced": null, "odds": "4-1"},
    "9": {"tokens": 0, "share": 0, "in_field": false, "scratched": false, "online": false, "cup": null,
          "conflict": false, "cups": [], "name": "THE PUMA", "replaced": null, "odds": null},
    "20": {"tokens": 4, "share": 0.026, "in_field": false, "scratched": true, "online": true,
           "cup": "A0:B7:65:12:34:69", "conflict": false, "cups": ["A0:B7:65:12:34:69"],
           "name": "FULLEFFORT", "replaced": null, "odds": null},
    "22": {"tokens": 7, "share": 0.0455, "in_field": true, "scratched": false, "online": true,
           "cup": "A0:B7:65:12:34:5E", "conflict": false, "cups": ["A0:B7:65:12:34:5E"],
           "name": "OCELLI", "replaced": "THE PUMA", "odds": "20-1"}
  },
  "leader": 7,
  "events": [ {"horse": 7, "delta": 1, "ts": 1695400000.0} ],
  "updated": 1695400000.0,
  "board_states": [1, 2, 3, 4, 5],
  "now": 1695400003.2,
  "closes_at": 1695400900.0,
  "prizes": {"win": 89, "place": 38, "show": 23},
  "split": {"win": 0.60, "place": 0.25, "show": 0.15},
  "chyron": ["TOTALS BASED ON CHEAP CHINESE ELECTRONICS - FINAL RESULTS HAND COUNTED",
             "NOT AFFILIATED WITH CHURCHILL DOWNS OR ANYONE WITH LAWYERS"],
  "names_rev": 3,
  "scratches": [ {"was": {"number": 9,  "name": "THE PUMA"},   "now": {"number": 22, "name": "OCELLI"}},
                 {"was": {"number": 20, "name": "FULLEFFORT"}, "now": null} ],
  "cups_online": 20, "cups_no_horse": 0,
  "results": null,
  "closing": null,
  "pot_scale": null, "pot_counted": null, "hand_counted": false,
  "race": {"name": "KENTUCKY DERBY", "year": 2026, "post_at": 1777762620.0,
           "post_local": "5:57 PM CDT", "tz": "America/Chicago"},
  "weather": {"location": "Dallas", "temp_f": 88, "condition": "Sunny"}
}
```

- `horses` has every horse `"1"`..`"24"`: 1-20 the field, 21-24 the
  also-eligibles, whose names can be entered ahead of time but who are not
  in the field until one replaces a scratched horse. `in_field` says who
  is: 1-20 unless scratched (either kind), 21-24 only while standing in
  for a scratched horse. A horse no cup claims looks like the `"1"` entry
  above. `cup` is the **MAC** of the cup claiming that horse, a string, or
  `null` when none does; since protocol v2 (`59e3b14`) it is never a
  number, because a cup owns its horse number and pi5 knows cups by MAC
  only. The board only tests it for `null`. `cups` lists every cup
  claiming the horse and `conflict` is true when there are two (the admin
  page's `⚠ 2 CUPS`; the board ignores both). `name` is served upper-cased
  (`""` when unset; the board then shows `HORSE n`); `replaced` is the
  upper-cased name of the horse this one stands in for, `""` when that
  horse had no name yet (the chyron prints `HORSE n`), or `null`.
- Two kinds of scratch. A replacement is a **renumber**: as at Churchill
  the also-eligible keeps its own program number (in the 2026 Derby The
  Puma #9 scratched and Ocelli ran as #22, not as #9), so the cup that was
  9 becomes 22, tokens and all (it is the same cup; nothing moves on the
  mantle), 9 leaves the field (`in_field` false, `cup` null) and 22 joins
  it with `replaced` `"THE PUMA"`. The pot does not move. Without a
  replacement, `scratched` is true, the horse is out of the field and
  **its tokens are out of `pot` and `prizes`** (they are handed back to
  be re-bet); `total_tokens` stays the sum of every cup. Neither kind produces
  an event. `scratches` is one record per scratch, ordered by
  `was.number`: `{"was": {"number", "name"}, "now": {"number", "name"}}`
  for a replacement, `"now": null` for a gateway scratch, names
  upper-cased (`""` when unnamed).
- `pot` = the tokens in play x `token_value`. `prizes` are whole dollars
  that always sum to the pot: place and show are the pot times their
  split, rounded half up (Decimal `ROUND_HALF_UP`, never Python's
  banker's `round()`: 38.5 is $39), win takes the rest. 154 gives
  92 / 39 / 23; 1 gives 1 / 0 / 0. `split` is pi5's `LQ_SPLIT_*` config,
  `chyron` its `LQ_CHYRON_LINES`. Once the host has hand counted the
  cash box, `pot` and `prizes` are the count's (`hand_counted`, below),
  worked out by pi5 with the same split and rounding: nothing here sums
  or splits anything.
- `now` is the server's clock when the JSON was built (never stored, never
  part of change detection, so the stream stays quiet between real
  changes). pi5 stamps it, and this relay stamps its own on every serve and
  publish rather than re-serving pi5's, so a page that loads on a quiet
  board gets a fresh clock, not one as old as the last bet. `closes_at` is
  when betting closes on that same clock, or `null`. The board counts down
  from their difference. `names_rev` is the names
  store's revision: it bumps on any name change, replacement scratch or
  its undo and is persisted; a gateway (no-replacement) scratch is pi5's
  bridge state, versioned separately, and leaves it alone.
- `share` = tokens / total_tokens (0 when the pot is empty). `leader` is the
  horse with most tokens (lowest number on a tie), `null` when nobody has
  bet. Both stay in the model; nothing on the board depends on them.
  `online` = pi5 heard the cup within the last 6 s.
- `events` are the last 8 bets, newest first, each `{"horse", "delta",
  "ts"}`.
- `results` is `{"win": 19, "place": 1, "show": 22}` (horse numbers) once
  the dashboard's SET WINNERS has been confirmed, `null` until then and
  again after a reset. In WINNER it is what turns the frozen board into
  the results screen (below). `cups_online` and `cups_no_horse` are the
  admin page's; the board does not read them.
- `closing` is the figures at the post: `{"pot", "prizes",
  "total_tokens", "horses": {"1": {"tokens"}, ... "24": ...}, "at"}`, the
  live fields' shapes as they were when betting closed, or `null`. pi5
  takes them the first time the race state is 3 (or 4 or 5 when 3 was
  skipped), keeps them through the race, the draw and a restart of pi5,
  and drops them on Reset betting and in 0 or 1. The board shows them in
  3, 4 and 5 (below); the live tokens keep following the cups underneath,
  and so do `pot` and `prizes` until a hand count is entered.
- `hand_counted`, `pot_counted` and `pot_scale` are pi5's counted pot.
  The scales are estimates, so after betting closes the host counts the
  cash box and enters the dollars on the admin page (pi5's
  `PUT /api/quiniela/counted_pot`): `pot_counted` is that count in whole
  dollars (or `null`), `pot_scale` the scale pot frozen at the post (or
  `null` while there are no figures), and `hand_counted` is true while the
  model's `pot` and `prizes` are the count's, which is from the post to
  the end (states 3 to 6; in FINAL CALL the count is held but betting is
  open again, so it is false). The count is stored inside `closing`'s
  record on pi5 and goes with it, but is not one of its keys: `closing`
  keeps its five, and its `pot` and `prizes` are the count's. The board
  reads `hand_counted` alone, for the tag (below); bets per horse are
  the scales' either way.
- `race` is La Quiniela's race info, the only copy (pi5's Race Setup is
  gone): `name` upper-cased (`KENTUCKY DERBY` until one is saved),
  `year`, `post_at` (unix seconds, or `null` while no post time is set),
  `post_local` (the post time on the race's clock, `"5:57 PM CDT"`, or
  `null`) and `tz` (that clock, pi5's `LQ_RACE_TZ`). The crawl's clock and
  time to post and the two race slides read it. `weather` is pi5's weather
  feed, `{"location", "temp_f", "condition"}` (any part `null`), or
  `null` when there is none: the crawl leaves it out. `horses[n].odds` is
  the track's line for that program number, a short string (`"5-2"`), or
  `null`; only the roster slide reads it.
- `board_states` are the race states in which the board owns the TV; pi5
  decides them (`[1, 2, 3, 4, 5]`: betting, the race and the results; the
  TV goes back to the playlist in 0 and 6). Until pi5 has been heard the
  splash serves an empty model: `link_ok` false, race state 0,
  `token_value` 1.0, every horse unassigned, `board_states` `[]` (so the
  board stays hidden), and none of the additive keys (`now`, `closes_at`,
  `prizes`, `split`, `chyron`, `names_rev`, `scratches`, `results`,
  `closing`, `pot_scale`, `pot_counted`, `hand_counted`, `cups_online`,
  `cups_no_horse`, `race`, `weather`, `name`, `replaced`, `odds`,
  `in_field`, `conflict`, `cups`); the board and the
  race slides tolerate their absence.

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

Commands (pi5 validates). Since protocol v2 only one does anything:

| `cmd` | Meaning |
| --- | --- |
| `state N` | Race state 0..6 (0 PRE_RACE, 1 BETTING_OPEN, 2 FINAL_CALL, 3 AT_THE_POST, 4 RUNNING, 5 WINNER, 6 AFTER_PARTY): what a dashboard mode button or the admin page's state buttons set |
| `demo`, `json ...` | Rejected by pi5 (400): its bridge never forwards them to the gateway |

The first word must be one of `state demo json`, one line, at most 200
characters, case-sensitive; anything else is a 400 from pi5 (`"command not
allowed: ..."`, `"empty command"`, ...). The v1 `horse`, `scratch` and
`roster` commands are gone: a cup's horse number is set on the cup itself,
scratches go through pi5's admin page (`POST /api/quiniela/scratch`), and
there is no roster. The results come from the dashboard (`POST
/api/results` on pi5), not from here.

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
| `PI5_URL` | `"http://joeydevpi.local:5000"` | pi5's dashboard: the betting model (`/api/quiniela*`), and in it everything the race slides and the crawl show (the race info, the field, the odds, the weather). The splash asks pi5 for nothing else |
| `QUINIELA_LOOK` | `"dots"` | The board's look: `"dots"` (the tote look, what the TV shows), `"impact"` or `"numbers"` (the tote look's figures, names left in Impact). `?look=` on the board's URL overrides it for that page. See "The tote look" below |

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
in `pi5/config.py`, `[1, 2, 3, 4, 5]`; the page never hard-codes it)
contains `race_state`:

- the playlist pauses in place (`window.ddmSlideshow.hold()` clears the
  slide timers, the current slide stays where it is) and the board
  crossfades in full-screen, over the same `TRANSITION_FADE_MS`;
- when the state leaves the set the board crossfades out and
  `release()` restarts the same slide's timers from zero — same slide,
  fresh dwell. The spacebar pause still wins: a paused show stays paused.
- a dead stream never hides the board. It stays up with its last data;
  only a model saying the state is outside `board_states` takes it down.

**Banner** (top right), by `race_state`:

| State | Banner | Rows, pot, prizes |
| --- | --- | --- |
| 1 `BETTING_OPEN` | BETTING OPEN (green), CLOSES IN under it | live |
| 2 `FINAL_CALL` | FINAL CALL (scarlet, pulsing), CLOSES IN under it | live |
| 3 `AT_THE_POST`, 4 `RUNNING` | BETTING CLOSED, no CLOSES IN line | **frozen**: the figures at the post, pi5's `closing` (names and the chyron stay live); back in 1 or 2 they go live again |
| 5 `WINNER`, `results` null | OFFICIAL RESULTS COMING (the text shrinks until the sign fits its 480 px cell) | still **frozen**: the betting board with the figures at the post |
| 5 `WINNER`, `results` in | OFFICIAL RESULTS (gold) | the **results screen** (below), from the same figures |

In 0 `PRE_RACE` and 6 `AFTER_PARTY` the board is down and the playlist
runs. (Should pi5's config ever add one of them to `board_states`, the
banner reads the state's name; in 6 with the results still in, the results
screen stays up.)

The figures on a frozen board are pi5's, not the page's: the model's
`closing`, taken when betting closed and kept by pi5 through the draw and
its own restarts. The page paints them when they arrive (and again only if
pi5 takes them again), so the live counts underneath, the winners' cups
being emptied for the draw included, change nothing on the TV, and every
screen shows the same numbers whenever it was loaded: a TV page reloaded
after the draw, a second screen, a phone, a restarted splash or pi5. A
model without `closing` (an older pi5) keeps the page's own freeze, what
it showed when betting closed, which a page loaded later cannot know.

**The hand count's tag.** While `hand_counted` is true the pot wears a small
tag, `HAND COUNTED`, to the left of the POT figure, at its middle: the pot
and the three prizes on the board (and on the results screen, whose header
keeps the pot) are the host's count of the cash box, not the scales'
estimate. It is what makes the crawl's `FINAL RESULTS HAND COUNTED` true.

It is **lettered in Impact in every look**: the stack the board itself
declares (Impact where installed, `static/fonts/Anton-Regular.ttf` where
not), upper case, 26 px with 4 px between the letters, so the capitals are
about 21 px tall (Impact 20.5, Anton 22.4), the height of the dot-matrix
tag it replaced, and the text is 203 px wide (190 in Anton), narrower than
that tag's 216. No rule of the tote look gives it a face or tiles: it is
not a tote field. The looks differ only in its box and colour: in `impact`
a gold pill (a 3 px gold outline, 237 px wide), in `dots` and `numbers`
plain amber letters, the figure's colour, with no pill.

The tag is out of the flow, so the figure, its label, the prize tiles, the
banner and the logo stand exactly where they do without it (measured at
1920 x 1080 in all three looks and at 1680 x 1050 in `dots`: 0 px; the
limit is 2); it follows the figure when its width changes (`$99` to
`$100`), when the window is resized and when the tote face arrives late.
Anton is a web font: where Impact is missing the text is laid out in the
browser's plain sans-serif (257 px wide) until it has arrived, and a width
measured then would pick the wrong text. So the tag stays down until
`document.fonts.load()` has settled for its own computed font, and is
measured again whenever a font finishes loading and whenever the window
is resized. It reads `COUNTED` instead when `HAND COUNTED` would not fit
between the logo and the figure; with the logo's 480 px cell there is room
at every width the header itself fits in (at 1680 px the tag stands 289 px
clear of the logo, crossing the logo cell's right edge by 41 px of empty
space, where the dot tag crossed it by 54), so that is a safety net, and
the tests make it happen by widening the logo. It is up only while the pot
shown is the count: never in betting (1, 2), where the pot is live whatever
a held count says, and never on a model without `hand_counted` (an older
pi5).

Either freeze only holds while the board is up. A board coming up already
in 3, 4 or 5 — a page loaded mid-race, or the server restarted during the
race — paints before it shows, so it never shows an earlier hidden paint of
an empty model (POT $0, every count 0).

**Results screen** (state 5 once `results` names all three; the stage
between the header and the chyron crossfades from the two columns to it,
500 ms, opacity only, the moment the model carries the results): three
rows, `WIN` / `PLACE` / `SHOW`, each with the place (WIN gold), the
horse's saddle cloth (150 px) and name (100 px, shrunk step by step down
to 44 px until it fits; never wraps), the bets that cup held (`4 BETS`)
and its prize at the right, 150 px (`$92`, WIN's in gold; it shrinks too
should a prize ever need four figures). The WIN row has the leader's gold
outline. The pot stays in the header above (its three prize tiles step
aside, each prize being in its row); one line under the rows reads `ONE
TOKEN DRAWN FROM EACH CUP · DRAWN TOKEN TAKES THE PRIZE`; the chyron keeps
crawling. Who won and the names are live (a late name correction shows);
the bets and the prizes are the figures at the post (`closing`). A winner that was never in
the field still gets its row (cloth, name, 0 bets): the board shows what
the results say. No toast while it is up.

**How La Quiniela pays, and so what the board shows.** A token is $1.
After the race one token is drawn from the WIN cup, one from the PLACE
cup and one from the SHOW cup, and each drawn token's owner takes that
cup's whole prize, a fixed fraction of the pot (60 / 25 / 15 % by
default). Nobody splits anything, so a token is a raffle ticket for a
fixed prize and the only number that matters per horse is how many tokens
are in its cup. No odds, no share bars: the board shows the pot, the three
prizes and the bets per horse, nothing else.

**Layout** (1920x1080, built for a TV across the room; 14 px serape band
top and bottom; the design is `tools/board_reference.html`, a static mock
the live board follows pixel for pixel where practical):

- Header (196 px, three cells 480 / flexible / 480): `la_quiniela.png`
  left, cropped to a 150 px medallion. Centre: `POT` with `$1 A TOKEN`
  muted beside it (from `token_value`: `$1` while it is a whole number,
  else `$1.50`), the pot at 104 px (whole dollars while the token is,
  else two decimals), and under it three prize tiles `WIN $92` (gold
  outline) `PLACE $39` `SHOW $23` from `prizes`. Right: the state banner
  and under it `CLOSES IN 14:22`, counted down locally from `closes_at`
  against the newest `now` the model carried (an older stamp never moves
  it back), so the TV's clock never matters; at zero it reads
  `CLOSING 0:00`; hidden when `closes_at` is null and in states 3 and
  above. Every message carries a fresh `now` (pi5 and the relay both stamp
  it at serve time), so a page that loads on a quiet board starts right.
- Rows: the field, in two columns of ten slots (22 px gutter, 6 px
  between rows, `HORSE` / `BETS` column headers), filling the height.
  The rows are every horse with `in_field` true, in numeric order, the
  first ten down the left column and the rest down the right. Fewer than
  twenty in the field leaves the trailing slots empty: no placeholder, no
  border, nothing (a field where 9 -> 22 reads 1-8, 10, 11 on the left and
  12-20, 22 on the right; a gateway scratch of 20 with nobody drawn in
  leaves the last slot blank). A scratched horse, either kind, never has a
  row: it shows in the chyron. The row set is re-rendered whenever the
  field changes, with no motion of its own (the horse that drew in appears
  with its cup's count, the scratched one is gone); counts, leader, pulse,
  name fit and the toast work on whatever row a horse occupies. Without
  `in_field` (the relay's empty model, an older pi5) the field is horses
  1-20 that are not scratched. Each row: the program number in its
  saddle-cloth coloured block (56 px; cloths 1-20 are the Derby's, 21-24
  placeholders for the also-eligibles, peach / teal / olive / slate, the
  same as the cup firmware shows); the name at 38 px, shrunk step by step
  down to 20 px until it fits its cell (never wraps, never an ellipsis;
  `HORSE 7` while no name is set); the bets at 58 px, right-aligned,
  tabular, or `NO BETS` at 22 px muted when the cup is empty. Leader
  (most tokens among the horses in the field, when anyone has bet): gold
  border and a soft glow. Cup offline (`cup` assigned, `online` false): a
  small dim dot in the row's corner.
- Toast: a new positive bet (`events` newest first, keyed `horse:ts`
  against the previous message; the newest new positive one) pops a card
  in the centre of the screen in that horse's saddle-cloth colours (cloth
  as background, the number block inverted, white border, drop shadow):
  number, name, `+1 BET` / `+N BETS`, by the horse's current number (after
  9 -> 22 a bet on that cup is `22 OCELLI` in 22's cloth); a long name
  shrinks (64 px down to 36 px) and the card never grows past the screen.
  Pop in ~150 ms, hold 2.5 s once up, drop out ~200 ms; a newer bet
  replaces it and restarts the hold. No toast on the first model after
  load, on a poll/stream duplicate, on a negative event, on a horse
  scratched at the gateway (it is out of the field and the pot does not
  move, so a token dropped in that cup is not a bet), on a renumber (pi5
  produces no event for one), while the board is hidden, or while the
  picture is frozen.
- Chyron: a 50 px band along the bottom. In `impact` and `numbers` a
  continuous right-to-left crawl edge to edge at ~120 px/s (one CSS
  transform animation over a track whose content is repeated; the
  duration is computed from the track's width after layout); `dots` is a
  dot-matrix sign of fixed tiles (the next section). Content, in order:
  `chyron[0]`; the live
  items: the time of day on the race's clock (`7:42 PM`), the time to post
  (`POST IN 1:14`, hours and minutes; under ten minutes `POST IN 9:42`,
  minutes and seconds; gone once the post time has passed or while there
  is none) and pi5's weather (`DALLAS 88°F SUNNY`, left out when pi5 has
  none); then, when
  `scratches` is not empty, one `SCRATCHED` item with every scratch, a gap
  between them: a replacement reads `[9] THE PUMA ▶ [22] OCELLI` (both
  badges in their cloth colours, the old name struck through, the gold
  arrow), a scratch with no replacement reads `[20] FULLEFFORT · RE-BET YOUR
  TOKENS` (badge, name, the note as bright as the name: it tells the
  bettors what to do); an unnamed horse prints
  `HORSE n`, and an entry that is not a record (the older string shape) is
  ignored; then the remaining `chyron` lines, gold diamonds between items.
  It is rebuilt when `chyron`, `scratches` or `names_rev` change, swapping
  the content at the loop boundary so the text never jumps (at once if
  nothing is crawling yet). The live items change **in place**, checked
  every second: a text is replaced by one of the same length (in the tote
  look every character is a tile, so the track keeps its width) and the
  crawl is not restarted. An item coming or going, or a text of another
  length (`9:59 PM` to `10:00 PM`, new weather), is a rebuild like any
  other, at the loop boundary.

**The crawl of `dots`** is a dot-matrix sign, not a track: the tiles
stand still and the message steps across them, a character a tile.

- *A fixed row of tiles across the band*, built once by the rows' fill
  rule at the crawl's own 4 px pitch: as many 24 px tiles as the band
  holds, or one more when each is still 94 % of 24 px, every tile the
  band's width / N and 32 px tall (78 tiles of 23.74 px in the 1852 px
  band of a 1920 px screen, 68 of 23.71 px at 1680 px), the bulbs at the
  pitch and a character's dots on them. The tile elements never move: no
  transform, no animation and no transition touches the row or a tile
  (sampled every frame for five seconds, every tile's x differs from its
  first by 0 px). They are added or taken away only when the window
  changes size, and the message goes on through it.
- *The message is a loop of cells, one character a tile.* Tile `i`
  shows cell `offset + i`; each **step** `offset` goes up by one, so the
  whole message is one tile to the left, the next character comes in on
  the right and the left tile's is gone. Stepped, not smooth: one step is
  one tile and there is nothing in between. The loop is the track's items
  in the same order, each followed by the gap (one blank tile, `◆`, one
  blank tile: the track's 34 px each side, in tiles, `CRAWL_PAD_TILES`),
  the last item too, so the end of the message and its start again are
  one gap apart, and a message shorter than the band shows itself more
  than once. A message that starts (the board comes up, or a rebuild that
  did not have to wait) starts from blank tiles and comes in on the
  right.
- *The rate* is `CRAWL_TILES_PER_SEC`, a named constant at the top of
  the script: 5 tiles a second, 119 px/s at 1920 px, the speed of the
  track the sign replaced (120; 8 tiles a second, tried first on DevPi,
  was far too fast). `?crawl_tps=` on the URL overrides it for that page,
  for tuning on the TV (`/?crawl_tps=4` slower, `/?crawl_tps=8` the first
  speed; from 0.25 to 60, and anything that is not a number is the
  default); `window.ddmQuiniela.crawl()`, a read-only
  snapshot of the sign (`tps`, `tiles`, `tile`, `offset`, `length`,
  `generation`, `pending`, `items`, `text`; null in the other looks), says
  what is in force. The steps come from `requestAnimationFrame`
  timestamps, not a timer: step n is due n / rate after the crawl started,
  so a late frame makes the next one take two steps and the rate does not
  drift (5.00 steps a second over 5.8 s of headless Chrome, one step every
  200 ms to the frame; 2.00 at `crawl_tps=2`, 8.01 at 8); a stall of more
  than half a second is not made up for. A step
  writes only the tiles whose character or look changed, and nothing is
  rebuilt.
- *The content* is the track's: the same items in the same order with
  the same separators, in capitals, a character a tile. The face's own
  characters stay as they are (it draws a lower-case letter, an accented
  one, a curly quote or a dash as the plain capital or mark, `SEÑOR` as
  `SENOR`). **A character the face lacks** is its base letter when it
  has one in the face (`Ž` is `Z`, `Ā` is `A`), else a blank tile;
  a run of blanks is one blank, a zero-width character or a combining
  mark is nothing, and none falls back to Impact (`toteChar`, from the
  script's own list of the face's characters, which a test holds equal to
  the face's character map). The default content is all in the face: the
  middle dot, the diamond, the arrow and the degree sign included. The
  badges are the one thing that is not dots: a saddle cloth is its
  number's digits on solid cloth tiles in Impact, one tile for `9`, two
  touching for `22`; `SCRATCHED` is red dots, a struck name dim with a
  line across its tiles, `RE-BET YOUR TOKENS` lit like the name before it.
- *Live updates* keep the track's rule (`f1a86e9`). A refresh that
  keeps an item's length (the clock's minute, the countdown's minute or
  second) is written into the message where it stands, at once, on the
  tiles that show it, and the crawl goes on. Anything that changes the
  message (a scratch added or undone, the weather text, an item coming
  or going, a text of another length) waits for the loop boundary, the
  message's start reaching the left edge, where the new message takes over
  from its first cell; while it waits the live items are kept up to date
  in it. Nothing an update does starts the crawl again from the
  beginning (against the fake, a scratch added mid-loop at 30 tiles a
  second showed `pending` for the rest of the loop and the offsets ran
  311, 312, 0, 1 with the new message from 0). The crawl is paused while
  the board is hidden (no animation frame is asked for) and goes on where
  it was.
- `numbers` and `impact` keep the track as it was: compared pixel for
  pixel with the build before, whole board, animations frozen at the same
  time, both are identical and `dots` is identical everywhere but the
  band. `?crawl_tps=` does nothing there.

**Motion.** A changed count ticks to the new value over ~500 ms (a
`requestAnimationFrame` tween writing the number) and the row pulses
once, ~400 ms (scale 1.015 plus a white overlay's opacity). The toast and
the crawl (the track of `impact` and `numbers`) are transform / opacity
too, and so is a scrolling name in `dots`. Everything is transform /
opacity only so it stays smooth on a Pi 5 in kiosk Chromium, except
the crawl of `dots`, which is not an animation at all: the script
writes the tiles whose character changed, five times a second (above).
Nothing loops
except the crawl, the FINAL CALL pulse and, in `dots`, a name too long
for its row (the crawl is paused while the board is hidden, the names
too, and under the results screen).

**What the board reads.** Of the model: `race_state`, `board_states`,
`link_ok`, `token_value`, `pot`, `horses[n].tokens / in_field / scratched /
online / cup / name`, `events`, and the additive keys `now`, `closes_at`,
`prizes`, `chyron`, `scratches`, `names_rev`, `results`, `closing`,
`hand_counted` (the tag), `race` (the crawl's clock and time to post) and
`weather`; the race
slides read `race` and `horses[n].odds` from the same script. Every one of
the additive keys is optional: before pi5 has been heard the splash's
empty model carries none of them and the board renders without errors
(hidden, since `board_states` is empty; prizes read `$0`, no countdown,
an empty chyron). `share`, `leader` and `replaced` stay in the model;
nothing on the board depends on them (the leader is computed from the
counts of the horses in the field).

**The tote look.** The board has two looks. `impact` is the board
described above. `dots` is the tote look: the dashboard's amber dots on
black tiles, on the tote's fields and nothing else, and it is what the TV
shows.

```
http://joeydevpi.local:5001/                the tote look (the default)
http://joeydevpi.local:5001/?look=impact    Impact, the board as it was before the tote look
http://joeydevpi.local:5001/?look=numbers   the tote look's figures, the names left in Impact
http://joeydevpi.local:5001/?crawl_tps=8    the tote look with its crawl at 8 tiles a second (5 is the default)
```

`config.QUINIELA_LOOK` is the default (`"dots"` as shipped, and `dots`
when the key is missing); `?look=` on the URL wins, for that page, on `/`
and on `/display` alike (the redirect keeps the query). A value that names
no look is the default, and a `QUINIELA_LOOK` that names none is `dots`,
logged once. The page carries the look in the board's `data-look`.

*A row in `dots`* is one strip of tiles that fills the row from just right of the cloth to its right padding, the same 22 px as the gap after the cloth:
- every row the same: the pitch from the row's height (7, a 56 px tile in the 64 px row at 1080 lines); N from its width, as many 6-pitch tiles as fit, or one more when each is still 94 % of 6 pitches, every tile then the room / N (20 of 40.25 px at 1920 px, 17 of 40.29 px at 1680 px), its bulbs and a character's dots at the pitch, centred;
- the bets in the strip's last tiles, one a digit, a dim `0` for an empty cup; the name left in the rest but one dark tile (`tiles - digits - 1`);
- a name longer than that scrolls in its area only: its start held 2 s, a tile at a time at 120 px/s (`CRAWL_PX_S`, the speed of the track of `impact` and `numbers`) until its last character is in the area's last tile, held 1 s, back to the start;
- whole tiles, never a slide, so a character always sits on the tiles' bulbs, like the dashboard's ticker; a count reaching 10 takes a tile from the name and the scroll is measured again, as are the strips when the window is resized.

- **Dotted** in `dots`: the pot, the three prizes,
  every row's name and bets, the crawl (a fixed row of tiles with the message stepping across it:
  its text, its diamonds, its arrows; `SCRATCHED` in red
  dots, a struck name dimmed), and the results
  screen's names, bets and prizes. In `numbers` the same without the names.
- **Not dotted**, in any look: the saddle cloths, solid blocks with their
  number, as on the dashboard's results tote (cloth, then the dotted
  name), the crawl's badges included; the state banner and the toast,
  which are signs, not tote fields; the `HAND COUNTED` tag, a label in
  Impact in every look; every label (`POT`, `WIN`, `HORSE`,
  `BETS`, the results' `WIN / PLACE / SHOW`, the line under them) and the
  `CLOSES IN` clock.
- **One layout.** Every box of the Impact look is where it was: header,
  columns, rows, cloths, chyron, the results rows. Measured on both
  screens, 79 boxes each: all within half a pixel of their Impact place
  but the header's three prize tiles and their labels, which hug their
  text and so are wider by the width of the dotted figures. Inside the
  rows `dots` has its own strip (above); `numbers` keeps the Impact
  look's name cell and bets column.

*How it is drawn.* One element per field, exactly as in the Impact look:
the text is set in a face whose glyphs are the dots. **DDM Tote**
(`static/fonts/DDMTote.ttf`, 37 KB) is built by `tools/make_tote_font.py`
from the dashboard's own 5x7 table (`dotPatterns` in
`pi5/static/js/ddm_control.js`), which is read, never copied: no glyph
bitmap lives in the splash, and the TV's dots are the dashboard's dots. No
element per dot and nothing for the JS to draw. Behind the text the
stylesheet lays one tile a character, the black tile and its 35 unlit
bulbs (an SVG background, 0.75em x 1em), and the glow is a text shadow.
The one place with an element per tile is the crawl of `dots`: 78 of them
across a 1920 px board, built once, each holding one character of the
face, which is what lets the tiles stand still while the message moves.

```bash
cd splash_display
python tools/make_tote_font.py            # after a pattern changes in the dashboard's table: rebuild, commit the face
python tools/make_tote_font.py --check    # is the face on disk what the table says? (the tests ask the same)
python tools/make_tote_font.py --list     # the characters it covers
```

The face covers A-Z, 0-9 and the punctuation in the table, the degree
sign included (the crawl's `88°F`); lower case and
accented letters are drawn with the plain capital (a 5x7 matrix has no
room for an accent: `SEÑOR` prints `SENOR`), curly quotes and dashes with
the straight ones. A character it lacks falls back to Impact (not in the
crawl of `dots`: there it is its base letter or a blank tile, above). The file is
written by hand, table by table (no font library is needed to build it),
and is the same bytes on every run. The root `.gitignore` ignores `*.ttf`
and excepts this one. The page's `?v=` stamping (`static_url()`) does not
reach the face, which `quiniela_board.css` loads by its own `url()`: a
rebuilt face bumps the `?v=` on that `url()` (`?v=2` since the degree
sign), or a TV that has the old one keeps it.

*Pitch, not font size.* The face's cell is 6 dot pitches wide and 8 tall
and its em is 8 pitches, so `font-size = 8 x pitch`. Sizes are whole
pitches, which keeps every dot on the pixel grid:

| Field | Impact | Tote look |
| --- | --- | --- |
| Pot | 104 px | pitch 12 (96 px) |
| `HAND COUNTED` tag | 26 px, gold-outlined pill | 26 px, plain amber: still Impact, not a tote field, so no pitch |
| Header prizes | 40 px | pitch 5 |
| Row (`dots`) | name 38 px, down to 20; bets 58 px; `NO BETS` 22 px | one strip, pitch from the row's height (7 at 1080 lines), tiles filling the row (20 of 40.25 px at 1920 px); a long name scrolls; a dim `0` |
| Row bets (`numbers`) | 58 px; `NO BETS` 22 px | pitch 7 on three tiles; `NO BETS` pitch 3, dim |
| Crawl | 26 px | pitch 4 (`numbers`: the dotted text on the track; `dots`: a row of 24 x 32 px tiles, 78 across 1920 px) |
| Results name | 100 px, down to 44 | pitch 12, down to 4 |
| Results bets | 88 px | pitch 10, down to 5 |
| Results prize | 150 px, down to 80 | pitch 18, down to 8 |

On the results screen a name that does not fit steps its pitch down a
pixel at a time, the way the Impact look steps the font size; it never
wraps and never gets an ellipsis, and the strip behind it is always a
whole number of tiles, the ones past the name unlit. A row's name in
`dots` never shrinks: it scrolls.

Which names scroll depends on the screen: at 1920 px a row has 20 tiles,
so a name scrolls past 18, 17 or 16 characters (one, two or three digits
of bets): `GRAND MO THE FIRST` (18) once it has 10 bets, and
`CATCHING FREEDOM` (16) fits even at 104. A narrower screen has fewer
tiles at the same pitch: at 1680 px, 17, where `EMERGING MARKET` (15)
fits with one digit of bets and scrolls with two (in 3d5e844's 16 tiles
it scrolled with one). `tools/fake_pi5.py --phase strip` scrolls three
rows on a 1920 px board, a fourth from 10 bets, and eleven at 1680 px.

With the crawl running and a bet every 4 s (`--phase strip`, headless
Chrome at 1920x1080, 12 s, CPU 6x slower, measured as the table below):
worst frame 11.2, 16.7 and 22.3 ms in three runs, none over 34 ms, no
long task, 16.8 ms with software raster (3d5e844's rows, same feed: 16.8
and 16.7 ms); with ten rows scrolling (seven of the feed's names made
longer with its `name` command) 11.2, 16.7 and 16.7 ms, 16.8 with
software raster. Each scroll is one Web Animation on the name's
transform, run by the compositor (the trace shows no compositing
failure); they pause while the board is hidden or the results screen
covers the rows.

The crawl of `dots` is the main thread's, not the compositor's: a row of
tiles written by the script five times a second where the track was
moved by the compositor. The same feed, 12 s, headless Chrome at
1920x1080, the CPU throttled over the DevTools protocol: worst frame
5.7 ms unthrottled, 11.0 ms at 4x slower, 16.7 ms at 6x, 16.8 ms at 6x
with software raster (60 Hz), none over 34 ms and no page error. A Shop
PC's Chrome, not a Pi's (below).

*Why not DOM dots.* The dashboard and the countdown slide's digits draw
their dots as elements, 35 to a character, from two separate copies of the
glyph table (the roster slide had a third until it became the board's own
rows, below). At the board's size that is 813 tiles,
29,268 elements more (255 elements become 29,641). Measured in headless
Chrome at 1920x1080 over 12 s of the `redesign` feed (a bet every 4 s: the
count ticks, pot and prizes follow, the row pulses, the toast pops; the
crawl running), main thread throttled, worst frame and frames over 34 ms:

| | Impact | Tote look (the face) | DOM dots |
| --- | --- | --- | --- |
| elements in the board | 255 | 255 | 29,641 |
| GPU raster, no throttle | 27.7 ms, 0 | 5.7 ms, 0 | 83.4 ms, 6 |
| GPU raster, CPU 4x slower | 5.7 ms, 0 | 11.1 ms, 0 | 83.3 ms, 7 |
| GPU raster, CPU 6x slower | 11.2 ms, 0 | 11.2 ms, 0 | 77.7 ms, 9 |
| software raster, CPU 6x slower (60 Hz) | 16.8 ms, 0 | 16.8 ms, 0 | 33.4 ms, 0 |
| raster work in those 12 s, software | 33 ms | 48 ms | 320 ms |
| crawl, px in one second | 119.6 | 120.6 | 120.2 |

The face costs what Impact costs; DOM dots stall the compositor for some
80 ms at every bet on a desktop GPU, and need ten times the raster work.
So the face draws every tote field, the header included: DOM dots in the
header alone would have been cheap enough, but two renderers on one board
make two kinds of dot. Two frames of the tote look's crawl (as it was,
and still in `numbers`) taken 1.146 s apart are the same picture moved
138 px, 120.4 px/s; two of the `dots` crawl one step apart are the
message moved one tile with every tile where it was. (Chrome's
dropped-frame marker is no use here: it flags most frames of the untouched
Impact board, because the slideshow's FPS counter asks for a frame on
every refresh and gets none.) None of this is a Pi: the check that counts
is RACE_NIGHT.md section 0, on the TV, `d` for the FPS overlay.

**Type.** Impact everywhere (`font-family: Impact, "Anton", sans-serif`,
everything upper-cased by `text-transform`). Impact is licensed with
Windows and not in the repo; **Anton** (SIL Open Font License 1.1) is
shipped as the fallback in `static/fonts/Anton-Regular.ttf` with its
licence beside it (`OFL-Anton.txt`; the root `.gitignore` ignores `*.ttf`
except this one). To get the real Impact on the Pi: copy
`C:\Windows\Fonts\impact.ttf` to `~/.fonts/` on DevPi and run
`fc-cache -f`; Chromium picks it up on the next launch. Names are
re-fitted when the web font arrives. The tote look's face is the
splash's own and is served with the page; when it arrives every dotted
field is fitted again and the crawl's track is measured again (the tiles
of `dots` do not depend on the face).

**NO LINK mark.** A small dim `NO LINK` mark sits in the top-right corner
of the board (above the banner, never over a row) when no SSE message of
any kind — a model or a `ping` — has arrived for 10 s, or when the latest
model says `link_ok: false` (the gateway itself has gone quiet). It hides
as soon as either condition clears. It is only ever a mark: the board
keeps its last data and never drops back to trivia on a hiccup.

### The race slides

The countdown and the roster are playlist slides
(`templates/splash/countdown.html`, `templates/splash/horse_roster.html`)
filled from La Quiniela's model through the board's script
(`window.ddmQuiniela`: `race()`, `now()`, `fillRoster()`). The race info
(`race`: `name`, `year`, `post_at`, `post_local`, `tz`) is what pi5's
La Quiniela admin page sets, the field and the names are La Quiniela's,
the odds are the track's (pi5's odds poller, `horses[n].odds`). There is
no other copy: the splash's old race poller (`/api/race` every 30 s) and
pi5's Race Setup are gone, and so are `DDM_2026_POST_TIME_ISO`,
`DASHBOARD_RACE_URL` and `RACE_DATA_STALENESS_S`.

- **Countdown.** Days, hours, minutes and seconds to `race.post_at` on
  pi5's clock, the race read again every second, so a post time saved on
  the admin page shows on the next tick. Under the digits
  `{name} {year}` (`KENTUCKY DERBY 2026`) and
  `{DAY} · {MONTH D} · POST TIME {post_local}`
  (`SATURDAY · MAY 2 · POST TIME 5:57 PM CDT`). Once the post time has
  passed the digits give way to **AND THEY'RE OFF** in the tote face
  (15 tiles; pitch 9 at 1920 px, 7 from 1500 px, 5 from 1150 px), the
  two lines kept under it. With no post time the slide is left out of
  the playlist (`/api/slides`, asked again at the end of every lap, so
  a post time set later brings it back on the next lap).
- **Roster.** The board's own rows (`fillRoster`), in the tote look's
  strip whatever the board's look: every horse `in_field` by number with
  its La Quiniela name, a replacement under its own number (21, 22...),
  the first ten down the left, the rest down the right. The track's odds
  sit where the board's bets do, right-aligned, as many tiles as
  characters (`5-1` three, `50-1` four) with one dark tile before them; a
  horse with no odds (`null`) shows a dim `—`. The header reads
  `POST TIME {post_local}`. The strip is measured on the slide, with the
  board's fill rule, so every row of both columns fits the panel (1920x1080:
  pitch 5, 22 tiles of 29.1 px, 19 rows); a name longer than its area
  scrolls a tile at a time, as on the board. Left out of the playlist
  until a horse in the field has a name. This retires the roster's own
  DOM-dot copy of the glyph table and its CSS.

Frame timing with the slideshow paused on the roster (headless Chrome,
1920x1080, 12 s, CPU 6x slower, measured as the table above): worst frame
5.7 ms with GPU raster, 16.8 ms with software raster (60 Hz), none over
25 ms, no long task; the same with a 37-character name scrolling.

### Dev aid: fake pi5

`tools/fake_pi5.py` serves a synthetic pi5 (the three `/api/quiniela`
routes, SSE with pings) on `127.0.0.1:5078` and runs the **real** splash
server (`server.py`, every route and template, the real `Pi5Link`) against
it on `127.0.0.1:5077`. No serial port, the dashboard poller stays off, so
it is safe on any machine:

```bash
cd splash_display
python tools/fake_pi5.py --phase open              # BETTING OPEN, 20 cups, leader, a gateway scratch (13: out of the field, 19 rows), an offline cup, recent events
python tools/fake_pi5.py --phase final             # FINAL CALL
python tools/fake_pi5.py --phase closed            # AT_THE_POST: BETTING CLOSED, board frozen
python tools/fake_pi5.py --phase running           # RUNNING: BETTING CLOSED, still frozen
python tools/fake_pi5.py --phase winner            # WINNER, no results yet: OFFICIAL RESULTS COMING over the board
python tools/fake_pi5.py --phase after             # state 6 (AFTER_PARTY): the playlist is back
python tools/fake_pi5.py --phase idle              # state 0, link up: plain slideshow
python tools/fake_pi5.py --phase cycle             # idle -> open -> final -> closed -> running -> winner -> the results arrive
                                                   # (the results screen) -> after party, ~15 s each, forever
python tools/fake_pi5.py --phase results           # how the 2026 Derby field's race ends: RUNNING -> WINNER with no results
                                                   # (OFFICIAL RESULTS COMING) -> the results arrive (19 Golden Tempo, 1 Renegade,
                                                   # 22 Ocelli: WIN $92 / PLACE $39 / SHOW $23 of POT $154) and 3 s later the three
                                                   # cups are emptied for the draw (the screen keeps 4 / 11 / 7 bets and the prizes:
                                                   # the model's closing, so a page reloaded now shows them too)
                                                   # -> AFTER_PARTY (the playlist) -> again; --period seconds per step
python tools/fake_pi5.py --phase results-static    # WINNER with those results, nothing moving, for screenshots
python tools/fake_pi5.py --phase open --stop-feed-after 3   # board up, then pi5 gone: NO LINK mark (0 would stop it before the splash's first request)
python tools/fake_pi5.py --phase bench                     # the 2026-09-25 bench picture: 50/42/8/3 tokens
python tools/fake_pi5.py --phase bench-reset               # bench, then a reset (counts 0, events cleared, state 0), then state 1 again; repeats
python tools/fake_pi5.py --phase redesign                  # the 2026 Derby field with three also-eligibles drawn in (5 Right to Party -> 21 Great
                                                           # White, 9 The Puma -> 22 Ocelli, 13 Silent Tactic -> 23 Robusta) and 20 Fulleffort scratched
                                                           # at the gateway: 19 rows, POT $150 = WIN $89 / PLACE $38 / SHOW $23, closes in 15 min,
                                                           # a bet on horse 7 every 4 s (the toast fires)
python tools/fake_pi5.py --phase redesign-static           # the same picture with nothing moving, for side-by-side screenshots
python tools/fake_pi5.py --phase strip                     # the tote look's rows: twenty names, three too long for a 1920 px row
                                                           # (they scroll; eleven at 1680 px), counts of 0, 7, 23 and 104; a bet on
                                                           # 2 Grand Mo the First every 4 s takes it 7 -> 12 (9 -> 10: its name area
                                                           # gives up a tile and it starts to scroll), then back to 7; no closing
                                                           # time, nothing else ticks
python tools/fake_pi5.py --phase strip-static              # the same picture with nothing moving
python tools/fake_pi5.py --phase counted                   # AT_THE_POST on the 2026 race, scale pot $154 (WIN $92 / PLACE $39 / SHOW $23), nothing
                                                           # counted yet; post `counted 152` to the fake and the pot reads $152 under a HAND COUNTED
                                                           # tag, WIN $91 / PLACE $38 / SHOW $23; `counted` alone puts the scale figures back
python tools/fake_pi5.py --phase results-static --counted 152   # the results screen over a hand counted pot
# --port 5077 (the splash), --pi5-port 5078 (the fake), --period 15 (cycle, and each bench-reset or results step),
# --host 127.0.0.1, --no-splash (the fake alone; point a splash at it)
# every phase carries the race (KENTUCKY DERBY, post time a day from now on America/Chicago's clock), Dallas 88°F Sunny
# and the track's odds by number (the redesign field's line; in redesign 23 Robusta has none: the dim dash):
# --post-in 74 (minutes to post; negative: already past, AND THEY'RE OFF), --no-post (the countdown leaves the playlist),
# --no-weather (the crawl leaves it out), --no-odds (every roster row a dim dash)
# --counted 152 starts with the host's hand count entered (states 3-5 only, as on pi5)
```

The older phases carry no horse names, so the board shows `HORSE n`;
every phase carries the full contract (horses 1-24 with `in_field`,
`name`, `odds`, `replaced`, `conflict` and `cups`, `now`, `closes_at`, `prizes`,
`split`, `chyron`, `names_rev`, `scratches` in the record shape,
`cups_online`, `cups_no_horse`, `results`, `race`, `weather`, and `closing`, taken and dropped
by pi5's rule: the first time the state is 3, 4 or 5 with none held, and in
0, 1 or on `reset`; with `pot_scale`, `pot_counted` and `hand_counted`: the hand count
lives and dies with `closing`, makes `closing`'s pot and prizes the count's through the
fake's one `prizes_for`, and the model's own from the post to the end, as pi5 does). A horse's `cup` is a MAC
string (`A0:B7:65:00:00:07` for the fake's cup 7) or `null`, as pi5 has
served it since protocol v2; a renumbered cup keeps its MAC. In `redesign` the cups
that were 5, 9 and 13 carry 21, 22 and 23 with their tokens (the count
that was on 9 is on 22), so the field reads 1-4, 6-8, 10-12 down the left
and 14-19, 21-23 down the right with the last slot blank; the chyron
carries the three replacements and `[20] FULLEFFORT · RE-BET YOUR TOKENS`.

Open the printed URL (`http://127.0.0.1:5077/display`) in a browser; add
`?look=dots` (or `impact`, `numbers`) to see a look. The
fake's `POST /api/quiniela/cmd` answers `{"ok": true, "echo": "<cmd>"}`;
`state N` switches its phase, `reset` plays pi5's reset (counts 0, events
and results cleared, state 0), `results W P S` names the winners as the
dashboard's SET WINNERS does (three different horses 1-24, else a 400;
in state 5 the board flips to the results screen; `results` alone clears
them and the board goes back to OFFICIAL RESULTS COMING), `scratch H 1` /
`scratch H 0` flips the no-replacement scratch on horse H (it leaves the
field, its tokens leave the pot), `renumber A B` is the replacement scratch (the cup on
horse A becomes horse B, tokens and all: A's row is gone and B appears
where its number sorts, no event, no toast, and the `redesign` feed's bet
on 7 then lands under B, since the token is the cup's; `renumber B A`
undoes it) and
`name N Some Long Name` renames horse N, 1-24 (`names_rev` bumps, no
event; `name N` alone clears it), which is how to watch a long name shrink
to fit its row (38 px down to 20 px), or scroll in `dots` and on the
roster slide. `post MINUTES` moves the post time (negative: already
past; `post none` clears it), `weather 88 Partly cloudy` sets the
weather (`weather none` clears it) and `odds none` / `odds on` take the
odds away and give them back, and `counted 152` enters the hand count of
the cash box (whole dollars, 0 to 10000, in states 3 to 5 once the figures
at the post exist, else a 400; `counted` alone clears it): what the admin
page, pi5's weather feed and its odds poller do on a real pi5. Post the
fake-only commands (`reset`, `results`, `scratch`, `renumber`, `name`,
`post`, `weather`, `odds`, `counted`) to the fake itself on 5078: the real relay forwards everything to
pi5 unchecked, but pi5 would reject them. Through the relay, `state N`
drives the takeover:

```bash
curl -s -X POST localhost:5077/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'   # board up
curl -s -X POST localhost:5077/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 5"}'   # WINNER: OFFICIAL RESULTS COMING
curl -s -X POST localhost:5078/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"results 7 3 10"}'   # the results screen
curl -s -X POST localhost:5077/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 6"}'   # board down
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
# a cup set to horse 7 on its own screen (hold -> HORSE -> 7 -> SET), then betting opens: the TV shows the board
curl -s -X POST localhost:5001/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'
# drop a token into that cup: horse 7 ticks to 1 on the TV
# WINNER: the board stays, OFFICIAL RESULTS COMING
curl -s -X POST localhost:5001/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 5"}'
# the results, as the dashboard's SET WINNERS saves them (pi5's route, not relayed): the results screen
curl -s -X POST localhost:5000/api/results -H 'Content-Type: application/json' -d '{"win":7,"place":1,"show":2}'
# AFTER_PARTY: the board yields to the playlist
curl -s -X POST localhost:5001/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 6"}'
```

Unit tests, no port and no network beyond loopback needed:

```bash
cd splash_display && python -m unittest -v tests.test_quiniela
```

`BoardPageTests` runs the board's own script (the real template and
`quiniela_board.js`, fed models through a stand-in stream) in headless
Chrome or Chromium and reads the figures off the page: once betting has
closed it must show `closing`, not the live fields. It finds `chromium`,
`chromium-browser` or Chrome by itself (`DDM_CHROME` names another) and is
skipped where there is none. `HandCountTagTests` does the same for the hand
count's tag, at 1920 x 1080 in all three looks (and at 1680 x 1050 in
`dots`): it is up with a count and down without, the pot, its label, the
prize tiles, the banner and the logo do not move by more than 2 px (they do
not move at all), it hangs left of the figure at its middle and clear of the
logo, it is lettered in Impact in every look (the board's own stack, never a
face of its own: its text is as wide as the same text in Impact, or in Anton
where Impact is not installed, and not as wide as the plain sans-serif;
capitals within 2 px of the old dot tag's 21; no wider than that tag; amber
and plain in the tote looks, a gold pill in `impact`), it is measured only
after its face has loaded (a slow Anton, and a room that only Anton's real
width fits), it follows a figure that changes width, a resized window and a
tote face that arrives late, it is there on the results screen and not in
FINAL CALL, and it gives way to `COUNTED` when the room is short.
`StepCrawlTests` reads the crawl of `dots` frame by frame under the same
headless Chrome's virtual clock: the row fills the band by the rows' rule
(78 tiles at 1920 px, 68 at 1680), no tile is ever anywhere but where it
started (0 px over every frame of five seconds), a step is the whole
message one tile to the left, the rate is `CRAWL_TILES_PER_SEC` (5: the
old track's 120 px/s to within 3 %) and `?crawl_tps=` overrides it (2,
60; not a number is the default; the rate stays from 0.25 to 60), the
window equals the loop at every step across
two seams and the loop ends in its gap, a live item that keeps its
length is written where it stands (on the tiles at once, the message not
rebuilt, the crawl not restarted), a scratch added waits for the loop
boundary and the offsets run on through it, the row follows a resized
window, `numbers` and `impact` keep the track and say the same items, and
a character the face lacks is its base letter or a blank (every character
on the tiles is one the face draws). `StepCrawlSourceTests` reads the
source: the rate is a constant above the script's first function, the
steps come from animation frames and no timer, and no rule of the tiles
moves anything; `ToteFontTests` holds the script's list of the face's
characters to the face's own character map.

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
