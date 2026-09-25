# Maintenance jobs and workflows

These jobs maintain an **already deployed, healthy** RustberryPi server. They do
not bootstrap a Pi. Deploy current `rust.yml`, including telemetry and backup
helpers, before using them. A mounted, separate NAS filesystem and working local
RCON are required. A missing NAS mount fails before the countdown. There is no
skip-backup option.

| Entry point | Work selected |
| --- | --- |
| `playbooks/maintenance-rust.yml` | Check Steam and update Rust if a newer public build exists |
| `playbooks/maintenance-os.yml` | Upgrade packages within the configured Debian repositories; reboot by default |
| `playbooks/maintenance-fex.yml` | Build/adopt and activate an explicitly pinned FEX commit |
| `playbooks/maintenance-all.yml` | OS, then FEX, then Rust in one maintenance window |
| `playbooks/maintenance.yml` | Select `rust_maintenance_target`: `os`, `fex`, `rust`, or `all` |

The three component task files live under `roles/rust_maintenance/tasks/`.
All entry points use the same outer workflow. **Do not chain the three standalone
playbooks to implement “all”: that would cause three countdowns and restarts.**

## What happens

1. Validate selection, deployed runtime, storage UUID, NAS mount and running game.
2. Acquire an atomic maintenance marker; reject overlapping or interrupted jobs.
3. Pause backup/update-check timers and telemetry, wait for existing background
   operations, and give RCON a quiet minute before taking its connection.
4. Query player count and send in-game announcements. Default warning: ten minutes,
   with reminders at five minutes, one minute, ten seconds and shutdown. Shorter
   countdowns omit reminders outside their range. Zero still announces shutdown.
   A failed RCON acknowledgement aborts before taking Rust offline.
5. Request `server.save`, stop Rust gracefully, and require successful shutdown.
6. Create a fresh offline NAS archive, independently compare its SHA256, read all
   members and validate paths/types plus deployment metadata. This verifies archive
   integrity, **not** a restore rehearsal or a bootable OS backup.
7. Run only the selected component(s). “All” runs OS → FEX → Rust.
8. Check 4 KB pages, start Rust, wait for A2S readiness and fresh authenticated RCON
   telemetry, then report success and restore previously running timers.

These are explicit maintenance actions, not zero-change convergence runs. They
warn, back up and restart even if the selected version is already installed;
the Rust helper avoids downloading an unchanged game build. No map wipe, access
policy change or firewall change is part of these jobs. Updates can still change
game compatibility; backup data does not make downgrading a game build safe.

Only in-game notifications are implemented here. Offline players will not see
them. Completion/failure is reported through the Ansible/AWX job; attach external
AWX notification templates if desired. No email, Discord or other credentials are
stored in this repository, and no external messages are sent by these playbooks.

## Run one or all

From an SSH controller with the existing private inventory and become credentials:

```sh
ansible-playbook playbooks/maintenance-rust.yml --ask-become-pass
ansible-playbook playbooks/maintenance-os.yml --ask-become-pass
ansible-playbook playbooks/maintenance-fex.yml --ask-become-pass
ansible-playbook playbooks/maintenance-all.yml --ask-become-pass
```

Run **one** command for the desired operation; the list is not a sequence to paste.
Or use the selectable workflow:

```sh
ansible-playbook playbooks/maintenance.yml --ask-become-pass \
  -e rust_maintenance_target=rust -e rust_maintenance_warning_seconds=600
```

Limit the target as usual. Private inventory host vars are loaded automatically;
keep any existing explicit `-e @...` overrides your deployment needs. Extra vars
have highest precedence, including over the standalone wrapper's selection.

Rust and FEX jobs can run on the Pi with `--connection local` and
`-e ansible_python_interpreter=/usr/bin/python3`. An OS job that reboots **must run
from another SSH controller or AWX**; the play rejects a local reboot because it
would kill its own controller. A local OS job can explicitly set
`rust_maintenance_os_reboot=false`; it reports the reboot as deferred, not done.
The SSH account must be permitted by the Pi's SSH firewall rules.

FEX (including “all”) requires private values:

```yaml
rust_maintenance_fex_commit: '<reviewed 40-character commit SHA>'
rust_maintenance_fex_version: '<matching FEX version label>'
```

