# KP's Pi 5 Rust server

Ansible for one specific experiment: a Raspberry Pi 5, 16 GB RAM, Debian 13
Trixie arm64, 4 KB Raspberry Pi kernel, FEX and a vanilla four-player Rust server.
The OS lives on microSD; the ext4 USB labelled `RUSTSERVER` holds `/srv/rust`.
No plugins, containers, game-server framework or scheduled game updates.

**This repository is implemented and statically validated, not a claim that Rust
has been proved playable under FEX on this Pi.** The existing host proved x86-64
Ubuntu execution. SteamCMD, Rust startup, performance, reboot recovery and live
idempotence still require the hardware acceptance procedure below.

## Established host and pins

| Component | Desired state |
| --- | --- |
| Host | `PiDesktop.local`, SSH user `kpeacocke` (edit inventory if needed) |
| Kernel | `/boot/firmware/kernel8.img`; require `getconf PAGESIZE` = `4096` |
| Observed kernel | `6.18.50+rpt-rpi-v8`; ABI version is not frozen |
| FEX | `e2f973fe931e6dc2ce523795e51ca1ac3ca85816` / `FEX-2609-120-ge2f973fe9` |
| RootFS | FEX Ubuntu 24.04, image dated 2026-08-11; Ubuntu 24.04.4 userspace |
| Steam app | `258550`, public branch, anonymous login |
| World | identity `kp-pi5`, procedural size `1500`, seed `12345`, four players |
| Network | UDP 28015 game, UDP 28017 query; RCON loopback TCP 28016; Rust+ disabled |

RootFS SHA256:
`2854b06d3ff1b8f6e526135bfb6dd5b7b30ab3ab73e79ae933a3d9fed959a178`.
SteamCMD bootstrap SHA256:
`cebf0046bfd08cf45da6bc094ae47aa39ebf4155e5ede41373b579b8f1071e7c`.
Both were computed from downloaded upstream bytes during repository creation.
The Steam bootstrap updates itself when invoked; the Rust build is recorded by
Steam's app manifest. Neither is falsely presented as an immutable game release.

## Controller setup

