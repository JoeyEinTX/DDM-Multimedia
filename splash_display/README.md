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
├── requirements.txt
├── content/
│   ├── trivia.json          # Trivia cards by category
│   └── splash_pages.json    # Splash templates with timing
├── templates/
│   ├── base.html
│   ├── slideshow.html       # Master slideshow (kiosk URL target)
│   ├── splash/{countdown,la_subasta,la_quiniela,derby_dash,ddm_brand}.html
│   └── trivia/{fact_card,qa_reveal}.html
├── static/
│   ├── css/ddm_style.css
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
