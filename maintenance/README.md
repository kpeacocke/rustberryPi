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
| `playbooks/hailo-candidate.yml` | Check common stable Hailo-8 releases and build a private candidate only |
| `playbooks/maintenance-hailo.yml` | Back up, activate and hardware-verify one explicitly selected local Hailo candidate |
| `playbooks/maintenance-all.yml` | OS, then FEX, then Rust in one maintenance window |
| `playbooks/maintenance.yml` | Select `rust_maintenance_target`: `os`, `fex`, `rust`, `os-rust`, `hailo`, or `all` |

`os-rust` upgrades Debian packages, reboots, then checks and updates Rust in one
maintenance window, preserving the deployed FEX version.

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
8. Check 4 KB pages, start Rust, wait for A2S readiness, restart telemetry, and
   wait for fresh authenticated RCON telemetry. Then clear the historical
   automatic restart counter, report success and restore previously running
   timers. The counter is cleared only after both health checks pass; failed
   maintenance retains it. This does not restart Rust or erase its journal. A
   subsequent automatic restart raises the dashboard notice again.
9. After releasing the maintenance window, run the dashboard maintenance checks
   immediately rather than waiting for the scheduled timer. Verify fresh package,
   Rust, upstream FEX and installed FEX results. A probe failure fails the job
   explicitly but does not stop the recovered game or reapply the offline guard.

## Hailo source candidates

The candidate job checks the stable 4.x release tags published by both HailoRT
and the PCIe-driver repositories, selects their newest common tag, and builds
the runtime/CLI, Python bindings, and PCIe module for the exact running kernel.
It downloads only the matching Hailo-8 firmware from Hailo's official URL. The
driver/runtime sources are fetched from their upstream tags. Candidates and
firmware stay under `/var/lib/rustberrypi-hailo` on the Pi; the job does not
install them, copy them to the controller, or upload them as AWX artifacts.

Hailo firmware has separate terms. Before enabling the job, accept Hailo's
firmware terms yourself and set the private AWX **host** variable
`rust_maintenance_hailo_firmware_eula_accepted: true`. Its repository default is `false`;
the playbook fails closed until that private approval is present. Do not set
this value in a survey, shared group variables or public files.

Create an AWX job template for `playbooks/hailo-candidate.yml` and schedule it
daily or weekly at a quiet time. It builds only when a newer common 4.x tag is
available; it never changes the active driver. To install, launch
`playbooks/maintenance-hailo.yml` (or select `hailo` in the maintenance survey)
and enter the exact candidate version. That job requires the same mounted NAS
backup and running Rust server as other maintenance. It stops the HAT worker,
checks candidate hashes and kernel match, installs the driver through DKMS,
switches the versioned runtime, and tests `hailortcli fw-control identify`.
On failed hardware validation it attempts to restore the previous firmware,
runtime pointer and DKMS module, then continues normal game/RCON recovery. A
rollback failure leaves the normal persistent maintenance guard in place.

The Hailo-8 4.x source line is selected intentionally; global 5.x releases target
other products. A successful v4.24 candidate build does not prove it fixes a
specific NNC firmware timeout. The installed device still requires successful
`fw-control identify` and a real HAT inference test before enabling
`rust_telemetry_hat_enabled`.

The installed FEX version is queried from `FEXGetConfig` beside the selected
runtime, not inferred from a configured version label. This local check is
independent of the upstream release API, so connectivity failures do not hide
the installed version. Refreshing telemetry alone does not rerun update checks.
Telemetry-only deployments discover the FEX executable from the deployed
`rust.service`, rather than defaulting to `/usr/bin/FEX`. An older system-wide
FEX can remain installed alongside the versioned runtime without implying that
the game was downgraded. The telemetry configuration records the selected
binary's reported version, not the repository's desired release label.

