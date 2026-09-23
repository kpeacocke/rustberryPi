#!/usr/bin/env python3
"""Fail closed on ambiguous labels, wrong mounts, non-USB or used partitions."""
import json
import os
from pathlib import Path
import subprocess
import sys


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.get('children', []))


def blank_usb_partition(node, ancestors):
    return (node['type'] == 'part' and not node.get('fstype')
            and not any(node.get('mountpoints') or [])
            and any(n.get('tran') == 'usb' for n in ancestors)
            and all(not any(n.get('mountpoints') or []) for n in ancestors))


def main(label, uuid, device, initialise):
    nodes = list(walk(json.loads(run('lsblk', '--json', '-p', '-o',
                                    'NAME,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINTS,TRAN'))['blockdevices']))
    matches = [n for n in nodes if (n.get('uuid') == uuid if uuid else n.get('label') == label)]
    formatted = False
    if not matches:
        if initialise != 'yes' or not device:
            raise ValueError('RUSTSERVER not found; attach USB or explicitly opt in to blank-partition initialization')
        actual = os.path.realpath(device)
        node = next(n for n in nodes if n['name'] == actual)
        ancestors = list(walk(json.loads(run('lsblk', '--json', '-s', '-p', '-o',
                                            'NAME,TYPE,FSTYPE,MOUNTPOINTS,TRAN', actual))['blockdevices']))
        if not blank_usb_partition(node, ancestors) or run('wipefs', '--no-act', '--noheadings', actual):
            raise ValueError('Refusing anything except an unmounted USB partition without signatures')
        for ancestor in ancestors:
            if ancestor['type'] == 'disk':
                siblings = walk(json.loads(run('lsblk', '--json', '-p', '-o', 'NAME,MOUNTPOINTS', ancestor['name']))['blockdevices'])
                if any(any(sibling.get('mountpoints') or []) for sibling in siblings):
                    raise ValueError('Refusing a disk with any mounted partition, including root or boot')
        if uuid:
            raise ValueError('Remove requested UUID for initialization; mkfs assigns a new UUID')
        subprocess.run(['mkfs.ext4', '-L', label, actual], check=True, stdout=sys.stderr)
        matches = [{'name': actual, 'fstype': 'ext4', 'uuid': run('blkid', '-s', 'UUID', '-o', 'value', actual)}]
        formatted = True
    if len(matches) != 1 or matches[0]['fstype'] != 'ext4':
        raise ValueError('Require exactly one matching ext4 filesystem')
    chosen = matches[0]
    mounted = subprocess.run(['findmnt', '-n', '-o', 'UUID', '--mountpoint', '/srv/rust'], capture_output=True, text=True)
    if mounted.returncode == 0 and mounted.stdout.strip() != chosen['uuid']:
        raise ValueError('/srv/rust is mounted from another filesystem')
    if mounted.returncode and Path('/srv/rust').exists() and any(Path('/srv/rust').iterdir()):
        raise ValueError('Refusing to hide files in an unmounted /srv/rust')
    # Duplicates or mounting the chosen filesystem elsewhere require explicit operator review.
    entries = [l.split() for l in Path('/etc/fstab').read_text().splitlines() if l.strip() and not l.lstrip().startswith('#')]
    if sum(e[1] == '/srv/rust' for e in entries) > 1:
        raise ValueError('Duplicate /srv/rust fstab entries; resolve before converging')
    aliases = {chosen['name'], 'UUID=' + chosen['uuid'], 'LABEL=' + label}
    if any(e[0] in aliases and e[1] != '/srv/rust' for e in entries):
        raise ValueError('Chosen filesystem already has a different fstab mountpoint')
    for mount in chosen.get('mountpoints') or []:
        if mount and mount != '/srv/rust':
            raise ValueError('Chosen filesystem mounted elsewhere')
    print(json.dumps({'uuid': chosen['uuid'], 'formatted': formatted}))


if __name__ == '__main__':
    main(*sys.argv[1:])
