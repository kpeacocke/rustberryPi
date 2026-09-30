#!/usr/bin/env python3
"""Scheduled maintenance checks only: never install packages or update Rust."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

import apt

sys.path.insert(0, '/usr/local/libexec/pi5-rust')
from steam_manage import installed_build, public_build


def check_packages():
    subprocess.run(['apt-get', '-o', 'APT::Update::Error-Mode=any', 'update'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
    cache = apt.Cache()
    updates = [package for package in cache if package.is_upgradable]
    security = [package.name for package in updates if any(
        'security' in (origin.archive + ' ' + origin.label).lower() for origin in package.candidate.origins)]
    return {'count': len(updates), 'security_count': len(security),
            'names': [package.name for package in updates][:100]}


def check_rust(config):
    with open('/run/lock/pi5-rust.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        uuid = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '--mountpoint', '/srv/rust'], text=True).strip()
        if uuid != config['storage_uuid']:
            raise ValueError('Wrong storage mount')
        env = dict(os.environ, HOME='/srv/rust/home', FEX_ROOTFS=config['rootfs'])
        command = ['runuser', '-u', 'rust', '--', config['fex'], '/bin/bash', '/srv/rust/steamcmd/steamcmd.sh',
                   '+login', 'anonymous', '+app_info_update', '1', '+app_info_print', '258550', '+quit']
        reply = subprocess.check_output(command, env=env, cwd='/srv/rust/steamcmd', stderr=subprocess.STDOUT,
                                        text=True, timeout=240)
        current = installed_build(Path('/srv/rust/server/steamapps/appmanifest_258550.acf'))
        if not current:
            raise ValueError('Installed Rust manifest is incomplete or absent')
        return {'installed': current, 'available': public_build(reply)}


def check_fex(config):
    req = urllib.request.Request('https://api.github.com/repos/FEX-Emu/FEX/releases/latest',
                                 headers={'User-Agent': 'rustberryPi-maintenance'})
    with urllib.request.urlopen(req, timeout=15) as response:
        release = json.load(response)
    return {'installed_pin': config['fex_version'], 'latest_release': release['tag_name'],
            'published_at': release['published_at']}


def main():
    config = json.loads(Path('/etc/rustberrypi/telemetry.json').read_text())
    path = Path('/var/lib/rustberrypi/maintenance.json')
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError):
        result = {}
    # Keep earlier successful values but mark failed probes as failed/stale.
    for name, fn in [('packages', check_packages), ('rust', lambda: check_rust(config)),
                     ('fex', lambda: check_fex(config))]:
        now = time.time()
        try:
            result[name] = dict(fn(), ok=True, checked_at=now, attempted_at=now)
        except Exception as error:
            print(name + ' check failed: ' + type(error).__name__, flush=True)
            result.setdefault(name, {}).update(ok=False, attempted_at=now, error='Check failed; inspect probe environment/connectivity')
    result['reboot_required'] = Path('/run/reboot-required').exists()
    try:
        result['backup'] = json.loads(Path('/var/lib/rustberrypi-backup.json').read_text())
    except (OSError, ValueError):
        result['backup'] = {}
    # Summarise operational evidence without publishing raw journal messages/IPs.
    try:
        rows = subprocess.check_output(['journalctl', '-u', 'rust', '--since', '24 hours ago', '-n', '1000',
                                        '--output=json', '--no-pager'], text=True, timeout=10).splitlines()
        entries = [json.loads(row) for row in rows]
        warnings = [row for row in entries if any(word in row.get('MESSAGE', '').lower()
                    for word in ('error', 'failed', 'warning'))]
        saves = [int(row['__REALTIME_TIMESTAMP']) / 1e6 for row in entries
                 if 'saved ' in row.get('MESSAGE', '').lower() and 'entities' in row.get('MESSAGE', '').lower()]
        result['journal'] = {'warning_matches': len(warnings), 'last_save': max(saves) if saves else None,
                             'checked_at': time.time(), 'scope': 'Last 1000 entries, up to 24 hours'}
    except Exception:
        result['journal'] = {'error': 'Journal check unavailable'}
    result['checked_at'] = time.time()
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result))
    temporary.chmod(0o644)
    temporary.replace(path)


if __name__ == '__main__':
    main()
