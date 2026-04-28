# DDM Splash Display — Pi 4B Autostart Setup

Step-by-step for a fresh Raspberry Pi 4B running **Pi OS Bookworm (full desktop)**.
Run as the `pi` user unless noted.

---

## 1. Enable autologin to desktop

```bash
sudo raspi-config
```

Navigate to:

`1 System Options` → `S5 Boot / Auto Login` → `B4 Desktop Autologin`

Reboot when prompted.

> Bookworm uses **Wayfire** (Wayland) on Pi 4 by default, with **labwc** as
> the alternative. Older builds may still be on **LXDE/X11**. The autostart
> step below covers all three; pick the section that matches your Pi.

---

## 2. Disable screen blanking (display power management)

### Bookworm desktop (Wayfire / labwc, recommended)

Use the GUI:

`Preferences → Screen Configuration → Configure → Screens → Disable Screen Blanking`

Or via raspi-config:

```bash
sudo raspi-config
# 2 Display Options → D4 Screen Blanking → No
```

### Older LXDE / X11 fallback

Append to `/etc/xdg/lxsession/LXDE-pi/autostart`:

```
@xset s off
@xset s noblank
@xset -dpms
```

---

## 3. Install the Flask service

```bash
# Clone the repo (if not already done)
cd ~
git clone <your-repo-url> DDM-Multimedia
cd DDM-Multimedia/splash_display

# Install Python deps (Bookworm requires --break-system-packages for pip)
pip install -r requirements.txt --break-system-packages

# Drop logos into static/img/ before first run:
#   ddm_master.png  la_subasta.png  la_quiniela.png  derby_dash.png

# Install + enable the systemd unit
sudo cp deploy/splash_display.service /etc/systemd/system/splash_display.service
sudo systemctl daemon-reload
sudo systemctl enable splash_display.service
sudo systemctl start splash_display.service

# Verify it is up
sudo systemctl status splash_display.service
curl -fsS http://localhost:5000/api/slides | head -c 200; echo
```

---

## 4. Set up Chromium kiosk on desktop login

Make the kiosk launcher executable:

```bash
chmod +x ~/DDM-Multimedia/splash_display/deploy/kiosk.sh
```

### Bookworm with Wayfire (Wayland) — DEFAULT on Pi 4 Bookworm

Edit `~/.config/wayfire.ini`. Add an `[autostart]` section if missing:

```ini
[autostart]
splash = /home/pi/DDM-Multimedia/splash_display/deploy/kiosk.sh
screensaver = false
dpms = false
```

Save and reboot. Wayfire will launch the kiosk script after login.

### Bookworm with labwc (alternative)

Edit (or create) `~/.config/labwc/autostart`:

```bash
/home/pi/DDM-Multimedia/splash_display/deploy/kiosk.sh &
```

Make sure the file is executable:

```bash
chmod +x ~/.config/labwc/autostart
```

Reboot.

### LXDE / X11 fallback (older builds)

Edit (or create) `~/.config/lxsession/LXDE-pi/autostart`:

```
@/home/pi/DDM-Multimedia/splash_display/deploy/kiosk.sh
```

Reboot.

---

## 5. Update workflow (content / code)

```bash
cd ~/DDM-Multimedia
git pull
cd splash_display
# If requirements.txt changed:
pip install -r requirements.txt --break-system-packages
sudo systemctl restart splash_display.service
```

To pick up content edits without restarting the service, just refresh the
browser (the playlist is rebuilt on every `/api/slides` call):

```bash
DISPLAY=:0 wtype -k F5    # wayland
# or, X11:
DISPLAY=:0 xdotool key F5
```

In practice it's easier to just restart the kiosk:

```bash
pkill -f chromium && sleep 2 && ~/DDM-Multimedia/splash_display/deploy/kiosk.sh &
```

---

## 6. Useful commands

```bash
# Tail the Flask service logs
sudo journalctl -u splash_display.service -f

# Stop the kiosk display only (leave Flask running)
pkill -f chromium

# Re-launch the kiosk manually
~/DDM-Multimedia/splash_display/deploy/kiosk.sh
```
