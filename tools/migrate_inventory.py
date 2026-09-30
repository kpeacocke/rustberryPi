#!/usr/bin/env python3
"""Preserve an old checkout's settings in ignored inventory, never overwrite."""
import argparse
from pathlib import Path
import re
import subprocess

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', required=True, help='Pre-update commit, usually ORIG_HEAD after pulling')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    def read(name):
        data = subprocess.check_output(['git', 'show', f'{args.ref}:{name}'], cwd=root, text=True)
        return yaml.safe_load(data)

    inventory = read('inventory/hosts.yml')
    settings = read('inventory/group_vars/rust_servers.yml')
    settings['fex_existing_rootfs'] = read('roles/fex/defaults/main.yml')['fex_existing_rootfs']
    if not settings.get('rust_identity'):
        raise ValueError('The selected commit has no deployment identity; select your actual pre-update commit')
    pending = {root / 'inventory/hosts.local.yml': inventory}
    for alias in inventory['rust_servers']['hosts']:
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', alias):
            raise ValueError('Unsupported inventory alias')
        base = root / 'inventory/host_vars' / alias
        if base.with_suffix('.yml').exists() or base.with_suffix('.yaml').exists():
            raise ValueError('Move existing host_vars/<alias>.yml into <alias>/settings.yml first')
        pending[base / '00-migrated.yml'] = settings
    if any(path.exists() for path in pending):
        raise ValueError('A migration destination already exists; preserve it and merge settings manually')
    for path, value in pending.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as stream:
            yaml.safe_dump(value, stream, sort_keys=False)
        path.chmod(0o600)
    print('Saved ignored local inventory and host settings. Review privately before deploying.')


if __name__ == '__main__':
    main()
