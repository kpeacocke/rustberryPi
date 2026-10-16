# Gameplay and deliberate new world

These jobs operate on an **existing managed, running installation**, using its
actual identity, deployment metadata, service unit and local RCON. They do not
install Rust, FEX, kernels or OS packages. A working managed telemetry/RCON setup,
Python websocket-client, existing access policy, mounted USB and a writable,
separately mounted NAS are prerequisites. Run Readiness check first.

## AWX setup

After merging and synchronising the project, create two templates:

| Template | Playbook | Survey |
| --- | --- | --- |
| RustberryPi - Gameplay | `playbooks/gameplay.yml` | `awx/gameplay-survey.json` |
| RustberryPi - New world | `playbooks/new-world.yml` | `awx/new-world-survey.json` |
| RustberryPi - Refresh boards | `playbooks/refresh-maintenance.yml` | None |

Use the existing project, inventory, Machine credential and tested execution
environment. Enable become; disable concurrent jobs and slicing; limit to exactly
one Pi; allow four hours. Attach and enable the appropriate survey, and set these
extra variables on both templates:

```yaml
rust_admin_players: []
rust_tutorial_enabled: false
```

Do not expose arbitrary extra variables at launch. Set `rust_gameplay_operation`
to `configure` on Gameplay and `new-world` on New world if your inventory already
defines that variable. These two values otherwise come from their playbooks.
No template or schedule is created by project sync. Never schedule New world or
save its confirmation in template defaults. An AWX approval node before its
single job is recommended; the exact confirmation remains mandatory. Do not run
other deployment, backup, restore or maintenance jobs concurrently. The process
holds the existing lifecycle lock and owns the maintenance directory throughout;
other workflows are not all protected by a whole-playbook lock.

Both jobs default to decay scale **0.25**, upkeep period **5760 minutes**, maximum
team size **5**, tutorial disabled and **no game administrators**. The existing
approved player roster, bans, public/restricted mode and public slot settings
remain intact. RCON stays available for management without player admin rights.
The gameplay job never changes the seed, size or removes world saves.
It performs an intentional maintenance restart even when the requested settings
already match. Repeated configuration does not duplicate convars.

Use private host variables to keep these intentions across future convergence:

```yaml
rust_admin_players: []
rust_decay_scale: 0.25
rust_upkeep_period_minutes: 5760
rust_maxteamsize: 5
rust_tutorial_enabled: false
```

The gameplay playbook owns these gameplay settings in server.cfg. Ordinary
`rust.yml` preserves those lines but does not apply changes to these four new
variables; run Gameplay when changing them. Existing access surveys must not
reintroduce administrator IDs. Survey answers override inventory for that launch.

## Launch sequence for a new world with these settings

1. Merge the reviewed PR, then synchronise the RustberryPi AWX project on `main`.
   Do not launch any existing deployment job just to make new playbooks appear.
2. Preserve the private host identity, approved player IDs, storage UUID, NAS and
   credential configuration. Set the gameplay host variables shown above. Keep
   the **current** `rust_seed` and `rust_worldsize` until the wipe succeeds.
3. Add the three Job Templates listed above using the existing project, inventory,
   Machine credential and execution environment. Enable privilege escalation;
   use Run type, one Pi limit, one slice, no simultaneous jobs and a 14400-second
   timeout. For Gameplay and New world, set `rust_admin_players: []` and
   `rust_tutorial_enabled: false` in template variables. Do not store a wipe
   confirmation there.
4. In each game template's Survey tab, add/enable the questions from its JSON
   file. The JSON is an API/import specification, not YAML for the Variables box.
   The UI equivalent is one question per `spec` entry, matching its answer
   variable, type, default and bounds. New world's confirmation is required text
   with no default. Refresh boards has no survey.
5. Run the existing Readiness check. Resolve failures before continuing. Ensure
   no maintenance/deployment/restore job is running and the game is healthy.
6. Launch **New world once**. It also applies all the gameplay settings, so do not
   launch Gameplay separately for this initial wipe. Choose decay 0.25, upkeep
   5760, team size 5, warning 600 seconds, size 1500 and the reviewed seed. Type
   the identity-specific confirmation below. Preview the answers and launch.
7. Wait for the game verification and board-refresh tasks to succeed. If only
   board refresh fails, use Refresh boards, never New world. Other failures follow
   the guarded recovery procedure below.
8. Update private host `rust_seed`/`rust_worldsize` to the successfully deployed
   values before any later normal convergence. Join using the existing address;
   confirm the new world, retained blueprints and access for the approved players.
   Use Gameplay for future setting changes without a wipe.

## New world approval

The proposed size is **1500**, retaining the repository's bounded 1000–2000
range. **1433876491 is a candidate**, not a verified current map recommendation.
Map generation and monuments depend on the installed Rust build; review a map
for that build and size before approval. No animal, navigation, performance or
other speculative feature switches are added.

At launch enter the new seed and size and type the exact string, substituting
the deployed identity, for example:

```text
WIPE rustberry 1433876491 1500 KEEP BLUEPRINTS
```

There is intentionally no blueprint-reset option. The operation is a **map-only
wipe**: removes procedural `.map`, `.sav` and numbered `.sav.N` files from the
selected identity in a staged copy. Unknown save naming or a custom map fails
closed. It retains every player database (including blueprints and their WAL
sidecars), other identities, configuration and access records. It does not reset
player statistics or identities. Selecting the same seed is still a real wipe.

After successful verification, update private AWX host `rust_seed` and
`rust_worldsize` to the new values **before ordinary convergence**. The job updates
the deployed unit and metadata, not AWX inventory. Existing world-change checks
will reject stale inventory; do not bypass them with `rust_allow_world_change`.

