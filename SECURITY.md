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
