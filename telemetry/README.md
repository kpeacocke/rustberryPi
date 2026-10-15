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
(UTC day boundaries, files `0600`, directory `0700`). The optional workstation
training tool reads these archives; the live CPU monitor consumes the same
sanitized samples directly and does not scan the archive. Rows are a flat,
versioned schema (`schema: 1`) of numbers, booleans and
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

## Server early warning and Hailo

### CPU baseline: included with telemetry

The Overview and System dashboards report `server_health` from the existing
loopback-only `GET /api/status` endpoint. The HAT worker reads only the numeric
schema-1 `GET /api/health-sample`, never the player-bearing status document.
No new port or control endpoint exists.
The analysis remains available when feature archival is disabled.

- Require a fresh RCON reply, successful A2S query, active game and at least five
  minutes of game uptime. Maintenance, startup and unavailable data do not train.
- Match the exact connected player count and a 5,000-entity band. A new workload
  says **learning**, rather than claiming it is healthy or unhealthy.
- Collect 120 eligible samples per workload (about ten minutes at the usual
  cadence). Learn median and median absolute deviation for FPS, CPU, memory,
  swap, temperature, disk reads/writes, A2S latency and RCON latency.
  A metric needs at least 80% coverage; unassessed metrics are listed.
- Use minimum scales of 5 FPS, 10 CPU percentage points, 5 memory percentage
  points, 64 MiB swap, 3 degrees Celsius, 128 KiB/s disk reads/writes, 5 ms A2S
  and 20 ms RCON. Flag adverse deviations of at least three scales; FPS is
  adverse downwards, the other metrics upwards. These are operational heuristics,
  **not probabilities, diagnoses or forecasts**.
- A single deviation says **watching**. The same metric must deviate for six
  consecutive eligible samples (roughly 30 seconds) before **review**. Gaps over
  20 seconds, workload changes and unavailable samples reset persistence.
- Baselines freeze after learning to avoid quietly treating a deterioration as
  normal. They depend on the initial samples being representative. Existing
  hard-limit suggestions and the watchdog continue independently.

State is atomically written at most once per minute, plus completed learning and
new maintenance events, to the private USB telemetry directory as `health.json`.
There are at most 24 workloads in each of four bounded sets: frozen baselines,
rolling pre-maintenance history, the latest comparison's reference and its
post-maintenance samples. Each workload holds at most 120 numeric metric rows.
Corrupt/oversized state is preserved and visibly reported rather than overwritten.
To intentionally relearn, stop telemetry, move `health.json` to a dated private
backup outside its original path, then restart telemetry. Do not delete the
whole telemetry directory; it also holds player history and archives.

### Maintenance comparison

Only maintenance that passes both game-query and authenticated RCON readiness
writes `/var/lib/rustberrypi/server-health-maintenance.json`, using the Pi's clock.
The collector restarts during recovery and waits until the maintenance marker is
released before resuming analysis. Its saved rolling history supplies the
pre-maintenance reference. After 120 eligible post-maintenance samples in a
matching workload, the dashboard shows before/after medians and absolute changes.
The comparison remains fixed for that event, not a moving claim of causation.

`not-recorded`, `collecting`, `unmatched`, `comparable` and `review` distinguish
the evidence states. A changed player count or entity band can be unmatched;
no regression or improvement is inferred in that case. The rolling reference is
the most recently observed samples in that workload, not necessarily the
immediately preceding ten minutes of wall time. Failed/interrupted maintenance
does not create successful completion evidence. Only the latest verified event
is compared; this is not a historical maintenance-results database.

### Optional Hailo predictor: release candidate, hardware acceptance pending

`rust_telemetry_hat_enabled` defaults to `false`. This feature does not use a
camera, identify players or train on the Pi. A small learned regression model
estimates the expected current metrics from player count and entity count.
It is exported as a 1x1 convolution plus ReLU, operating on a repeated 32x32
workload tensor. Spatial repetition is only a compiler tensor-layout contract;
there are no images or invented time-series inputs.

This is an experiment in workload-conditioned residual scoring, not a chatbot
or a failure predictor. A two-input model cannot explain every cause of server
load, and may not beat the simpler statistical baseline. Review its chronological
holdout errors against the median baseline before adopting it.

**No device-specific HEF is bundled.** Real training data and successful Hailo
Dataflow Compiler execution are prerequisites, not optional acceptance checks.
The compiler is proprietary workstation tooling; it is not installed by Ansible
and cannot run on this macOS controller or the ARM game server. A small network
can still hit compiler allocation/quantization limitations. ONNX export and a
mocked SDK contract test do not prove compilation or accelerator compatibility.

#### 1. Identify the hardware and compatible runtime on the Pi

```bash
hailortcli fw-control identify
ls -l /dev/hailo0
/usr/bin/python3 -c 'import numpy, hailo_platform'
```

Record whether the chip is **HAILO8** (`hailo8`) or **HAILO8L** (`hailo8l`).
Do not infer it from "first generation": the original AI Kit used Hailo-8L.
Use Hailo/Raspberry Pi's supported driver, firmware and Python runtime combination
for that chip. The worker uses the HailoRT 4.20-style synchronous `InferModel`
API; that source API was checked, but no installed Pi runtime is certified here.
Do not install a different kernel or change this Rust server's required 4 KB
page configuration just to satisfy an accelerator package.

Arrange `/dev/hailo0` read/write access for `rust-telemetry` through a reviewed
vendor udev/group policy. Do not grant root, sudo or world-writable device access.
The play fails explicitly if the Python runtime or device access is absent.

#### 2. Train on representative real server samples

