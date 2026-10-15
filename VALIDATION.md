# Validation record

## Automatic accelerator and camera provisioning — 2026-10-15

Added `pi_hardware` to both full deployment (after 4 KB kernel verification)
and telemetry-only deployment. The user's live output establishes a PCI-visible
Hailo-8-family device (`1e60:2864`), missing Hailo driver/runtime, a 4 KB running
kernel, and an IMX708 bound in the boot log. That evidence motivated automatic
provisioning; **the new role has not yet run on the Pi**.

Discovery tests use a temporary sysfs tree and cover those exact PCI/sensor
values, unrelated devices, absent/unbound peripherals and explicit read failures.
Provisioning contract tests check package selection, header/kernel safeguards,
permissions, playbook ordering, capture-free camera enumeration and presence-only
startup directory creation. They do not prove a driver build or physical capture.

| Check | Result |
| --- | --- |
| `/opt/homebrew/Cellar/ansible/14.5.0/libexec/bin/python -m unittest discover -s tests -p test_pi_hardware.py -v` | 9 passed |
| `/opt/homebrew/Cellar/ansible/14.5.0/libexec/bin/python -m unittest discover -s tests -v` | 177 discovered: 175 passed, 2 optional model tests skipped |
| `ansible-lint` | Passed production profile, 0 failures |
| `ansible-playbook -i inventory/hosts.yml playbooks/rust.yml --syntax-check` | Passed |
| `ansible-playbook -i inventory/hosts.yml playbooks/telemetry.yml --syntax-check` | Passed |
| `git diff --check` | Passed |

Inspected Raspberry Pi's published Trixie package metadata and the
`hailort-pcie-driver` 4.23.0 installer/rule. The driver installer supplies firmware,
builds with DKMS (falling back to a direct build), and loads/reloads the module.
Its shipped rule grants mode `0666`; the project installs its later restricted
`0660`/`rust-hailo` rule before package configuration. No dependency on
`hailo-all`, TAPPAS or camera inference models was added.

Physical acceptance still requires exact running-kernel headers to resolve,
the driver to build/load, successful firmware identification and Python imports,
working service-account permissions, and libcamera enumeration. Repeat the play
to verify convergence. Enabling presence additionally requires a desktop login
with refreshed group membership and actual blank/wake checks; hardware discovery
alone intentionally does not enable continuous camera use.

## Server early warning and Hailo preparation — 2026-10-15

Software checks ran locally on macOS, **not on the Pi**. The CPU advisory monitor,
maintenance comparison, numeric worker endpoint, optional Hailo worker, model
export/compiler tooling, dashboard and deployment configuration are implemented.
HAT delivery remains a **release candidate**: no real feature corpus was available
here, no chip-specific HEF was compiled and no physical accelerator was exercised.

| Exact command / check | Result |
| --- | --- |
| `/opt/homebrew/Cellar/ansible/14.5.0/libexec/bin/python -m unittest discover -s tests -v` | 168 discovered: 166 passed, 2 optional numpy/ONNX cases skipped in that controller environment |
| `.venv/bin/python -m unittest discover -s tests -p 'test_*health*.py' -v` | 19 passed, including both optional cases above; real ONNX execution compared with CPU reference |
| `.venv/bin/python -m compileall -q telemetry tools tests` | Passed |
| `ansible-lint` | Passed production profile, 0 failures; duplicate installed collection notice remains environmental |
| `ansible-playbook -i inventory/hosts.yml playbooks/telemetry.yml --syntax-check` | Passed |
| `ansible-playbook -i inventory/hosts.yml playbooks/maintenance.yml --syntax-check` | Passed |
| `node --check dashboard/app.js` | Passed |
| `git diff --check` | Passed |
| Actual local HTTP handler tests | GET status/numeric sample 200; hostile Host 403; POST 501; traversal 404 |
| Integrated-browser synthetic fixture | Learning/normal/unavailable/review, HAT error, stale analysis, unmatched comparison and literal-text evidence rendering verified |
| Kiosk layout checks | Five-player Overview fits 1280x720; System fits 1920x1080 with bounded scrolling detail panels |
| Hailo compiler/runtime interface tests | Mocked SDK/device contract only: **not hardware evidence** |
| Real Hailo Dataflow Compiler | Unavailable (`hailo_sdk_client` not installed); not run |
| Snyk code scan | Tool unavailable in this session; not run |

Optional workstation validation used the versions pinned in
`requirements-health-model.txt`. CI now has a separate real ONNX export job so
the optional tests do not silently remain skipped in normal controller jobs.
That workflow has not been run remotely by this authoring session.

Tests also cover workload matching, warmup/startup exclusions, sustained versus
single-sample deviations, gaps, missing sensors, private bounded persistence,
corrupt-state preservation, invalid maintenance evidence, successive before/after
comparisons, HEF manifest validation, CPU/HAT parity rejection, stale results and
nonzero `--once` failure without CPU substitution.