Also persist that desired commit/version as `fex_commit` and `fex_version` in the
normal private host variables, so later convergence does not revert the selection.
Maintenance retains the deployed RootFS location; upgrading the RootFS is a
separate planned migration. Native FEX compilation can take considerable time on
the Pi and happens inside this downtime window. Existing versions are retained.

## AWX

Use the repository's normal execution-environment dependencies and an SSH machine
credential with privilege escalation. Do not use connection-local inventory in
AWX. Keep operational host variables and FEX pins in private inventory, not Git.

Create job templates:

| Template | Playbook |
| --- | --- |
| RustberryPi – Rust maintenance | `playbooks/maintenance-rust.yml` |
| RustberryPi – OS maintenance | `playbooks/maintenance-os.yml` |
| RustberryPi – FEX maintenance | `playbooks/maintenance-fex.yml` |
| RustberryPi – All maintenance | `playbooks/maintenance-all.yml` |
| RustberryPi – Select maintenance | `playbooks/maintenance.yml` |

Disable concurrent jobs and job slicing; target one Pi per maintenance run. Give
the job ample timeout for FEX compilation, backup, updates and boot (for example,
four hours initially; measure actual build time). Attach the survey in
`awx/maintenance-survey.json` to the selectable template. FEX fields are optional
in the survey UI because Rust/OS do not need them; the play enforces them for
FEX/all before downtime. The selectable template already orchestrates one/all.

If AWX Workflow Job Templates are wanted for approvals/notifications, create a
workflow with a single selectable-maintenance job node and prompt that node for
variables. Put the same survey on the workflow and pass its variables through.
Use success/failure edges only for reporting, not an unconditional restart or
restore. Never run OS, FEX and Rust nodes in parallel on the same Pi. No AWX objects
are created automatically by this repository.

## Failure and interrupted-job recovery

`/var/lib/rustberrypi-maintenance` serializes these maintenance workflows. Once
offline work starts, its `offline` file is also a persistent systemd start guard
on Rust, the backup service and the maintenance-check service. It survives a
reboot/controller loss. Timers may run again, but guarded services cannot start
until the offline marker is removed. Do not run normal convergence, updates or
manual restores concurrently: their per-operation locks do not cover this entire
multi-job window.

Failure before offline work releases the window and leaves the game running.
Failure during backup/update/health verification deliberately leaves Rust stopped
and retains the marker. There is no automatic rollback, retry loop, data restore
or removal of partial installations. Controller loss/cancellation may prevent
cleanup; inspect the Pi rather than immediately rerunning a job.

1. Confirm the old AWX/controller job and its remote async commands have finished.
   Do not remove a marker owned by a live operation.
2. Inspect the failed task, package-manager state and game installation. Protect
   raw Rust logs/process listings: they can contain the RCON password. Preserve
   the new NAS archive. Diagnose/repair the selected component while Rust is off.
3. For a failed FEX activation, the prior unit (if changed) is saved at
   `/var/lib/rustberrypi-maintenance/rust.service.before-fex`. Review it before
   restoring it; restore consistent private pins, telemetry configuration and
   deployment metadata as well. Do not blindly downgrade Rust or restore a world.
4. After repair, remove **only** the offline marker, reload systemd and start Rust:

   ```sh
   sudo rm /var/lib/rustberrypi-maintenance/offline
   sudo systemctl daemon-reload
   sudo systemctl start rust
   sudo systemctl start rust-telemetry
   ```

5. Verify the game query and fresh RCON dashboard samples; join and check the
   world. If unhealthy, restore the offline marker and stop Rust while investigating.
   Once healthy, archive the maintenance directory under a dated name outside its
   original location, then resume the timers you normally use. This releases the
   maintenance marker for subsequent jobs without discarding failure evidence.

A normal OS or Rust job does not update FEX; a FEX job does not request a Rust app
update. None changes Debian repositories or performs a Debian release upgrade.
OS jobs with reboot enabled reboot once even when no reboot-required marker exists,
because Debian does not reliably provide that marker for every relevant update.

## Validation limits

Automated tests cover countdown selection, fixed RCON commands, failure-before-save,
archive checksums and unsafe/missing archive contents. Syntax/lint checks cover all
entry points. Hardware reboot/reconnect, a live NAS backup and the full maintenance
window still require validation on the Pi; these jobs have not been run there by
the repository authoring session.
