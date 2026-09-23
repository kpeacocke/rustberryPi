# Security

Keep the repository private if you add host addresses or operational details.
Never commit SSH keys, NAS credentials or RCON passwords. Use Ansible Vault for
secrets. RCON is bound to loopback and has no password configured in v1; use SSH
for administration. The game and query UDP ports are the only intended inbound
game ports. Rust+ is disabled.

Report vulnerabilities privately to the repository owner. Do not attach secrets
or player databases to public issues. Treat restore archives as trusted operator
input even though paths, file types and checksums are validated.

No bundled FEX or game binaries are redistributed. The compressed RootFS manifest
contains paths and hashes only. The Steam bootstrap URL is mutable, so its pinned
checksum intentionally fails when Valve replaces it; review and update the hash.
