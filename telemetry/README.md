# Local telemetry

The collector runs as `rust-telemetry` and serves only `127.0.0.1:8090`. It samples
every five seconds and keeps ten minutes of trends in memory. RCON is loopback-only
with a random per-host password; only `serverinfo` and `playerlist` are queried.
The browser receives no RCON credential, Steam ID, player address or raw log text.
Player names are optional (`rust_telemetry_show_names: false` anonymises them).
Successful RCON polls share one persistent WebSocket connection. After a failed
connection or command, the collector closes it and waits 60 seconds before retrying;
host metrics continue updating and old game samples retain their timestamps.
This avoids creating a new connection every five seconds and rapid failed retries.
The API has no mutation or arbitrary-command endpoint, rejects non-local Host
headers, and does not enable CORS. Never forward this port or proxy it publicly.

`maintenance.py` is a separate root oneshot, scheduled twice daily. It refreshes
APT metadata and inspects package candidates, including security origins. It runs
SteamCMD app-info as `rust` under the same lock as backup/update, verifies the USB
UUID, and compares the public build with the installed manifest. SteamCMD may
update its own bootstrap; no Rust update or package installation is requested.
It also checks the latest upstream FEX release, backup evidence and a bounded
journal summary. FEX release differences are for review, not an automatic upgrade.
Checks can take several minutes. Failed checks retain earlier values but mark
them failed; their previous success timestamp remains visible. A reboot marker's
absence does not prove that no reboot is necessary.

## Deploy

Use your existing private inventory, identity and pins. On the Pi:

When upgrading from an older dashboard installation, first run the full
`playbooks/rust.yml` with `rust_telemetry_enabled=true` to install the managed
startup helper. This fixes a missing local RCON listener by passing its settings
before Rust initializes networking. It causes one restart on an existing setup.

```sh
ansible-playbook playbooks/telemetry.yml --connection local --ask-become-pass \
  -e ansible_python_interpreter=/usr/bin/python3
```

Or set `rust_telemetry_enabled: true` in private host vars and converge `rust.yml`.
The first deployment enables authenticated RCON in the persistent `server.cfg`
and gracefully restarts Rust if `rust_start` is true. Unchanged subsequent runs
do not restart Rust. The credential is stored in `/etc/rustberrypi/rcon-password`
and the persistent `server.cfg`; treat both and NAS archives as sensitive. On a
microSD rebuild a new credential is generated and both sides are updated together.
Existing RCON clients would need the new credential. No router or UFW rule is added.
Review any pre-existing custom RCON settings before deploying.

The startup helper reads only the managed telemetry block and passes its password,
WebSocket mode and loopback binding as Rust startup arguments. The password is not
embedded in the public systemd unit or Ansible output, but can be visible in local
process arguments and game startup diagnostics. Treat process listings and raw
Rust journals as sensitive; redact before sharing. The collector logs exception
types only, never credential-bearing RCON URLs. Deployment now waits for a fresh
authenticated RCON sample, not just a successful dashboard HTTP response.

The standalone play assumes FEX at `/usr/bin/FEX` and the project's default RootFS.
If yours differs, set `rust_telemetry_fex`, `rust_telemetry_rootfs`, and
`rust_telemetry_fex_version` in private vars. Full convergence uses the selected
FEX facts. Configure `rust_telemetry_port`, `rust_telemetry_check_schedule`, and
`rust_telemetry_show_names` as needed. The timer starts a first check after boot
and may randomise it by five minutes. For an immediate operator-triggered check:

```sh
sudo systemctl start rust-maintenance-check.service
sudo journalctl -u rust-maintenance-check -n 30 --no-pager
```

Check `sudo systemctl status rust-telemetry` and `sudo ss -lntp`: HTTP must be on
127.0.0.1:8090 and RCON on 127.0.0.1:28016. No public listener is intended. Confirm
that a joining/leaving player appears, unplug/reconnect telemetry to check the
stale banner, and verify update comparisons before relying on them.

## Feature archive

Each sample also appends one JSON line to a local feature archive at
`/srv/rust/data/.rustberrypi-telemetry/features/features-YYYY-MM-DD.jsonl`
(UTC day boundaries, files `0600`, directory `0700`). This is an offline record
for later trend and anomaly work; nothing reads it today and no behaviour depends
on it. Rows are a flat, versioned schema (`schema: 1`) of numbers, booleans and
short status strings: host load, thermals and throttle bits, disk and network
rates, Rust FPS/entities, A2S and RCON reachability with their round-trip
latencies, systemd state and restart count, watchdog state, backup age, and
suggestion counts.

The archive records counts only. No player name, Steam ID, address, chat, command
or raw log text is ever written, so it carries no more information than the
dashboard already shows. Values that are unknown are written as `null` rather than
carrying a stale reading forward; a player count is omitted entirely unless the
RCON poll succeeded in that same sample.

