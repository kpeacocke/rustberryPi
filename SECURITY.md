# Security

Keep the repository private if you add host addresses or operational details.
Never commit SSH keys, NAS credentials or RCON passwords. Use Ansible Vault for
secrets. RCON is bound to loopback and has no password configured in v1; use SSH
for administration. Public deployment should use `inventory/public-server.example.yml`
as described in the README. The security role installs UFW with incoming/routed deny
and outbound allow, restricts SSH to the two management LANs, and disables direct
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