Use Linux or WSL with Python 3.12+, Git and SSH. Keep the checkout in the WSL Linux
filesystem (`~/src/pi5-rust-ansible`), not a world-writable Windows mount. Native
Windows is not an Ansible controller. Commands below run from the repository root.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
ansible-galaxy collection install -r requirements.yml
ansible-inventory --graph
ssh kpeacocke@PiDesktop.local
```

Establish and verify the SSH host key interactively first. Configure sudo on the
Pi, or add `--ask-become-pass` to playbook commands. SSH passwords, if needed, use
`--ask-pass` and the controller's SSH password support. The fresh OS needs SSH,
Python 3 and sudo, and Raspberry Pi firmware/kernel packages supplying
`/boot/firmware/kernel8.img`. It must be the equivalent Pi-supported Debian image,
not a generic ARM image with a different boot layout.

Edit `inventory/hosts.yml` and `inventory/group_vars/rust_servers.yml`. Local
overrides can go in ignored `inventory/host_vars/pidesktop.yml`. Discover the
USB UUID with `lsblk -f` and set `rust_storage_uuid` for stronger identification.
No network address or NAS credentials beyond the conversation's host example are
assumed. Inventory settings apply to both existing and fresh hosts.

## Converge the current Pi

Read-only preflight:

```sh
ansible pidesktop -m ansible.builtin.ping
ansible pidesktop -b -m ansible.builtin.command -a 'getconf PAGESIZE'
ansible pidesktop -b -m ansible.builtin.command -a 'lsblk -f'
ansible pidesktop -b -m ansible.builtin.command -a '/usr/bin/FEXGetConfig --version'
ansible-playbook playbooks/rust.yml --syntax-check
ansible-playbook playbooks/rust.yml --limit pidesktop --diff
ansible-playbook playbooks/rust.yml --limit pidesktop --diff
```

Expect `changed=0` on the second run while settings, packages and runtime state
remain unchanged. An expired apt metadata cache can refresh without changing the
installed package set; no package upgrades are requested. Actual first/second-run
results must be recorded on the Pi. Full `--check` is deliberately rejected because
it cannot simulate the filesystem mount, reboot or emulated runtime. Syntax checks
and helper tests are safe offline checks. Do not use `--start-at-task` to bypass
storage validation.

The roles execute in this order: prerequisites → kernel → USB → FEX/RootFS →
SteamCMD → Rust/service/firewall → backup hooks → A2S game readiness. A smaller
`playbooks/bootstrap.yml` stops after storage preparation.

### Adoption and replacement rules

- A single unconditional `kernel=kernel8.img` is left as-is. Other active kernel
  directives are replaced with one final `[all]` directive, with a one-time
  `config.txt.pre-ansible` backup. Other settings and `config.txt.pre-fex` survive.
  Included configuration files cause a reviewable failure rather than guessing
  their precedence. Only a non-4096 running kernel triggers reboot; set
  `rust_allow_reboot: false` to configure then stop for a manual reboot.
- The labelled/UUID filesystem must be unique and ext4. A wrong existing mount,
  mounted source elsewhere, duplicate fstab entries or files hidden underneath an
  unmounted `/srv/rust` cause a failure. The existing corrected fstab mount is
  normalized to one UUID entry. The mount is required at boot; no `nofail` is used.
- `/usr/bin/FEXGetConfig --version` verifies manual FEX (`FEX --version` is not
  supported). Matching `/usr` installation is adopted without a build. A different
  version is preserved and the exact source/submodules are built with Clang, Qt5
  and QtQml into `/opt/fex/<commit>`. Service configuration selects that path.
- The RootFS manifest checks every expected directory, symlink and file digest.
  A matching manual extraction at
  `/home/kpeacocke/.fex-emu/RootFS/Ubuntu_24_04` is copied into the managed versioned
  location, preserving the original. Set `fex_existing_rootfs` if its actual name
  differs. This avoids granting the service access through a private home directory.
  Otherwise the checksum-pinned image is extracted to staging and verified before
  activation. A corrupt existing managed tree fails for review without deletion;
  select a new versioned `fex_rootfs_path` to repair it while retaining the old tree.
  Extra manual files are retained during adoption; expected files must match.
- `/tmp/FEX` remains unless `fex_cleanup_tmp: true`. Even then cleanup happens only
  after version, RootFS contents and x86 execution are verified as `rust`.
- Existing game installations are adopted only with a complete Steam app manifest
  and a `RustDedicated` executable. Incomplete installs are repaired by SteamCMD.
  SteamCMD writes game binaries; it does not delete the persistent `data` directory.

The adopted RootFS is copied once; leave enough microSD room for the managed copy
and a source build. A new RootFS needs approximately 2 GB plus its 501 MB archive.
The 32 GB USB is tight: upstream presently recommends at least 15 GB for Rust;
updates need extra room. The updater requires 5 GiB free before starting but cannot
guarantee a future depot will fit. Monitor `df -h /srv/rust`; replacing the USB with
a larger SSD is a separate deliberate migration, not an automatic formatting action.

### Storage initialization (new blank USB only)

Normal rebuilds **never format**. For a newly partitioned USB only, inspect
`lsblk -f` and use a stable `/dev/disk/by-id/...-part1` path:

```sh
ansible-playbook playbooks/bootstrap.yml --limit pidesktop \
  -e rust_storage_initialise=true \
  -e rust_storage_device=/dev/disk/by-id/REPLACE_WITH_USB_ID-part1
