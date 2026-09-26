#!/usr/bin/env python3
"""Fixed maintenance operations; no arbitrary RCON command interface."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time

import websocket

UNIT = Path('/etc/systemd/system/rust.service')
STATE = Path('/var/lib/rustberrypi-maintenance')
CONFIG = Path('/etc/rustberrypi/telemetry.json')
METADATA = Path('/srv/rust/data/deployment.json')


def run(*args):
    return subprocess.check_output(args, text=True, timeout=30).strip()


def inspect():
    text = UNIT.read_text()
    lines = [line for line in text.splitlines() if line.startswith('ExecStart=')]
    if len(lines) != 1:
        raise ValueError('Expected one managed ExecStart')
    argv = shlex.split(lines[0].split('=', 1)[1])
    index = argv.index('/srv/rust/server/RustDedicated')
    fex = argv[index - 1]
    if not re.fullmatch(r'/(usr|opt/fex/[a-f0-9]{40})/bin/FEX', fex):
        raise ValueError('Unrecognized managed FEX path')
    config = json.loads(CONFIG.read_text())
    uuid = run('findmnt', '-n', '-o', 'UUID', '--mountpoint', '/srv/rust')
    if not uuid or uuid != config['storage_uuid']:
        raise ValueError('Persistent filesystem mismatch')
    return {'fex': fex, 'rootfs': config['rootfs'], 'uuid': uuid,
            'telemetry_port': config['port']}


def exchange(client, command, identifier):
    client.send(json.dumps({'Identifier': identifier, 'Message': command, 'Name': 'maintenance'}))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        packet = json.loads(client.recv())
        if packet.get('Identifier') == identifier:
            message = packet.get('Message', '')
            if any(word in message.lower() for word in ('command not found', 'unknown command')):
                raise RuntimeError('Server rejected maintenance command')
            return message
    raise TimeoutError('No matching RCON acknowledgement')


def countdown_points(seconds):
    return sorted({value for value in (seconds, 300, 60, 10, 0) if value <= seconds}, reverse=True)


def notices(seconds, jobs):
    # One connection for the countdown; notices and save are deliberately fixed.
    password = Path('/etc/rustberrypi/rcon-password').read_text().strip()
    with closing(websocket.create_connection('ws://127.0.0.1:28016/' + password, timeout=15,
                                             http_no_proxy=['127.0.0.1'])) as client:
        players = json.loads(exchange(client, 'playerlist', 1))
        print(json.dumps({'connected_players': len(players)}), flush=True)
        identifier = 2
        checkpoints = countdown_points(seconds)
        deadline = time.monotonic() + seconds
        for remaining in checkpoints:
            # Drain broadcasts and send occasional ping frames while waiting.
            while time.monotonic() < deadline - remaining:
                client.settimeout(min(5, max(0.1, deadline - remaining - time.monotonic())))
                try:
                    if not client.recv():
                        raise ConnectionError('RCON closed during countdown')
                except websocket.WebSocketTimeoutException:
                    client.ping()
            client.settimeout(15)
            text = (f'Server maintenance ({jobs}) in {remaining} seconds. Please finish up.' if remaining
                    else 'Server maintenance starting now. Please reconnect after maintenance.')
            exchange(client, 'say ' + json.dumps(text), identifier)
            identifier += 1
        exchange(client, 'server.save', identifier)


def verify_archive(path):
    path = Path(path)
    expected = path.with_suffix(path.suffix + '.sha256').read_text().split()[0]
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if expected != actual:
        raise ValueError('Backup checksum mismatch')
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        for member in members:
            name = Path(member.name)
            if name.is_absolute() or '..' in name.parts or not name.parts or name.parts[0] != 'data':
                raise ValueError('Unsafe backup path')
            if not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe backup member')
            if member.isfile():
                with archive.extractfile(member) as stream:
                    while stream.read(1024 * 1024):
                        pass
        if 'data/deployment.json' not in archive.getnames():
            raise ValueError('Backup metadata missing')


def activate_fex(commit, version):
    if not re.fullmatch('[a-f0-9]{40}', commit) or not re.fullmatch(r'[A-Za-z0-9._-]+', version):
        raise ValueError('Invalid FEX pin')
    desired = f'/opt/fex/{commit}/bin/FEX'
    # The role can adopt an exact manually installed revision.
    try:
        adopted_version = run('/usr/bin/FEXGetConfig', '--version')
    except (OSError, subprocess.SubprocessError):
        adopted_version = None
    if adopted_version == version:
        desired = '/usr/bin/FEX'
    if run(str(Path(desired).with_name('FEXGetConfig')), '--version') != version:
        raise ValueError('FEX version mismatch')
    old = inspect()['fex']
    text = UNIT.read_text()
    lines = text.splitlines(keepends=True)
    updated = ''.join(line.replace(old, desired, 1) if line.startswith('ExecStart=') else line for line in lines)
    if text != updated:
        (STATE / 'rust.service.before-fex').write_text(text)
        temporary = UNIT.with_suffix('.maintenance-new')
        temporary.write_text(updated)
        temporary.chmod(0o644)
        os.replace(temporary, UNIT)
    config_path = CONFIG
    config = json.loads(config_path.read_text())
    config.update(fex=desired, fex_version=version)
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    metadata_path = METADATA
    metadata = json.loads(metadata_path.read_text())
    metadata['fex_commit'] = commit
    metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['inspect', 'announce', 'verify-backup', 'activate-fex'])
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--jobs', choices=['os', 'fex', 'rust', 'all'], default='all')
    parser.add_argument('--archive')
    parser.add_argument('--commit')
    parser.add_argument('--version')
    args = parser.parse_args()
    if args.operation == 'inspect':
        print(json.dumps(inspect()))
    elif args.operation == 'announce':
        if not 0 <= args.seconds <= 3600:
            raise ValueError('Countdown must be between 0 and 3600 seconds')
        notices(args.seconds, args.jobs)
    elif args.operation == 'verify-backup':
        verify_archive(args.archive)
    else:
        activate_fex(args.commit, args.version)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not print credential-bearing connection errors or RCON responses.
        print('Maintenance helper failed: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
