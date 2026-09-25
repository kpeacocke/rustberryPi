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
# Xwayland provides explicit window positions; native Wayland placement is compositor-controlled.
"$browser" --ozone-platform=x11 --no-first-run --disable-session-crashed-bubble \
  --user-data-dir="$state/game" --class=rustberry-game --window-position=0,0 \
  --window-size=1920,1080 --app="http://127.0.0.1:$port/game" &
"$browser" --ozone-platform=x11 --no-first-run --disable-session-crashed-bubble \
  --user-data-dir="$state/touch" --class=rustberry-touch --window-position=1920,630 \
  --window-size=1280,720 --app="http://127.0.0.1:$port/touch" &
"$browser" --ozone-platform=x11 --no-first-run --disable-session-crashed-bubble \
  --user-data-dir="$state/system" --class=rustberry-system --window-position=3200,0 \
  --window-size=1920,1080 --app="http://127.0.0.1:$port/system" &
wait