```

The helper rejects whole disks, mounted devices, disks with any mounted partition,
non-USB transport, existing filesystem/signature data and a configured UUID during
initialization. It does not repartition, wipe signatures or force mkfs. Once a
matching labelled filesystem exists, even this opt-in command adopts it. Return
the flag to false and record its UUID. Erasing a previously used USB is intentionally
outside v1.

## Persistent layout and microSD rebuild

```text
/srv/rust/                   # verified USB mount, stable rust UID 2001
  home/                     # Steam/FEX user caches and Steam client library link
  steamcmd/                 # rebuildable SteamCMD installation
  server/                   # rebuildable Rust depot and Steam app manifest
    server -> /srv/rust/data
  data/
    deployment.json         # identity, world parameters, UID and emulator pins
    kp-pi5/                 # worlds, players, cfg, bans, owners, etc.
```

The service requires the mount and verifies its UUID before every start. The
account's numeric UID stays stable across reinstalls. All Rust identities live
under the `server/server` symlink, so world/player state is outside the depot.
No recursive ownership reset or deletion of existing state occurs on convergence.
An account/UID conflict fails instead of silently moving ownership.

To reimage: take a NAS backup, shut down, disconnect the USB while imaging the
microSD, install the equivalent Debian image, restore SSH/sudo access, reattach
the USB, confirm its UUID, then run `playbooks/rust.yml`. Keep identity, seed,
worldsize and UID unchanged. No NAS restore is needed if the USB survived: its
state is already in place. A mismatch with `deployment.json` blocks a silent world
change. `rust_allow_world_change: true` is reserved for intentional changes after
backup; it does not migrate existing ownership if you change the UID.

If adopting some later manual installation with a real `server/server` directory,
the playbook stops before hiding it. Stop Rust, back up that entire directory,
compare it with `data`, and migrate its identity directories into `data` manually.
Rename the old directory to a preserved sibling, then rerun so Ansible can create
the symlink. Never overlay two different worlds by guessing which wins.

## Updates, service and network

```sh
ansible-playbook playbooks/rust.yml --limit pidesktop -e rust_update=true
ssh kpeacocke@PiDesktop.local 'sudo systemctl status rust'
ssh kpeacocke@PiDesktop.local 'sudo journalctl -u rust -n 100 --no-pager'
```

An explicit update fetches Steam's public build ID. If it matches, the server is
not stopped. Otherwise it takes the maintenance lock, stops cleanly with SIGINT,
updates/validates the depot, requires Steam's success response plus complete local
manifest, and lets Ansible start the configured service. A failed update leaves
Rust stopped for inspection; rerun after fixing disk/network problems. No automatic
retry restarts a partially updated installation. External checks can change Steam's
caches without being reported as a deployment change. Steam may advance again
between the check and download; the installed build ID is the authoritative result.
Rust forced wipes and protocol changes are upstream behavior; backups do not make
old saves compatible with every new release.

The systemd unit restarts on failure with rate limits. Configuration changes trigger
restart; ordinary convergence does not. First map generation under emulation may
be slow: readiness waits up to 30 minutes for a real A2S_INFO UDP response, including
Steam's challenge exchange. `systemctl active` alone is not considered readiness.
The check fails early if the service stops or repeatedly restarts and includes
service status and the last 100 journal lines in its error. Override
`rust_health_timeout` only when logs show legitimate slow startup; a longer timeout
does not repair a crashed server.
Join from a client with `connect PI_ADDRESS:28015`. Confirm four-player capacity,
save/restart behavior, CPU temperature, memory and playability before relying on it.

Default firewall management is off to preserve the Pi's other jobs. Permit UDP
28015 and 28017 in the existing host/LAN firewall. If explicitly enabling
`rust_manage_firewall`, this project installs UFW, allows the configured SSH port,
allows the two UDP ports from `rust_client_cidr` and enables incoming deny policy.
Review existing UFW/nftables rules and other services before enabling it. Rules
already present are preserved, so inspect actual exposure. Router forwarding and
Internet access are not configured. RCON is loopback-only and Rust+ is disabled;
do not forward TCP 28016.

## NAS backup and restore

Mount a NAS share separately at `/mnt/nas` using your existing NFS/SMB setup.
Credentials and share provisioning are deliberately outside this repository.
Set `rust_backup_mount` and `rust_backup_destination` to whitespace-free absolute
paths. The helper requires a real, separate mount and a destination beneath it;
it refuses to quietly write backups to microSD when the NAS is absent.

```sh
ansible-playbook playbooks/backup.yml --limit pidesktop
```

The one-shot service locks against updates/restores, stops Rust gracefully, verifies
clean shutdown, archives the **entire data directory**, writes a SHA256 sidecar,
and restarts only if previously running. Partial archives use a temporary suffix.
The service account's caches and game binaries are not backed up. There is no
automatic retention deletion; configure NAS snapshots/retention separately.
Set `rust_backup_schedule: '*-*-* 04:00:00'` to enable the timer; empty disables it.
Inspect `journalctl -u rust-backup` and `systemctl list-timers rust-backup.timer`.
Backups introduce maintenance downtime and NAS outages fail visibly.

For disaster recovery onto a new USB, initialize it explicitly as above and deploy
without starting a new world:

```sh
ansible-playbook playbooks/rust.yml -e rust_start=false
ansible-playbook playbooks/restore.yml \
  -e rust_restore_confirm=true \
  -e rust_restore_replace=true \
  -e rust_restore_archive=/mnt/nas/rust-backups/REPLACE.tar.gz \
  -e rust_restore_sha256=REPLACE_WITH_64_HEX_DIGEST
