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
