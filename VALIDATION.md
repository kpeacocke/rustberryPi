# Validation record

Repository built on 2026-09-23. Checks ran in Ubuntu under WSL, not on the Pi.

| Check | Result |
| --- | --- |
| Resolve FEX abbreviated revision through GitHub | Full SHA verified; source checkout matches |
| Download dated RootFS and SteamCMD bootstrap | SHA256 digests recorded in role defaults |
| Extract pinned RootFS and generate manifest | 39,527 path entries; compressed manifest included |
| Verify manifest against complete reference extraction | Passed |
| Inventory resolves pidesktop and group variables | Passed |
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
