# Recovery validation

Automated policy, protocol and backup-recovery tests run in the existing CI matrix.
They are not proof of Pi runtime recovery or a fix for the observed FEX/Steam fault.

Use a spare/test world for the following hardware acceptance checks. A suspended
process cannot save; forced recovery may lose progress since its last completed
save. Take and verify an off-host backup before the test.

1. Converge with normal private settings and telemetry enabled. Confirm A2S/RCON,
   player joins, timer enablement and an unchanged second-run convergence.
2. Run `systemctl start rust-backup`. Confirm an archive and checksum, successful
   post-backup game recovery, and separate `backup_success`/`recovery_success`
   evidence. Test failed recovery on the test system: archive evidence must remain
   and the backup unit must fail.
3. With the game healthy, run
   `sudo systemctl kill --kill-whom=all --signal=SIGSTOP rust.service`.
   This reproduces a live process with open ports that cannot answer probes.
   Confirm three failed probes cause diagnostics and restart. Stopping a suspended
   game can consume the configured 180-second stop timeout before systemd kills it.
   Verify readiness and join using the same world after recovery.
4. Repeat only on the test system until the third requested recovery exhausts the
   budget. Confirm no further restart across timer ticks and a host reboot, and an
   urgent dashboard warning. Manually restart/repair the game, then use
   `watchdog.py --reset`; resetting an unhealthy game must fail.
5. Stop Rust deliberately. Confirm it stays stopped. During a backup and a genuine
   maintenance/restore window, confirm the watchdog takes no lifecycle action.
6. Simulate missing/wrong USB only on a spare system. Recovery must refuse it.
   Inspect diagnostic files and job logs for accidental RCON credential exposure.

Record observed timings, results and installed FEX/game versions here after the
hardware checks. No live suspension, backup or reboot was performed while authoring
this change. Bounded recovery improves availability; it does not establish native
x86-level reliability for this emulated Rust deployment.
