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

To manage startup with Ansible, add these to your private host variables:

```yaml
rust_telemetry_enabled: true
rust_telemetry_desktop_autostart: true
rust_telemetry_desktop_user: your_desktop_login
```

Run `playbooks/telemetry.yml` (or the full `playbooks/rust.yml`). This installs
Chromium and Xwayland and manages the selected user's
`~/.config/autostart/rustberrypi.desktop`. The launcher waits for the local
dashboard endpoint before opening the three windows, with a bounded timeout;
it does not wait for Rust to finish starting. The configured telemetry port is
passed automatically. Set autostart to false, keeping the desktop user specified,
and rerun to remove the startup entry. Already open windows are left running.

For unattended startup after reboot, enable desktop auto-login for that account
in your Pi's desktop login settings. Ansible does not change login policy.
Auto-login gives anyone with physical access that desktop session. Without it,
the dashboards open when you log in. Keep the saved display layout matching the
coordinates above. Actual placement still depends on your compositor.

Alternatively, manually create a **private local** file
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
