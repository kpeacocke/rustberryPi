# Local telemetry

The collector runs as `rust-telemetry` and serves only `127.0.0.1:8090`. It samples
every five seconds and keeps ten minutes of trends in memory. RCON is loopback-only
with a random per-host password; only `serverinfo` and `playerlist` are queried.
The browser receives no RCON credential, Steam ID, player address or raw log text.
Player names are optional (`rust_telemetry_show_names: false` anonymises them).
The API has no mutation or arbitrary-command endpoint, rejects non-local Host
headers, and does not enable CORS. Never forward this port or proxy it publicly.

`maintenance.py` is a separate root oneshot, scheduled twice daily. It refreshes
APT metadata and inspects package candidates, including security origins. It runs
SteamCMD app-info as `rust` under the same lock as backup/update, verifies the USB
UUID, and compares the public build with the installed manifest. SteamCMD may
update its own bootstrap; no Rust update or package installation is requested.
It also checks the latest upstream FEX release, backup evidence and a bounded
journal summary. FEX release differences are for review, not an automatic upgrade.
Checks can take several minutes. Failed checks retain earlier values but mark
them failed; their previous success timestamp remains visible. A reboot marker's
absence does not prove that no reboot is necessary.

## Deploy

Use your existing private inventory, identity and pins. On the Pi:

```sh
ansible-playbook playbooks/telemetry.yml --connection local --ask-become-pass \
  -e ansible_python_interpreter=/usr/bin/python3
```

Or set `rust_telemetry_enabled: true` in private host vars and converge `rust.yml`.
The first deployment enables authenticated RCON in the persistent `server.cfg`
and gracefully restarts Rust if `rust_start` is true. Unchanged subsequent runs
do not restart Rust. The credential is stored in `/etc/rustberrypi/rcon-password`
and the persistent `server.cfg`; treat both and NAS archives as sensitive. On a
microSD rebuild a new credential is generated and both sides are updated together.
Existing RCON clients would need the new credential. No router or UFW rule is added.
Review any pre-existing custom RCON settings before deploying.

The standalone play assumes FEX at `/usr/bin/FEX` and the project's default RootFS.
If yours differs, set `rust_telemetry_fex`, `rust_telemetry_rootfs`, and
`rust_telemetry_fex_version` in private vars. Full convergence uses the selected
FEX facts. Configure `rust_telemetry_port`, `rust_telemetry_check_schedule`, and
`rust_telemetry_show_names` as needed. The timer starts a first check after boot
and may randomise it by five minutes. For an immediate operator-triggered check:

```sh
sudo systemctl start rust-maintenance-check.service
sudo journalctl -u rust-maintenance-check -n 30 --no-pager
```

Check `sudo systemctl status rust-telemetry` and `sudo ss -lntp`: HTTP must be on
127.0.0.1:8090 and RCON on 127.0.0.1:28016. No public listener is intended. Confirm
that a joining/leaving player appears, unplug/reconnect telemetry to check the
stale banner, and verify update comparisons before relying on them.

## Evidence and limits

* Current health and player data require actual device/Rust responses. Unknown
  values stay unknown. Ready requires service active plus A2S and RCON responses. Service active plus unavailable RCON is labelled as
  starting/telemetry unavailable, not conclusively healthy or failed.
* Backup/restore successes are recorded by the updated backup helper. Existing
  archives are not retroactively counted. Deploy the full play once to update
  that helper. A restore success records an operation, not proof of playability;
  still test a recovered server on a spare system.
* Last save and warning counts come from at most 1000 journal records in 24 hours,
  sampled on the slow maintenance schedule. Warnings are text matches, not proof
  of a crash. Displayed ages and check timestamps matter.
* Rust+ is a local TCP-listener check, not a WAN/pairing check. Public IP routing,
  router firmware, firewall auditing and AWX job status are not probed in this v1.
* Pi temperature reads sysfs. `vcgencmd` may be absent or inaccessible to the
  unprivileged collector; voltage/throttle status is then unknown. No broad device
  permissions are granted just to fill a tile. Disk I/O is host-wide, not USB-only.
* Restart counters are cumulative. Swap used is not swap rate. Suggestions report
  evidence and uncertainty; they never execute remediation.
* RCON is an administrative credential despite this app issuing only read-only
  commands. Protect the local account and physical desktop accordingly.

Sources: [Facepunch WebRCON](https://github.com/Facepunch/webrcon),
[Raspberry Pi vcgencmd](https://www.raspberrypi.com/documentation/computers/os.html#vcgencmd).
