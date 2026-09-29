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
environment. Use an execution environment with the tested ansible-core version
from `requirements-dev.txt` and Python compatible with it; collection sync alone
does not upgrade the execution environment's Ansible version.

## Jobs and workflow entry points

| Job | Playbook | Workflow |
| --- | --- | --- |
| Converge | `playbooks/rust.yml` | Deploy / configure |
| Player access | `playbooks/rust.yml` | Access survey from `awx/survey.json` |
| Telemetry | `playbooks/telemetry.yml` | Dashboard deployment |
| Security | `playbooks/secure.yml` | Security convergence |
| Backup | `playbooks/backup.yml` | Backup only; no automatic player warning |
| Restore latest | `playbooks/restore-latest.yml` | Explicit restore confirmation survey |
| OS maintenance | `playbooks/maintenance-os.yml` | OS only |
| FEX maintenance | `playbooks/maintenance-fex.yml` | FEX only; exact commit/version required |
| Rust maintenance | `playbooks/maintenance-rust.yml` | Rust only |
| All maintenance | `playbooks/maintenance-all.yml` | One countdown/backup for OS, FEX and Rust |
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
