# AWX setup

Use this public Git repository as a Git project, branch `main`. The
`collections/requirements.yml` file supplies the same pinned collections as the
root `requirements.yml`; keep both in sync. Enable project update on launch with
a short cache timeout. Project synchronization does not deploy the Pi.

Use an AWX inventory group named `rust_servers` containing the existing Pi host.
Keep identity, storage UUID, NAS paths, approved player/admin IDs, SSH restrictions
and other host-specific settings in private AWX inventory variables. Use SSH
(`ansible_connection: ssh`), not the Pi's local connection setting. Select an
existing Machine credential that can connect to the Pi and become root. Preserve
SSH host-key verification and provision trusted host keys in the execution
environment. Use an execution environment with the tested ansible-core 2.20.1
from `requirements-dev.txt`, 2.18.19 from `awx/requirements-compat.txt`, or
2.18.20 from `awx/requirements-compat-21820.txt`, and a compatible Python
version. CI tests all three versions; collection sync alone
does not upgrade the execution environment's Ansible version.

Verified runtime: official `ghcr.io/ansible/community-ansible-dev-tools:v26.1.0`
contains ansible-core 2.20.1 and Python 3.13.9. Pin its manifest rather than latest:

```text
ghcr.io/ansible/community-ansible-dev-tools@sha256:1cb572c1c66b8a73af9b6368e12d9becf2e960b8bfab885c663b8b65583cec3b
```

Create a dedicated AWX execution environment with this image and select it for
all RustberryPi job templates. Keep other projects on their existing runtime.
Project synchronization installs this repository's pinned collections.

Some custom AWX Compose deployments disable runner process isolation. In that
case the execution worker's installed Ansible runs every job, regardless of the
image selected in AWX. The readiness check reports the **actual** version. The
2.18.19 and 2.18.20 compatibility runtimes are tested for this deployment mode; do not assume
that selecting the 2.20.1 image upgrades a non-isolated worker. Changing the shared
worker or enabling container isolation is a separate infrastructure change.

## Jobs and workflow entry points

Shared defaults are mirrored in `playbooks/group_vars/rust_servers.yml` so AWX's
generated inventory loads them too. Keep that file identical to
`inventory/group_vars/rust_servers.yml`; the test suite checks this. Put private
overrides on the AWX **host**, not in shared group defaults.

| Job | Playbook | Workflow |
| --- | --- | --- |
| Readiness check | `playbooks/awx-preflight.yml` | Read-only deployment checks |
| Converge | `playbooks/rust.yml` | Deploy / configure |
| Player access | `playbooks/rust.yml` | Access survey from `awx/survey.json` |
| Gameplay | `playbooks/gameplay.yml` | [Gameplay survey and safeguards](GAMEPLAY.md) |
| New world | `playbooks/new-world.yml` | [Explicit map-only wipe approval](GAMEPLAY.md) |
| Telemetry | `playbooks/telemetry.yml` | Dashboard deployment |
| Security | `playbooks/secure.yml` | Security convergence |
| Refresh boards | `playbooks/refresh-maintenance.yml` | Refresh and verify maintenance status only |
| Backup | `playbooks/backup.yml` | Backup only; no automatic player warning |
| Restore latest | `playbooks/restore-latest.yml` | Explicit restore confirmation survey |
| OS maintenance | `playbooks/maintenance-os.yml` | OS only |
| FEX maintenance | `playbooks/maintenance-fex.yml` | FEX only; exact commit/version required |
| Hailo candidate | `playbooks/hailo-candidate.yml` | Builds a private candidate only; never installs |
| Hailo maintenance | `playbooks/maintenance-hailo.yml` | Survey from `awx/hailo-maintenance-survey.json`; installs an exact candidate after backup and announcement |
| Hailo safety control | `playbooks/hailo-control.yml` | Survey from `awx/hailo-control-survey.json`; status, disable, enable-and-verify, or install a candidate while disabled |
| Rust maintenance | `playbooks/maintenance-rust.yml` | Rust only |
| All maintenance | `playbooks/maintenance-all.yml` | One countdown/backup for OS, FEX, Rust and detected Hailo hardware |
| Select maintenance | `playbooks/maintenance.yml` | Survey from `awx/maintenance-survey.json` |

