#!/usr/bin/env python3
"""Offline state archives, atomic publication and non-destructive restore."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import sys
import json
import os
import re
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


def checked_archive(path, expected):
    if path.is_symlink() or not re.fullmatch(r'[a-f0-9]{64}', expected) or sha(path) != expected:
        raise ValueError('Archive checksum mismatch or unsafe archive path')
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        validate_members(members)
        if 'data/deployment.json' not in archive.getnames():
            raise ValueError('Missing deployment metadata')
        for member in members:
            if member.isfile():
                with archive.extractfile(member) as stream:
                    while stream.read(1024 * 1024):
                        pass
        metadata = json.load(archive.extractfile('data/deployment.json'))
        return metadata, sum(member.size for member in members if member.isfile())


def latest_archive(destination):
    # A sidecar marks a completed backup; never fall back silently from a corrupt one.
    candidates = sorted((p for p in destination.glob('rust-*.tar.gz')
                         if re.fullmatch(r'rust-\d{8}T\d{6}\.\d{6}Z\.tar\.gz', p.name)
                         and p.with_suffix(p.suffix + '.sha256').is_file()), reverse=True)
    if not candidates:
        raise ValueError('No completed NAS backup with a checksum sidecar was found')
    path = candidates[0]
    sidecar = path.with_suffix(path.suffix + '.sha256')
    if sidecar.is_symlink():
        raise ValueError('Checksum sidecar must not be a symlink')
    fields = sidecar.read_text().split()
    if len(fields) != 2 or fields[1] != path.name:
        raise ValueError('Invalid checksum sidecar')
    checked_archive(path, fields[0])
    return {'archive': str(path), 'sha256': fields[0]}


def record_status(**fields):
    status_path = Path('/var/lib/rustberrypi-backup.json')
    try:
        try:
            status = json.loads(status_path.read_text())
        except (OSError, ValueError):
            status = {}
        status.update(fields)
        temporary = status_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(status))
        temporary.chmod(0o644)
        temporary.replace(status_path)
    except OSError:
        print('Backup status could not be saved', file=sys.stderr)


def restart_and_verify(timeout, require_rcon):
    record_status(recovery_attempt=datetime.now(timezone.utc).timestamp(), recovery_ok=False)
    try:
        subprocess.run(['systemctl', 'start', 'rust.service'], check=True, timeout=30)
        command = ['/usr/bin/python3', str(Path(__file__).with_name('health.py')), str(timeout)]
        if require_rcon:
            command.append('--rcon')
        # Keep archive stdout machine-readable; failure diagnostics are redacted by health.py.
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout + 30)
        if result.returncode:
            print(result.stderr[-24000:], file=sys.stderr)
            raise RuntimeError('Archive operation finished but Rust recovery failed')
    except (OSError, subprocess.SubprocessError, RuntimeError):
        record_status(recovery_ok=False)
        raise
    record_status(recovery_ok=True, recovery_success=datetime.now(timezone.utc).timestamp())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['backup', 'restore', 'select-latest'])
    parser.add_argument('--mount', type=Path, default=Path('/mnt/nas'))
    parser.add_argument('--destination', type=Path, default=Path('/mnt/nas/rust-backups'))
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--replace', action='store_true', help='Preserve existing state in a dated sibling and restore')
    parser.add_argument('--leave-stopped', action='store_true', help='Reapply configuration before starting restored Rust')
    parser.add_argument('--health-timeout', type=int, default=1800)
    parser.add_argument('--require-rcon', action='store_true')
    args = parser.parse_args()
    if not 60 <= args.health_timeout <= 3600:
        raise ValueError('Health timeout must be between 60 and 3600 seconds')
    with open('/run/lock/pi5-rust.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mounted(Path('/srv/rust'))
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        data = Path('/srv/rust/data')
        if args.operation in ('backup', 'select-latest'):
            mounted(args.mount)
            if args.mount.resolve() not in args.destination.resolve().parents:
                raise ValueError('Destination must be beneath the separate NAS mount')
            if os.stat(args.mount).st_dev in {os.stat('/srv/rust').st_dev, os.stat('/').st_dev}:
                raise ValueError('Backup must be on a filesystem separate from USB and OS root')
            if args.operation == 'backup':
                args.destination.mkdir(parents=True, exist_ok=True)
            # Refuse a directory that escapes through a symlink to another filesystem.
            if os.stat(args.destination).st_dev != os.stat(args.mount).st_dev:
                raise ValueError('Backup destination is not on the configured NAS filesystem')
            if args.operation == 'select-latest':
                print(json.dumps(latest_archive(args.destination)))
                return
        else:
            if not args.archive or not args.sha256:
                raise ValueError('Supply an archive and its matching SHA256')
            metadata, required = checked_archive(args.archive, args.sha256)
            if (data / 'deployment.json').exists():
                current = json.loads((data / 'deployment.json').read_text())
                if any(metadata[k] != current[k] for k in ['identity', 'seed', 'worldsize', 'uid']):
                    raise ValueError('Restore metadata differs from deployed settings')
            if shutil.disk_usage(data.parent).free < required + 512 * 1024**2:
                raise ValueError('Insufficient free USB space to preserve current state and stage restore')
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
            # Archive/restore evidence is independent of subsequent game recovery.
            record_status(**{args.operation + '_success': datetime.now(timezone.utc).timestamp()})
            success = True
        finally:
            # Backup errors do not strand the game offline. Failed restore needs inspection.
            if active and (success or args.operation == 'backup') and not (args.operation == 'restore' and args.leave_stopped):
                restart_and_verify(args.health_timeout, args.require_rcon)


if __name__ == '__main__':
    main()