ansible-playbook playbooks/rust.yml
```

Align group variables with `deployment.json` in the backup before preparation.
Restore requires a matching checksum; rejects absolute paths, traversal, links
and device nodes; extracts to staging on the USB; validates identity/seed/size/UID
against deployed metadata; then renames the prior data directory to a dated sibling
before activating the restored tree. `rust_restore_replace=true` is needed because
even a prepared empty deployment has metadata. Old state is preserved, not deleted.
Have free space for both trees. A failed restore leaves a previously running server
stopped for inspection. A successful restore restarts only if it was running before.
Verify the sidecar through a trusted channel; a hash is integrity, not authentication.

## Validation and acceptance

```sh
ansible-lint
python -m unittest discover -s tests -v
for p in playbooks/*.yml; do ansible-playbook "$p" --syntax-check; done
```

CI runs these checks without SSH access. Helper tests cover kernel normalization,
storage guard conditions, RootFS drift, Steam metadata, unsafe restores and the
A2S challenge exchange. They do not emulate the Pi or establish performance.

Hardware acceptance checklist:

1. Current Pi convergence completes and A2S readiness passes; join with a client.
2. Second unchanged play finishes with `changed=0`; service start timestamp stays
   unchanged. Save both recaps and the Steam build ID.
3. Save player/world changes, restart the service and reboot the Pi; confirm state.
4. Take a NAS backup, restore into a deliberate test copy/new USB, and confirm state.
5. Reimage microSD with the USB disconnected; reconnect, rerun, and verify the same
   identity/world/player state and another zero-change second run.
6. Verify missing/wrong USB blocks deployment and service start, unchanged updates
   do not restart, and failures leave data intact.

See `VALIDATION.md` for checks actually performed while building this repository.

## Pin maintenance and upstream references

Changing `fex_commit` also requires the matching `fex_version`. RootFS changes
require a dated URL, SHA256, new versioned path and regenerated compressed manifest;
the manifest must be generated from a checksum-verified image using
`tools/rootfs_manifest.py`. Never relabel arbitrary content as an existing pin.
Review SteamCMD bootstrap checksum changes rather than disabling verification.

- [FEX pinned source](https://github.com/FEX-Emu/FEX/tree/e2f973fe931e6dc2ce523795e51ca1ac3ca85816)
- [FEX RootFS image](https://rootfs.fex-emu.gg/Ubuntu_24_04/2026-08-11/Ubuntu_24_04.sqsh)
- [Facepunch server setup and ports](https://wiki.facepunch.com/rust/Creating-a-server)
- [Valve SteamCMD](https://developer.valvesoftware.com/wiki/SteamCMD)