Collect at least 1,200 complete eligible samples spanning at least one hour
(normally at least 100 minutes at five-second cadence). Exercise representative
player and entity counts. Copy only the numeric feature archive into a private
operator workspace; never copy RCON credentials or player databases.
The exporter reads at most the latest 20,000 eligible rows, uses the first 70%
for fitting and the last 30% for validation, and does not shuffle across time.
Corrupt archive rows fail explicitly. Missing/unhealthy rows are excluded.

In a separate Python 3.12+ workstation environment:

```bash
python -m venv .venv-health
.venv-health/bin/pip install -r requirements-health-model.txt
.venv-health/bin/python tools/train_server_health.py train \
  --features /private/copied-features \
  --output /private/health-model \
  --architecture hailo8
```

Substitute `hailo8l` if appropriate. The output directory must be empty.
Outputs are `server-health.onnx`, NHWC `calibration.npy` and `manifest.json`.
Inspect `validation_rmse` versus `median_baseline_rmse`: lower is better.
Constant metrics or poor coverage can give little useful learned signal.
The CPU reference and runtime only accept workloads inside the observed input
ranges; new workloads need additional real data and retraining.

#### 3. Compile in a supported x86-64 Linux Hailo SDK environment

Transfer the model directory and check out this repository in your licensed,
compatible SDK environment. Use that environment's Python/numpy; do not install the workstation
requirements over the SDK's own dependency pins.

```bash
python tools/train_server_health.py compile --model-dir /private/health-model
```

Compilation must actually succeed. This creates `server-health.hef` and binds its
SHA256 into the manifest. Keep ONNX, calibration, compiler/runtime versions and
validation results together as a private versioned artifact. Retrain/compile
into a new directory rather than overwriting the deployed model.

#### 4. Deploy and prove real inference

In private host vars or AWX:

```yaml
rust_telemetry_hat_enabled: true
rust_telemetry_hat_model_dir: /absolute/controller/path/health-model
```

Run the normal telemetry playbook with your private inventory:

```bash
ansible-playbook -i inventory/hosts.local.yml playbooks/telemetry.yml
```

The model/manifest are root-owned, group-readable under `/opt/rustberrypi/models`.
The separate `rust-health-hat.service` runs as `rust-telemetry`, with a 256 MiB
memory limit, 15% CPU quota, one-second inference timeout and a 30-second systemd
watchdog. The unit also hides the RCON credential and player-history file.
It cannot restart Rust or change game settings. A worker crash or hung
vendor runtime cannot block the telemetry sampling thread.
With `rust_start: true`, deployment also waits for a real fresh HAT result
(`ready` or `review`), not merely an active systemd unit. It allows up to
7.5 minutes for the five-minute game-uptime gate and model startup, then fails
explicitly if inference is still unavailable. Preparation with `rust_start: false`
skips this gate and is not evidence of working hardware.

On the Pi:

```bash
sudo systemctl status rust-health-hat --no-pager
sudo journalctl -u rust-health-hat -n 50 --no-pager
curl -fsS http://127.0.0.1:8090/api/status \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["server_health"],indent=2))'
```

For a single hardware inference test, first stop the worker so it does not contend
for the device, then run:

```bash
sudo systemctl stop rust-health-hat
cd /opt/rustberrypi
sudo -u rust-telemetry /usr/bin/python3 -m telemetry.hat --once
sudo systemctl start rust-health-hat
```

`--once` exits nonzero unless actual Hailo inference succeeds with a valid
prediction. It never substitutes CPU output. Every result must match the CPU
reference within 0.05 normalized units per output, before any score is accepted.
Predictions are compared against observed metrics with a three-scale threshold
using holdout RMSE and the CPU monitor's minimum scales. Unlike the CPU monitor,
HAT residual evidence describes that individual sample, not a six-sample alert.
The API expires results/source samples after 20 seconds and explicitly reports
disabled, unavailable and error states. A missing model is not success.

### Tonight's hardware acceptance and rollback

1. Deploy CPU monitoring first; observe real learning and workload changes.
2. Verify a successful maintenance event leads to matched or explicitly unmatched
   before/after comparison. Do not run maintenance just to test the UI.
3. Identify the actual chip and runtime, collect/copy real samples, compile and
   deploy only after the preceding gates succeed. Compiler failures must be fixed
   before claiming the HAT path is ready.
4. Verify real hardware/reference parity, nonzero exit on a wrong checksum or
   incompatible model, stale/unavailable reporting when the worker is stopped,
   and continuing telemetry/game operation during worker failures.
5. Compare Rust FPS, query latency, host CPU, memory and temperature with the
   worker disabled/enabled under comparable load for at least 30 minutes each.
   Release acceptance: no new game restarts, mean CPU increase below five
   percentage points, and median FPS reduction below 5%. These thresholds are
   acceptance targets, not measured results from this authoring session.

To roll back HAT use, set `rust_telemetry_hat_enabled: false` and converge. This
stops/disables the worker and removes its result from API analysis; CPU monitoring,
Rust, dashboards, feature capture and camera settings remain independent.
To stop immediately, use `sudo systemctl stop rust-health-hat`. No model drives
automated updates, bans, restarts, tuning or watchdog decisions.

References: [Raspberry Pi AI Kit](https://www.raspberrypi.com/documentation/accessories/ai-kit.html),
[HailoRT 4.20 Python API](https://github.com/hailo-ai/hailort/blob/v4.20.0/hailort/libhailort/bindings/python/platform/hailo_platform/pyhailort/pyhailort.py).

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
camera on the next login. The camera is only ever opened by this feature. Enabling
it installs `python3-picamera2` and `wlopm` and adds the desktop account to the
`video` group, which takes effect at the next login. Sensitivity is tuned with
`rust_telemetry_screen_pixel_threshold` and `rust_telemetry_screen_area_fraction`;
see [display setup](../dashboard/README.md) for what those two control.

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