Writing is best effort. A full disk, a permissions problem or a corrupt path is
recorded as `feature_error` on the API and never interrupts sampling or the
dashboard. Retention is bounded twice: files older than
`rust_telemetry_feature_retain_days` (default 90) are deleted, and the oldest
files are dropped once the directory exceeds `rust_telemetry_feature_max_mib`
(default 256). The file currently being appended to is never deleted, so the
ceiling can be briefly exceeded by one day's rows. At the five-second cadence the
size ceiling is normally the binding limit. Pruning runs when a new day's file is
created and roughly hourly otherwise, not on every sample.

Set `rust_telemetry_feature_capture: false` to disable the archive entirely; the
collector then performs no feature extraction and writes nothing. The directory
lives under the collector's existing private telemetry directory on USB, so it is
covered by the same ownership reclaim and the existing data backup.

## Camera presence sensing

Optional and off by default. When `rust_telemetry_screen_blank` is enabled, the Pi
camera is used as a presence sensor so the three displays switch off after
`rust_telemetry_screen_idle_minutes` (default 10) of an empty room, and switch
back on as soon as somebody moves in front of them.

Because waking on movement was a requirement, the camera necessarily runs
continuously whenever the desktop session is up, including while the screens are
blank. This is a wider posture than the rest of this project, so the handling is
deliberately narrow. Frames are captured at roughly two per second into memory at
the sensor's low-resolution stream, reduced to about 80×60 brightness samples,
compared against the previous frame, and then discarded. Colour is never examined.
No frame is written to disk, logged, cached, transmitted, or exposed through the
dashboard API. Nothing derived from the camera enters the feature archive. The
only value the detector produces is a boolean for "something changed", which is
used immediately and not retained. There is no recording, no face or person
detection, and no identification of any kind.

The kill switch is `rust_telemetry_screen_blank: false`, which is also the default.
Converging with it disabled removes the autostart entry, so nothing starts the
camera on the next login. The camera is only ever opened by this feature.

Failures resolve towards the screens staying on. If the camera is missing, busy,
or erroring, presence is reported as unknown and the displays are never blanked.
If no supported display power command is available the feature disables itself and
leaves the screens on. The process also restores power on exit, including when it
is terminated, so a crash cannot leave the displays dark.

## Evidence and limits

The Overview and Players & game screens list every approved player from the
managed access policy, including offline players. Last-login dates are estimates
derived from Rust's ConnectedSeconds, displayed with ≈ in the browser's local
timezone. There is no retrospective journal scraping: players not observed since
this feature was deployed show “Not observed yet”. Names are learned on connection;
optional private `rust_telemetry_player_names` maps Steam IDs to display aliases
for players who have not connected yet. `rust_telemetry_show_names: false` also
anonymises this roster. IDs and addresses never appear in the browser API.

History lives at `/srv/rust/data/.rustberrypi-telemetry/players.json` on USB and
is included in the existing data backup. It is written on session changes and
at most once per minute otherwise. Login history survives service restarts and
OS rebuilds; an interrupted write or sudden power loss can lose recent samples.
A corrupt history file is preserved and reported instead of overwritten.
Ansible grants the collector traversal only on the data parent and ownership of
its own private history directory. Reapply telemetry after restore to restore
ACLs and ownership; no access to world contents or the access-policy file is granted.
Offline is only reported after a fresh successful player-list response; unavailable
RCON reports unknown status and retains the last recorded date.

* Current health and player data require actual device/Rust responses. Unknown
  values stay unknown. Ready requires service active plus A2S and RCON responses. Service active plus unavailable RCON is labelled as
  starting/telemetry unavailable, not conclusively healthy or failed.
* Backup/restore successes are recorded by the updated backup helper. Existing
  archives are not retroactively counted. Deploy the full play once to update
  that helper. A restore success records an operation, not proof of playability;
  still test a recovered server on a spare system.
* Last save and warning counts come from at most 1000 journal records in 24 hours,
  sampled on the slow maintenance schedule. Warnings are text matches, not proof
  of a crash. Displayed ages and check timestamps matter.
* Rust+ is a local TCP-listener check, not a WAN/pairing check. Public IP routing,
  router firmware, firewall auditing and AWX job status are not probed in this v1.
* Pi temperature reads sysfs. `vcgencmd` may be absent or inaccessible to the
  unprivileged collector; voltage/throttle status is then unknown. No broad device
  permissions are granted just to fill a tile. Disk I/O is host-wide, not USB-only.
* Restart counters are cumulative. Swap used is not swap rate. Suggestions report
  evidence and uncertainty; they never execute remediation.
* RCON is an administrative credential despite this app issuing only read-only
  commands. Protect the local account and physical desktop accordingly.

Sources: [Facepunch WebRCON](https://github.com/Facepunch/webrcon),
[Raspberry Pi vcgencmd](https://www.raspberrypi.com/documentation/computers/os.html#vcgencmd).

Player capacity is shown as `connected / capacity` on the touch and game screens.
Restricted mode uses the actual approved roster count (up to five), because its
vanilla public slot setting is deliberately zero. Public mode uses configured
`rust_maxplayers` (1–5, default 5). Stale RCON still displays Unknown, and roster
Steam IDs are never sent to the browser.
