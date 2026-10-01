#!/bin/sh
# Run as the logged-in desktop user, never with sudo.
set -eu
port=${RUST_DASHBOARD_PORT:-8090}
case "$port" in ''|*[!0-9]*) echo 'Invalid dashboard port' >&2; exit 1;; esac
state=${XDG_STATE_HOME:-"$HOME/.local/state"}/rustberrypi-browser
mkdir -p "$state"
exec 9>"$state/launcher.lock"
flock -n 9 || exit 0
browser=${RUST_DASHBOARD_BROWSER:-chromium}
# Desktop startup can precede the collector. Wait for HTTP, not game readiness.
python3 - "$port" <<'PY'
import sys
import time
import urllib.request

for attempt in range(60):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{int(sys.argv[1])}/api/status', timeout=2) as response:
            if response.status == 200:
                break
    except OSError:
        pass
    time.sleep(2)
# Still open the pages after the bounded wait so a failed collector is visible.
PY
# Give the desktop compositor time to restore its saved display layout.
sleep 5
# Xwayland provides explicit window positions; native Wayland placement is compositor-controlled.
# --password-store=basic keeps Chromium off the Secret Service. Desktop auto-login never unlocks
# the login keyring through PAM, so a libsecret store would block startup on an unlock prompt.
# These profiles hold no credential: the dashboard is read-only, local and has no sign-in.
open_window() {
  "$browser" --ozone-platform=x11 --no-first-run --disable-session-crashed-bubble \
    --password-store=basic --user-data-dir="$state/$1" --class="rustberry-$1" \
    --window-position="$2" --window-size="$3" --app="http://127.0.0.1:$port/$1" &
}
open_window game 0,0 1920,1080
open_window touch 1920,630 1280,720
open_window system 3200,0 1920,1080
wait