After both readiness checks pass, the workflow records the verified target and
Pi completion timestamp for advisory performance comparison. The collector waits
for the maintenance marker to be released, then gathers eligible post-maintenance
samples. A comparison needs matching player/entity workload history; missing
history is explicitly unmatched, never an assumed regression or improvement.
See [server analysis](../telemetry/README.md#server-early-warning-and-hailo).

These are explicit maintenance actions, not zero-change convergence runs. They
warn, back up and restart even if the selected version is already installed;
the Rust helper avoids downloading an unchanged game build. No map wipe, access
policy change or firewall change is part of these jobs. Updates can still change
game compatibility; backup data does not make downgrading a game build safe.

## Update, feature and blueprint policy

Target the latest stable public Rust build, with vanilla gameplay defaults.
Preserve blueprint progress wherever the upstream version supports it. There are
no scheduled discretionary map or blueprint wipes.

- Routine updates preserve the existing world, player databases, server identity
  and configuration. Use the Rust-only maintenance workflow for its verified
  pre-update backup and post-update readiness checks.
- New features use the installed build's defaults unless a gameplay exception is
  deliberately approved. Do not accumulate speculative or obsolete convar
  overrides from older releases.
- Features that depend on new map generation may require a new world. An optional
  map-only wipe to obtain them requires explicit operator approval and a verified
  backup; existing bases and terrain will be lost. The 1500-size map may still
  omit some monuments.
- Treat map wipes and blueprint wipes separately. Preserve blueprint databases
  during a map-only wipe; never delete the entire server-identity directory as
  a shortcut, since it also contains player data and configuration.
- Mandatory upstream map wipes still apply. Blueprint progress is retained where
  compatible, but an upstream blueprint reset or database incompatibility may
  prevent preservation. A backup cannot guarantee compatibility with a new build.

This is an operator policy, not a new automation feature. These playbooks do not
provide a map-only wipe workflow or guarantee preservation across incompatible
upstream releases. Do not wipe maps or blueprints to troubleshoot connectivity.

Only in-game notifications are implemented here. Offline players will not see
them. Completion/failure is reported through the Ansible/AWX job; attach external
AWX notification templates if desired. No email, Discord or other credentials are
stored in this repository, and no external messages are sent by these playbooks.

## Run one or all

### Optional managed SMB backup storage

Create a dedicated NAS share and non-admin account with write access only to that
share. On the Pi, create a credentials file outside the repository:

```sh
sudo install -d -o root -g root -m 0700 /etc/rustberrypi-nas
sudo touch /etc/rustberrypi-nas/credentials
sudo chown root:root /etc/rustberrypi-nas/credentials
sudo chmod 0600 /etc/rustberrypi-nas/credentials
sudo nano /etc/rustberrypi-nas/credentials
```

Its contents are `username=YOUR_BACKUP_ACCOUNT` and `password=YOUR_PASSWORD`, on
separate lines without quotes. Never paste these credentials into chat, command
arguments or tracked files. Recreate this file after reimaging the OS, or provision
it privately through Ansible Vault/AWX; this role deliberately does not read it
back into controller output.

In ignored private host variables, configure:

```yaml
rust_nas_enabled: true
rust_nas_server: nas.example.invalid
rust_nas_share: rust-backups
rust_backup_mount: /mnt/nas
rust_backup_destination: /mnt/nas/rust-backups
```

Run `playbooks/nas.yml` using your normal inventory/connection options to install
SMB support, mount the share persistently and check write/read access. This does
not stop Rust or take a backup. The same optional role runs before future
maintenance and during normal convergence. It refuses unrelated existing mounts,
nonempty underlying directories and credentials not owned by root with mode 0600.
The mount uses SMB3, root-only file permissions, no execution/device/setuid support,
and systemd automount with nofail so an offline NAS does not prevent OS boot.
Maintenance still refuses to proceed if the NAS is unavailable.

The share itself is mounted at `/mnt/nas`; archives go into its `rust-backups`
subdirectory. If the NAS firewall is enabled, allow SMB TCP 445 from the Pi.
Changing `rust_nas_enabled` to false stops management; it does not remove the
existing mount/fstab entry or backups.

### Launch the selected job

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
are created automatically by this repository. See
[adding new things to AWX](../awx/README.md) for the AWX-side steps a new
playbook, variable or survey question needs.

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

## Refresh boards after successful maintenance

The workflow now runs `rust-maintenance-check.service` after releasing its
maintenance window and restoring timers/collector. It waits for fresh successful
package, Rust and FEX probe evidence before the AWX job finishes. This step sits
outside the game recovery block: a probe failure does not stop or roll back a
healthy game. Real outstanding updates remain visible. Retry only
`playbooks/refresh-maintenance.yml` after fixing the probe, rather than repeating
the maintenance operation. Gameplay and new-world jobs use the same refresh.
