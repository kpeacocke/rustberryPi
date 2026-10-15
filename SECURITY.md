# Security

Keep host addresses and operational details in ignored local inventory/host vars
or a separate private inventory repository. Review staged files before publishing.
Never commit SSH keys, NAS credentials or RCON passwords. Use Ansible Vault for
secrets. RCON is bound to loopback and has no password configured in v1; use SSH
for administration. Public deployment should use `inventory/public-server.example.yml`
as described in the README. The security role installs UFW with incoming/routed deny
and outbound allow, restricts SSH to explicit management networks, and disables direct
root and empty-password SSH login. Normal user password authentication stays
unchanged until SSH keys have been verified. Public ports are UDP 28015/28017 and
optional Rust+ TCP 28083. RCON remains loopback-only. Unrelated preexisting UFW
allowances are preserved and must be reviewed in the reported effective rules.

Raspberry Pi Connect is also supported through the outbound-allow policy and
stateful replies; no inbound Connect port or router forwarding is required.
Verify an existing installation with `rpi-connect doctor` as its signed-in user
and test a new remote session after enabling the firewall. Connect account
linking is managed separately from Ansible.

Report vulnerabilities privately to the repository owner. Do not attach secrets
or player databases to public issues. Treat restore archives as trusted operator
input even though paths, file types and checksums are validated.

No bundled FEX or game binaries are redistributed. The compressed RootFS manifest
contains paths and hashes only. The Steam bootstrap URL is mutable, so its pinned
checksum intentionally fails when Valve replaces it; review and update the hash.

Optional camera presence sensing is off by default and must be enabled explicitly
with `rust_telemetry_screen_blank`. When enabled, the Pi camera runs continuously
while the desktop session is up so the displays can wake on movement. Frames are
reduced in memory to coarse brightness samples, compared with the previous frame,
and discarded. Nothing is written to disk, logged, cached, served by the dashboard
API or added to the feature archive, and the only output is a boolean for
"something changed". There is no recording and no face, person or identity
detection. Disabling the flag and converging removes the autostart entry, and no
other part of this project opens the camera.

The kiosk desktop logs in automatically, so no keyring is unlocked at boot.
Chromium is therefore launched with a basic password store and never asks the
Secret Service for a key, which removes the unlock prompt without weakening any
other credential on the host. Treat the kiosk account as unattended and do not
sign browser profiles into accounts or store passwords in them.

Server early-warning analysis is advisory only and never drives game commands,
restarts, updates, access control or watchdog decisions. Its bounded persistent
state and optional training corpus contain numeric metrics and counts, not player
identities, logs, credentials or camera data. Copying the corpus off-device is an
explicit operator action; neither worker uploads data.

Optional Hailo inference runs in a separate resource-limited service as the
unprivileged telemetry account. Its unit hides the RCON credential and player
history, limits device access to `/dev/hailo0`, and reads only the loopback numeric
`/api/health-sample` endpoint. Existing Host allowlisting and read-only HTTP
behavior apply to that endpoint too. Device permissions must use a reviewed udev
or group policy, never sudo or world-writable access.

Treat ONNX, calibration, HEF and manifests as trusted operator inputs: the compiler
and accelerator runtime process native model formats. Models remain root-owned;
the manifest pins the HEF checksum and inference must pass CPU-reference parity.
A checksum detects accidental model mixing, not a malicious trusted model
producer. Do not publish host-specific model/calibration artifacts. Missing,
invalid, stale or failing accelerator output is explicitly unavailable; it does
not replace ordinary telemetry or suppress existing safety checks.
