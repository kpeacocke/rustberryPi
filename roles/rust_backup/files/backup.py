#!/usr/bin/env python3
"""Offline state archives, atomic publication and non-destructive restore."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


def sha(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def validate_members(members):
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'data':
            raise ValueError('Unsafe archive path')
        if not (member.isfile() or member.isdir()):
            raise ValueError('Links and special files are not allowed in state archives')


def mounted(path):
    subprocess.run(['mountpoint', '-q', str(path)], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['backup', 'restore'])
    parser.add_argument('--mount', type=Path, default=Path('/mnt/nas'))
    parser.add_argument('--destination', type=Path, default=Path('/mnt/nas/rust-backups'))
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--replace', action='store_true', help='Preserve existing state in a dated sibling and restore')
    args = parser.parse_args()
    with open('/run/lock/pi5-rust.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mounted(Path('/srv/rust'))
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        data = Path('/srv/rust/data')
        if args.operation == 'backup':
            mounted(args.mount)
            if args.mount.resolve() not in args.destination.resolve().parents:
                raise ValueError('Destination must be beneath the separate NAS mount')
            if os.stat(args.mount).st_dev in {os.stat('/srv/rust').st_dev, os.stat('/').st_dev}:
                raise ValueError('Backup must be on a filesystem separate from USB and OS root')
            args.destination.mkdir(parents=True, exist_ok=True)
            # Refuse a directory that escapes through a symlink to another filesystem.
            if os.stat(args.destination).st_dev != os.stat(args.mount).st_dev:
                raise ValueError('Backup destination is not on the configured NAS filesystem')
        else:
            if not args.archive or not args.sha256 or sha(args.archive) != args.sha256:
                raise ValueError('Supply an archive and its matching SHA256')
            with tarfile.open(args.archive) as archive:
                validate_members(archive.getmembers())
                if 'data/deployment.json' not in archive.getnames():
                    raise ValueError('Missing deployment metadata')
            if any(data.iterdir()) and not args.replace:
                raise ValueError('State exists; use --replace to preserve and replace it explicitly')
        active = subprocess.run(['systemctl', 'is-active', '--quiet', 'rust.service']).returncode == 0
        if active:
            subprocess.run(['systemctl', 'stop', 'rust.service'], check=True)
            result = subprocess.check_output(['systemctl', 'show', '-p', 'Result', '--value', 'rust.service'], text=True).strip()
            if result != 'success':
                raise RuntimeError('Unclean shutdown; refusing state operation; service left stopped')
        success = False
        try:
            if args.operation == 'backup':
                target = args.destination / ('rust-' + stamp + '.tar.gz')
                temporary = target.with_suffix('.partial')
                with tarfile.open(temporary, 'w:gz', dereference=False) as archive:
                    archive.add(data, arcname='data')
                with tarfile.open(temporary) as archive:
                    validate_members(archive.getmembers())
                digest = sha(temporary)
                temporary.rename(target)
                target.with_suffix(target.suffix + '.sha256').write_text(digest + '  ' + target.name + '\n')
                print(target)
            else:
                stage = Path(tempfile.mkdtemp(prefix='.restore-', dir='/srv/rust'))
                try:
                    with tarfile.open(args.archive) as archive:
                        archive.extractall(stage, filter='data')
                    metadata = json.loads((stage / 'data/deployment.json').read_text())
                    if (data / 'deployment.json').exists():
                        current = json.loads((data / 'deployment.json').read_text())
                        if any(metadata[k] != current[k] for k in ['identity', 'seed', 'worldsize', 'uid']):
                            raise ValueError('Restore metadata differs from deployed settings; align group_vars and retry')
                    subprocess.run(['chown', '-R', 'rust:rust', str(stage / 'data')], check=True)
                    previous = data.with_name('data.pre-restore-' + stamp)
                    data.rename(previous)
                    try:
                        (stage / 'data').rename(data)
                    except BaseException:
                        previous.rename(data)
                        raise
                    print('Restored; previous state preserved at ' + str(previous))
                finally:
                    shutil.rmtree(stage)
            success = True
        finally:
            # Backup errors do not strand the game offline. Failed restore needs inspection.
            if active and (success or args.operation == 'backup'):
                subprocess.run(['systemctl', 'start', 'rust.service'], check=True)


if __name__ == '__main__':
    main()
