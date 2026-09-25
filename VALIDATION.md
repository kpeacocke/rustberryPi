# Validation record

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