Each workflow should invoke its corresponding job once. Do not chain component
maintenance jobs to implement All: the combined playbook owns the one shared
maintenance window. Disable concurrent launches and slicing on every template;
limit jobs to the Pi. Avoid launching different workflows concurrently, since
per-template concurrency settings do not serialize different templates. Do not
create recurring schedules until an operating schedule is chosen.

Enable privilege escalation, set a suitable timeout (initially four hours for
maintenance/restore), and enable `rust_telemetry_enabled` where telemetry is
required. The OS workflow can reboot through SSH. FEX updates require both the
reviewed commit and matching version; persist the normal deployment pins too.
Restore requires `rust_restore_confirm=true`, `rust_restore_replace=true`,
`rust_start=true` and `rust_telemetry_enabled=true`. Ask for restore confirmation
at launch; do not hard-code permanent approval into a scheduled template.

Private local files are not included in the public SCM project. Migrate their
values into private inventory or appropriate AWX credentials before the first
job. Keep NAS credentials on the Pi or provide them using the documented private
credential mechanism. Never put passwords into surveys or ordinary inventory
variables. In-game maintenance warnings are already implemented. External AWX
notifications need an explicitly selected notification destination.

Creating templates does not prove connectivity. Verify project sync, execution
environment compatibility, SSH/become access and private variables before the
first operational launch. Live AWX object IDs and credentials do not belong in
this public repository.

## Manual launches and schedules

Run **Readiness check** first. It checks the actual controller version, SSH/sudo,
hostname, DNS, 4 KB pages, running services, USB/NAS mounts, deployed world
settings, maintenance guard and recent collector RCON health. It does not verify
NAS write permissions, archive integrity, all deployment settings, Steam login,
or successful completion of a future update. The operational jobs retain their
own backup and health gates.

For manual work, open the desired workflow and choose Launch. Answer its survey.
OS and Rust use the ten-minute warning by default; FEX and All also require a
reviewed commit and matching version. Backup briefly stops Rust and has no
countdown. Restore requires deliberate confirmation and rolls back progress.

For a recurring run, open the workflow's Schedules tab, choose Add, select the
timezone, start time and recurrence, and save the launch inputs. Schedules cannot
wait for a person to answer a survey or enter an SSH/sudo password. Save reviewed
survey values with the schedule and use the existing stored Machine credential.
Do not schedule restores. Do not schedule FEX/All expecting automatic latest;
their pins must be selected and maintained deliberately. Do not overlap different
maintenance workflows or the Pi's backup timer. No recurring schedule is enabled
by this repository; choose an operating window before creating one.

## Adding new things to AWX

AWX does not learn about repository changes on its own. A project sync updates
the checked-out playbooks and installs the collections pinned in
`collections/requirements.yml`. It does not create templates, attach or refresh
surveys, set variables, or change the execution environment's ansible-core
version. Everything below is a deliberate AWX-side step taken after the code has
merged, and none of it is created automatically by this repository.

| What changed in the repo | What to do in AWX |
| --- | --- |
| New playbook | Create a job template, add it to the table above, and add a workflow if it needs approval or notifications |
| New role default | Nothing, unless this Pi should differ from the shipped default |
| New shared fleet default | Mirror it into both `group_vars` files; sync the project |
| New host-specific or private value | Set it as an AWX **host** variable |
| New per-launch choice | Add it to the survey JSON, then re-import the survey |
| New collection | Add the pin to `collections/requirements.yml` and `requirements.yml`, then sync |
| New Python or ansible-core requirement | Rebuild or re-pin the execution environment |
| New secret | Create or extend a credential; never a survey or inventory variable |