Before hardware release, follow [HAT commissioning and rollback](telemetry/README.md#server-early-warning-and-hailo):
identify the actual 8/8L chip, verify its driver/runtime/device permissions without
changing the required 4 KB kernel, train on real representative samples, compile
successfully on the supported x86 Linux SDK host, prove actual on-device
inference/reference parity, and measure the stated Rust-performance limits.
No deployment, maintenance window, kernel change or game restart was performed.

Repository built on 2026-09-23. Checks ran in Ubuntu under WSL, not on the Pi.

| Check | Result |
| --- | --- |
| Resolve FEX abbreviated revision through GitHub | Full SHA verified; source checkout matches |
| Download dated RootFS and SteamCMD bootstrap | SHA256 digests recorded in role defaults |
| Extract pinned RootFS and generate manifest | 39,527 path entries; compressed manifest included |
| Verify manifest against complete reference extraction | Passed |
| Inventory resolves rustpi and group variables | Passed |
| Syntax check all four playbooks | Passed with ansible-core 2.20.1 |
| ansible-lint production profile | Passed with ansible-lint 26.1.1; no rule suppressions |
| Python helper tests | 27 passed |
| Compile Python helpers and manifest tool | Passed |
| Git whitespace check | Passed |

Tests cover kernel normalization and repeat-run stability, blank USB guards,
RootFS corruption detection and preservation, adoption without subsequent writes,
Steam metadata parsing, unchanged install/update without stop, stop before a real
update, no restart after failed update, idempotent service start, archive traversal
and link rejection, and a real local UDP challenge/response round trip.

No SSH deployment was performed. FEX compilation on Debian/Pi, actual SteamCMD
and Rust execution, full-play zero-change convergence, thermal/performance behavior,
reboot recovery and real NAS backup/restore remain hardware acceptance tests.
The README gives the procedure; these must not be inferred from static checks.

## Mount collection compatibility fix — 2026-09-25

Updated `ansible.posix` from 2.1.0 to 2.2.2. The released mount module uses
`ansible.module_utils.common.text.converters` and `module.warn`, replacing the
deprecated text imports and `warnings` result field. Exercised `state: mounted`
in check mode against a disposable fstab under ansible-core 2.20.1: passed without
the reported deprecation warnings. No actual filesystem was mounted or formatted.
The deployment playbook syntax check also passed. Deprecation warnings remain enabled.

## First Pi deployment follow-up — 2026-09-25

User-provided output confirms the kernel/storage checks, pinned FEX build and
execution smoke test, RootFS extraction and Steam Rust installation completed on
the Pi. A2S readiness subsequently timed out. The cause of that runtime failure
is not established by the Ansible output alone.

Prepared the Rust account's private Ansible temporary directories and configured
Steam library symlink ownership without following its initially absent target.
Readiness now checks systemd state, fails early on a stopped/crashing service, and
includes status/journal diagnostics. All four syntax checks and 32 helper tests
passed locally, including five new health failure/diagnostic cases. These changes
do not by themselves establish that Rust can start on the Pi.

## Unity log-open failure — 2026-09-25

The supplied journal establishes the immediate startup failure: Unity reports
`Unable to open log file, exiting.` with `-logfile /dev/stdout`, exits 127, and
systemd subsequently hits its start limit. Changed the unit to the documented
Unity console form `-logFile -` and made explicit deployment retries reset failed
state before start/restart. Automatic restart limits are unchanged. Added a helper
test covering start and restart after failure. The Pi must rerun to establish
whether startup now completes or encounters a later runtime issue.

Reference: [Unity Player command-line arguments](https://docs.unity3d.com/6000.0/Documentation/Manual/PlayerCommandLineArguments.html).

## Live acceptance and Rust+ correction — 2026-09-25

User-provided recaps establish successful convergence followed by an unchanged
run (`ok=49 changed=0 failed=0`). The user joined successfully and confirmed startup
after reboot. The reboot journal reports `Server startup complete`, bootstrap in
216.30 seconds, Steam connected, and a player spawned. This verifies those core
PoC behaviors, not sustained performance, world-state persistence or NAS recovery.

The same journal shows an unwanted Rust+ connectivity test. Replaced `+app.port 0`
with Facepunch's documented disable syntax `+app.port 1-`. This remains a pending
on-Pi verification until the changed service is deployed. Early Steam interface
and 251 ms IPC warnings preceded successful initialization; no unsupported Steam
workaround or log suppression was introduced.

## Optional Rust+ on WAN2 — 2026-09-25

The user verified outbound IPv4 through the selected public WAN after a temporary NetworkManager
DNS change. Added `rust_plus_enabled` (default false), port 28083, optional UFW
management and a local TCP-listener check. Both enabled and disabled service units
were rendered through Ansible in check mode and their companion arguments verified.
Deployment syntax and production lint passed. Router forwarding, live companion
registration and phone pairing remain to be verified on the user's network.

## Public-host security baseline — 2026-09-25

Added a security-only playbook and public inventory profile. It installs UFW,
allows SSH only from the two management LANs, exposes the configured game/query
and companion ports, denies other unsolicited incoming and routed traffic, keeps
outbound access, and validates SSH configuration before disabling root and empty
password login. Ordinary user password login is preserved pending key verification.

Validated in an isolated Debian 13 Docker container with its own network namespace
and NET_ADMIN capability (not host networking). The actual security playbook
completed first run with `ok=22 changed=9 failed=0`; the repeat run returned
`ok=21 changed=0 failed=0`. UFW reported active, incoming/routed deny and outgoing
allow, with LAN-only SSH, UDP 28015/28017 and TCP 28083. Effective sshd configuration
reported root login disabled, empty passwords disabled, normal password login
unchanged. No Pi settings were changed by this local test. Runtime packet flow and
new SSH access on the Pi must still be verified after deployment.

All five playbooks passed syntax checks, production lint passed and 38 helper tests
passed, including allowed/disallowed management peers and mismatched SSH ports.

## Public inventory and account update

Validated syntax for all six playbooks and 40 helper tests, including private
inventory migration preservation and refusal to overwrite operator changes.
An isolated Debian 13 container exercised the actual automation account role:
first run changed five tasks, second run changed zero, key-based SSH and
`sudo -n id -u` succeeded, and effective SSH configuration required public-key
authentication with password/keyboard-interactive authentication disabled.
The storage account tasks adopted UID 2001/GID 2011 from simulated persistent
ownership, converged with zero changes on repeat, and rejected a requested
conflicting GID while preserving ownership. These tests did not exercise ARM
emulation, mounts or a reboot, and have not deployed this update to a live Pi.

## Player access survey

Production lint and the interactive playbook syntax check passed (including its
imported convergence play). All 49 helper tests passed, including empty/invalid
list rejection, numeric profile parsing, duplicate handling, unlisted admin
rejection, revocation, preservation across unattended runs, and filesystem apply
idempotence with mocked service control. The helper stopped the mocked service
before writing and did not stop it on the unchanged second application.
The AWX survey JSON was parsed locally but has not been imported into a live AWX
instance. Actual approved/unapproved player joins remain a live Pi acceptance
check; unit tests do not establish game admission behaviour.

## Administrator survey update

All 53 helper tests passed, including admin grants, owner/moderator revocation,
ban preservation, unchanged-run idempotence, persistence across unattended runs,
and rejection of administrators outside the restricted player list. Live game
administrator authorization still requires deployment and a reconnect on the Pi.

## Local telemetry and three-display dashboards

63 helper tests passed, covering player-data projection (no Steam IDs/IPs),
fixed read-only RCON commands, HTTP Host validation, rejection of mutation and
path traversal requests, missing sensor data, stale update checks, current versus
historical undervoltage, APT index-only checks, Steam app-info-only probes and
mount mismatch refusal. Backup helpers continue to pass their existing tests.

Headless Chromium-based browser checks used synthetic, explicitly local test data
at 1280x720 for the touch view and 1920x1080 for both HDMI views. All three fitted
the viewport without document overflow; no JavaScript errors occurred. A player
name containing HTML rendered as text, and the missing/stale telemetry state was
checked. Synthetic data is not shipped in the dashboard or live collector.

These checks do not validate live Raspberry Pi sensor permissions, installed-game
RCON response schemas, graphical compositor placement, display overhead or a full
Ansible deployment on the Pi. The dashboard HTTP health check alone does not prove
game readiness. Live acceptance is documented in telemetry/README.md.

## Telemetry feature archive and kiosk session hardening

140 helper tests passed, covering the existing telemetry, dashboard, backup and
watchdog helpers plus the new feature archive and camera presence detector. The
archive tests cover sampling, rotation against the configured age and size caps,
refusal to retain anything beyond the recorded numeric features, and recovery from
a truncated file. The presence tests cover the idle timer, wake on movement, the
warmup window reporting motion while the sensor settles, unknown presence keeping
the screens on, probing display power with the wake command rather than the blank
command, self-disabling when no supported power command exists, and restoring
power on exit and on termination. The launcher tests confirm all three Chromium
windows are opened with the same options, including the basic password store.

`ansible-lint` reported no failures and no warnings across 119 files on the
production profile, and the shell launcher passed `sh -n` and `shellcheck`.

These checks do not validate any of this on the Pi. Camera availability and
permissions, `python3-picamera2` and `wlopm` package availability on the installed
OS, the compositor's response to display power commands, real-room motion
sensitivity, the absence of a keyring prompt after an actual reboot, feature
archive growth against the retention caps, and converge idempotence are all
unverified until a hardware run. Motion thresholds are a starting estimate and are
expected to need tuning in the room.
