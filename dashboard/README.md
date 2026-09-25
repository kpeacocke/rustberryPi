# Three local displays

Open these in separate browser windows on the Pi desktop:

| View | URL | Intended display |
| --- | --- | --- |
| Game and connected players | http://127.0.0.1:8090/game | Left HDMI, 1920×1080 |
| Large overview | http://127.0.0.1:8090/touch | Middle touch, 1280×720 landscape |
| Hardware and maintenance | http://127.0.0.1:8090/system | Right HDMI, 1920×1080 |

Drag each window to its display and press F11. The current layout has coordinates
0,0 / 1920,630 / 3200,0. An optional `sh /opt/rustberrypi/dashboard/launch.sh`
launcher requests those positions through Xwayland. It needs Chromium and Xwayland
already installed, runs as the desktop user, and uses three dedicated profiles.
It never changes monitor rotation or touch calibration. Window placement can still
depend on the compositor; manually position/fullscreen if needed. Different layouts
should use manually placed windows or a locally adapted launcher.

The pages use local assets only and one shared collector; no CDN, fonts, analytics
or external browser requests. The API is polled every five seconds. UI text is
inserted with textContent, including untrusted player names. No game or system
control buttons are exposed. Navigation is touch-friendly and player addresses
and Steam IDs are omitted from browser data.

To autostart after desktop login, create a **private local** file
`~/.config/autostart/rustberrypi.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=RustberryPi displays
Exec=sh /opt/rustberrypi/dashboard/launch.sh
Terminal=false
```

This does not enable automatic desktop login. Change the launcher port with
`RUST_DASHBOARD_PORT` if needed. Keep Raspberry Pi Connect available for recovery.
Browser overhead on a Pi running FEX must be measured on the actual device;
close unused desktop applications and compare server performance before/after.