### Where a new variable belongs

Choose the narrowest home that works. A role default in `roles/*/defaults/main.yml`
ships with the project and needs no AWX action at all; this is the right place for
a new feature flag that is safe when off, which is why `rust_telemetry_enabled`
and `rust_telemetry_screen_blank` default to `false`. Turning such a flag on
for this Pi is a host variable in AWX, not an edit to shared defaults.

Server CPU analysis is included when telemetry is deployed. Optional HAT analysis
uses host variables `rust_telemetry_hat_enabled: true` and
`rust_telemetry_hat_model_dir`: the latter must be an absolute path accessible
**inside the execution environment**, not a path on your laptop. Supply the
private compiled HEF/manifest through a reviewed read-only artifact mount or
private deployment source; do not commit them to the public project. Both deploy
playbooks automatically detect Hailo/CSI hardware and provision its runtime,
driver and permissions unless the persistent Hailo safety latch is present;
while latched, Hailo provisioning and HAT inference are skipped regardless of
hardware detection or the inference toggle. No manual package job or camera
capture is needed.
Exact running-kernel headers must be available. Model compilation and real
hardware acceptance remain prerequisites for enabling HAT inference.
See [HAT commissioning](../telemetry/README.md#server-early-warning-and-hailo).
No new survey, credential or game-control permission is required.

### Hailo safety control

Create a dedicated job template for `playbooks/hailo-control.yml`, import
`awx/hailo-control-survey.json`, enable privilege escalation and use the
`rust_servers` inventory. The launch variable
`rust_hailo_control_action` defaults to `status` and offers exactly
`status`, `disable`, `enable_verify`, and `install_while_disabled`.
`rust_hailo_control_version` is used only by the last choice and must identify
an exact, ready local candidate for the running kernel. Keep this template
restricted to trusted Hailo operators: it changes kernel-module policy and can
install a driver/runtime. Disable template concurrency and slicing, do not
schedule it, and do not overlap it with candidate builds or maintenance. Use a
**28800-second (8-hour) timeout** so the action can wait for an in-flight
candidate/build lock and complete a bounded DKMS transaction; the control
commands, initramfs rebuild (all installed kernel variants), and Rust lifecycle
lock have their own time limits.

`status` is read-only and reports control policy, module load state, PCI
presence, and whether a previous explicit hardware verification still matches
the running kernel. It does not activate or identify hardware. `disable`
stops and masks only `rust-health-hat.service`, installs both an explicit
modprobe denial and blacklist, removes the Hailo modules-load entry, rebuilds
all initramfs variants, and attempts a non-forced module unload. An unload
failure is reported and never described as unloaded; the persistent boot block
is retained. `enable_verify` requires a connected supported Hailo-8 board,
checks the running-kernel module and runtime, loads and identifies the device,
then leaves inference stopped and masked. On failure it restores the disabled
policy where possible. `install_while_disabled` accepts only a complete,
integrity-checked candidate for the running kernel, updates source-managed
driver/runtime/firmware, and keeps module loading blocked and inference off; it
does not modprobe, identify the board, or claim hardware verification. APT-
managed installed 4.23.0 can be explicitly enabled and verified without a
source-managed runtime link.

This workflow never stops, starts, restarts or otherwise changes Rust. It does
not reboot. The disable policy is persistent across OS provisioning, All
maintenance and OS reboots; ordinary Hailo maintenance refuses to clear it and
All explicitly skips Hailo while latched, even if a PCI device is detected.
The control workflow does not modify the public dashboard candidate-readiness
artifact. Keep firmware EULA approval in private host variables; it is not a
survey field.

Hailo source updates use a separate candidate-only AWX job template for
`playbooks/hailo-candidate.yml`; schedule it only after accepting Hailo's
firmware terms and setting the private host variable
`rust_maintenance_hailo_firmware_eula_accepted: true`. The builder keeps all outputs on the
Pi and never uploads firmware. `playbooks/maintenance-hailo.yml` is a distinct
NAS-backed, manual maintenance action. Leave the candidate version blank to
select the newest complete local candidate newer than the installed driver
and matching the running kernel, or enter an exact version override.
Hashes and source commits are validated before announcing downtime. No eligible
candidate means the job stops before interrupting Rust.
Review its rollback/hardware validation output. Import
`awx/hailo-maintenance-survey.json` into that dedicated template and enable its
survey. Only the countdown is required; the version is an optional override. The EULA
approval is intentionally not a survey field. The candidate-build template
needs no survey and never chains automatically into installation.
The selectable maintenance survey also includes the `hailo` choice.
Its `all` choice includes Hailo when the existing read-only PCI detector finds
Hailo hardware; hosts without Hailo explicitly skip it. This is independent of
`rust_telemetry_hat_enabled`, which enables inference only. Private
`rust_maintenance_hailo_firmware_eula_accepted: true` remains required.
If the persistent Hailo safety latch is active, All explicitly skips Hailo
regardless of PCI detection and still performs its selected OS/FEX/Rust work;
ordinary deployment/provisioning does not remove that latch.
All validates/pins the common stable Hailo-8 4.x release or exact optional
`rust_maintenance_hailo_version` override before downtime, then builds/revalidates
against the actual running kernel after OS work/reboot. Already-current hosts
undergo module-kernel and hardware verification without a driver upgrade.
Historical source-runtime build manifests remain immutable when OS DKMS
rebuilds the installed module for a new kernel; their integrity is checked
separately from the installed module's actual kernel identity.
Standalone `hailo` still requires a ready, newer local candidate before downtime.
All shares the candidate artifact lock with candidate jobs and blocks new EDA
builds for its maintenance window; it waits for in-flight builds before downtime.
No additional provisioning, firmware approval or automatic maintenance chaining
is introduced. Set the Select-maintenance/All controller timeout to **72000
seconds (20 hours)** when All can build Hailo. The existing four-hour timeout
(`14400`) is insufficient. This conservative budget covers draining an
in-flight builder (`21660` seconds), the new build (`21600`), backup (`3600`),
the maximum warning (`3600`), game health (`1800` by default), and time for
OS/reboot, FEX, Rust, RCON recovery and board refresh. Increase it further if
measured component times or the configured health timeout require more.
Read-only release preflight does not wait on the artifact lock; builders are
drained only after acquiring the maintenance marker and before downtime.
On first rollout of artifact locking, drain any candidate job started with the
older builder before launching All; an already-running old process cannot
retroactively acquire the new lock.
Re-import `awx/maintenance-survey.json` after syncing if using that template
instead. These files define launch surveys, not AWX job-template objects;
project sync alone does not create templates or import surveys.

### Event-driven Hailo builds

`rulebooks/hailo-candidate.yml` is the EDA entry point. Configure an EDA Git
project using this repository and a decision environment containing
`ansible-rulebook` and the `rustberrypi.hailo.hailo_release` source. Build the
derived environment from the repository root:

```sh
podman build -f extensions/eda/Dockerfile \
  --build-arg EDA_BASE_IMAGE=YOUR_TESTED_EDA_IMAGE \
  -t localhost/rustberrypi-eda-hailo:YOUR_REVISION .
```

The Dockerfile preserves the base image's runtime user and needs no shell build
steps, so packaging does not require starting a nested build container.
The Dockerfile includes only the source and shared release selector, not private
inventory or firmware. Use a locally cached image with pull policy **Never**
when the activation worker uses that same Podman backend; otherwise publish it
to your approved registry and configure its image credential.
Simply syncing a Git project does not install its Python source into the
decision environment. Its source is
`extensions/eda/plugins/event_source/hailo_release.py`; the source reuses the
builder's stable common Hailo-8 4.x release selection. Set `awx_organization`
in the activation variables to the organization owning the candidate template
and attach an Automation Controller credential authorized to launch only the
candidate job. The EDA activation checks hourly and emits once on startup and
when the common upstream version changes. The Pi-side builder still checks
whether a newer candidate is needed; an EDA restart cannot force a downgrade.
A failed build is visible as a failed AWX job; retry it manually after correcting
the error, or restart the activation to recheck the current release.

The rulebook never launches maintenance, approves firmware terms, changes
surveys, or installs a candidate. Operators launch Hailo maintenance or Select
maintenance after a candidate exists. Standard AWX surveys do not dynamically
disable the Launch button based on Pi state; the pre-downtime candidate gate is
the authoritative availability check. Do not enable optional-version surveys
until the project revision includes the corresponding selection code.

An EDA container marked running is not sufficient: the controller needs initial
organization/user setup, a synced project, a decision environment, controller
credentials, and a healthy rulebook activation with a working execution backend.
Outbound GitHub polling needs no public webhook or EDA reverse proxy. Browser
UI/API access requires separate DNS/TLS routing; preserve the controller's
WebSocket route and do not expose an unauthenticated API.

A genuinely fleet-wide, non-secret default belongs in
`inventory/group_vars/rust_servers.yml` **and** the identical
`playbooks/group_vars/rust_servers.yml`, because AWX's generated inventory loads
the `playbooks` copy rather than the repository inventory. The test suite fails if
the two drift apart. Anything host-specific, operational or sensitive — identity,
storage UUID, NAS paths, addresses, approved player and admin IDs, FEX pins —
belongs on the AWX host, where it stays out of this public repository.

### Surveys

`awx/survey.json` and `awx/maintenance-survey.json` are the reviewed source of
truth, but AWX keeps its own copy inside each template. Editing the file and
syncing the project changes nothing until the survey is re-imported into the
template and re-enabled. Confirm the variable names afterwards: a survey answer
is an extra variable and silently wins over inventory, so a typo fails open to
the inventory value rather than erroring.
Keep multiple-choice `choices` as JSON arrays when importing surveys; this
AWX launch UI shows an empty dropdown for legacy newline-delimited strings.
If the Select maintenance workflow's launch wizard reports "No JobTemplate
matches the given query", launch its single `RustberryPi - Select maintenance`
job template instead. Do not launch both.

Surveys are for decisions a person makes at launch, not for configuration that
should persist. Never put a password, key or NAS credential in one. A question
whose answer is always the same is better as a host variable, and a destructive
one — restore confirmation in particular — should be asked at launch rather than
saved into a schedule.

### Execution environment and collections

Project sync installs collections into the project, so a new pinned collection is
picked up by the next sync. It does not upgrade ansible-core or Python. A change
that requires a newer ansible-core needs the execution environment rebuilt or
re-pinned to a new manifest, and on a Compose deployment with runner process
isolation disabled it needs the shared worker upgraded instead, because the image
selected in AWX is ignored there. The readiness check reports the version actually
in use; trust it over the template's configuration.
The Rust convergence play checks that the worker can load `community.general.ufw`
before it changes or stops the game when firewall management is enabled. If this
fails after a successful project sync, repair collection visibility in the
execution worker (verify `ansible-doc -t module community.general.ufw` there);
changing the project's pinned requirements alone will not fix the job's runtime.

### Before the first real launch

Creating a template does not prove it works. Sync the project, run **Readiness
check** against the Pi, and confirm the controller version, SSH and become access,
and that the new variables resolve as intended. Launch the new template once with
`--check`-equivalent expectations in mind and read its output before scheduling
it. Disable concurrent launches and job slicing, limit the job to the Pi, and set a
timeout that covers the worst case rather than the typical run. Do not add a
recurring schedule until the job has succeeded manually at least once.