## What the transaction proves

1. Validates input and exact confirmation before acquiring resources. Checks USB
   UUID, metadata versus unit, NAS separation, free space, regular-file-only state,
   managed procedural world, configuration conflicts and running service.
2. Pauses previously active timers/collector, rejects active background work,
   queries all requested convars on the installed build and checks current seed
   and size. Missing or ambiguous replies abort before downtime.
3. Announces the countdown and requests `server.save`. Requires a successful
   graceful SIGINT shutdown before reading or copying world data.
4. Archives the complete stopped data tree to NAS. SHA256 and a full read of all
   safe archive members must succeed before staging. Publishes a checksum sidecar.
5. Copies state to a same-filesystem staging directory, keeps ownership/modes,
   reconciles settings and administrators, and verifies every blueprint file's
   SHA256 against the original. Renames the original tree to a timestamped sibling.
6. Updates only seed/size in the existing service and the deployment metadata;
   retains all unrelated startup parameters. Starts Rust, requires A2S and RCON,
   confirms roster grants, checks effective gameplay/seed/size values and persisted
   empty administrators, requests a save and requires a nonempty save with the
   requested seed/size. A map wipe has no old save to satisfy that check.
7. Writes root-only evidence `/srv/rust/gameplay-<timestamp>.json`, including NAS
   archive/checksum, removed filenames and preserved blueprint hashes. Keeps the
   original directory and NAS archive for deliberate later cleanup.

Blueprint hashes prove unchanged staging, not database compatibility with a
future Rust update or Facepunch forced blueprint wipe. This workflow does not
update the game. Test blueprint availability with a player after the first
approved launch. Free-space checks are conservative estimates, not quotas.

## Immediate board refresh

After the gameplay process has exited and released its lock, AWX explicitly runs
`rust-maintenance-check.service` and waits for completion. Ordinary maintenance
uses the same final step, outside its rollback/failure block. The service is
restarted to ensure a fresh check even if its timer fired during cleanup. This
refreshes package, Rust and FEX availability; it does not install updates or
artificially clear real outstanding updates/warnings. Boards consume the refreshed
status on their next normal poll.

AWX verifies fresh successful timestamps for all three probes, not just the
service exit code. If this last step fails, the game remains in its successfully
verified state. **Do not rerun New world to repair a board refresh.** Run the
Refresh boards template (`playbooks/refresh-maintenance.yml`) after resolving the
reported connectivity/probe issue. This playbook refuses an open maintenance
window. It is also the AWX equivalent of manually starting the maintenance-check
service, with evidence verification added.

## Failure and interrupted-job recovery

A caught failure after downtime leaves the persistent `offline` startup guard.
If the staged tree was activated, the helper stops Rust, preserves the failed
new tree, restores the original data directory and service unit, and reloads
systemd. It does **not** automatically start the restored game. A failure before
activation leaves the original tree untouched. A failure before downtime releases
the window. Previously active timers/collector are restored; guards prevent
backup/maintenance starts and the watchdog respects the maintenance directory.

Do not blindly relaunch after an AWX timeout, process kill or power loss. Async
work may still be running. Check the job and process first. Keep all archives,
`data.pre-gameplay-*`, `data.failed-gameplay-*` and `.gameplay-*` directories.
Read `/var/lib/rustberrypi-maintenance/gameplay.json` and
`rust.service.before` privately. Abrupt interruption cannot run Python rollback.
The offline marker is briefly removed to start the unit, then re-armed as soon as
systemd reports it active. An interrupted job can still leave a running but
unverified new world (conditions gate starts, not running processes). Stop
it and restore the marker before inspecting or recovering state.

With the transaction confirmed stopped, choose either the intact original tree
named by `previous` in the journal or the checksum-validated NAS archive. If a
rename was interrupted, do not assume `/srv/rust/data` is present. Preserve any
current/failed tree; restore the selected complete tree at `/srv/rust/data`, retain
its ownership/modes, and restore `rust.service.before` to
`/etc/systemd/system/rust.service`. Never merge old and new trees. Verify the
identity/seed/size in deployment metadata match the unit and private inventory.
For `rolled-back-stopped` this restoration has already happened: inspect it rather
than repeating renames. Use the existing backup validation helper before any NAS
restore; the standard restore playbook requires metadata alignment.

After deliberate recovery approval, reload systemd, remove only the `offline`
marker, start Rust, run `health.py 1800 --rcon`, confirm the old/new selected world,
blueprints and roster, then remove the maintenance directory and restore only
the recorded previously active timers/collector. Do not clear the guard merely
to make an AWX job green. The NAS archive covers data; the journal separately
preserves the pre-change systemd unit required to restore the old seed.

## Capability evidence and validation limits

Facepunch documents [decay and upkeep](https://wiki.facepunch.com/rust/useful_commands),
[team size](https://wiki.facepunch.com/rust/server-gamemodes) and
[tutorial enablement](https://wiki.facepunch.com/rust/tutorial_island).
These references establish the names; preflight queries establish support in the
installed build. Runtime replies must have the form `convar: "value"` (quotes
optional); an unfamiliar response aborts rather than assuming success.

Local tests cover input rejection, config preservation/idempotence, narrow unit
changes, map selection, blueprint/WAL preservation, corruption detection,
successful configure/wipe transactions and failures before and after activation.
Systemd/RCON are mocked in those tests. CI also runs syntax/lint and the repository
suite. Neither local nor CI checks prove a live AWX deployment or game acceptance.
No live wipe is authorised by this code review.
