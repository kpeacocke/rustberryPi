#!/usr/bin/env python3
"""Rename a private inventory alias by IP while preserving its host variables."""
import argparse
from datetime import datetime, timezone
import ipaddress
import os
from pathlib import Path
import re
import yaml


def rename(root, address, name):
    ipaddress.ip_address(address)
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name):
        raise ValueError('Use a short lowercase hostname')
    source = root / 'inventory/hosts.local.yml'
    inventory = yaml.safe_load(source.read_text())
    hosts = inventory['rust_servers']['hosts']
    matches = [alias for alias, settings in hosts.items()
               if str((settings or {}).get('ansible_host', alias)) == address]
    if len(matches) != 1:
        raise ValueError('Expected exactly one matching address in rust_servers')
    old = matches[0]
    base = root / 'inventory/host_vars'
    for alias in {old, name}:
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', alias):
            raise ValueError('Unsupported existing alias')
        for suffix in ('.yml', '.yaml'):
            if (base / (alias + suffix)).exists():
                raise ValueError('Move single-file host vars into a directory before renaming')
    origin, target = base / old, base / name
    for path in (source, origin, target):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Inventory paths must stay inside this repository')
    if old != name and (name in hosts or target.exists()):
        raise ValueError('Destination already exists; refusing to merge or overwrite settings')
    settings = dict(hosts[old] or {})
    settings['rust_system_hostname'] = name
    if old == name and hosts[old] == settings:
        return False
    backup_dir = base / '.rename-backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '.yml')
    with backup.open('x') as stream:
        stream.write(source.read_text())
    backup.chmod(0o600)
    moved = old != name and origin.exists()
    if moved:
        origin.rename(target)
    hosts.pop(old)
    hosts[name] = settings
    temporary = source.with_suffix('.rename-tmp')
    try:
        with temporary.open('x') as stream:
            yaml.safe_dump(inventory, stream, sort_keys=False)
        temporary.chmod(0o600)
        os.replace(temporary, source)
    except BaseException:
        if moved:
            target.rename(origin)
        raise
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', required=True)
    parser.add_argument('--name', default='rustberrypi')
    args = parser.parse_args()
    changed = rename(Path(__file__).resolve().parents[1], args.address, args.name)
    print('Inventory and host vars renamed; saved world settings preserved.' if changed else 'Already configured.')
    print('Apply playbooks/hostname.yml to set the Linux hostname.')


if __name__ == '__main__':
    main()
