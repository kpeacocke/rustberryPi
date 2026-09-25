#!/usr/bin/env python3
"""Reconcile vanilla skip-queue access while Rust is stopped; preserve bans/admins."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import subprocess
import sys
import tempfile


def parse_ids(raw):
    if isinstance(raw, str):
        raw = re.split(r'[\s,]+', raw.strip()) if raw.strip() else []
    if not isinstance(raw, list):
        raise ValueError('Players must be a list or comma/newline separated SteamID64 values')
    players = []
    for value in raw:
        value = str(value).strip()
        value = re.sub(r'^https://steamcommunity\.com/profiles/(\d+)/?$', r'\1', value)
        if not re.fullmatch(r'7656119\d{10}', value):
            raise ValueError('Use numeric SteamID64 values or numeric profile URLs; resolve vanity links first')
        players.append(value)
    players = sorted(set(players))
    return players


def policy(request, previous):
    admin_input = request.get("admins", "preserve")
    mode = request.get('mode', 'preserve')
    if mode == 'preserve':
        request = previous or {'mode': 'public', 'players': []}
        mode = request['mode']
    if mode not in ('public', 'restricted'):
        raise ValueError('Choose public or restricted access')
    players = parse_ids(request.get('players', []))
    if mode == 'restricted' and not 1 <= len(players) <= 4:
        raise ValueError('Restricted mode requires one to four distinct players')
    result = {'mode': mode, 'players': players if mode == 'restricted' else []}
    if admin_input == 'preserve':
        if previous and 'admins' in previous:
            result['admins'] = previous['admins']
    else:
        result['admins'] = parse_ids(admin_input)
    if mode == 'restricted' and not set(result.get('admins', [])).issubset(players):
        raise ValueError('Every administrator must also be in the approved player list')
    return result


def reconcile(text, desired):
    kept = []
    current = []
    owners = []
    moderators = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith('//'):
            kept.append(line)
            continue
        if ';' in stripped:
            raise ValueError('Compound commands in users.cfg require explicit review')
        fields = shlex.split(stripped)
        command = fields[0].removeprefix('global.')
        if command == 'skipqueueid':
            if len(fields) < 2:
                raise ValueError('Malformed skipqueue entry; inspect users.cfg')
            current.append(fields[1])
            continue
        if command in ('ownerid', 'moderatorid') and 'admins' in desired:
            if len(fields) < 2:
                raise ValueError('Malformed administrator entry')
            (owners if command == 'ownerid' else moderators).append(fields[1])
            continue
        if desired['mode'] == 'restricted':
            if command not in ('ownerid', 'moderatorid', 'banid'):
                raise ValueError('Unknown users.cfg command; review before restricting access')
            if command in ('ownerid', 'moderatorid') and (len(fields) < 2 or fields[1] not in desired['players']):
                raise ValueError('An existing administrator is outside the approved list; review users.cfg explicitly')
        kept.append(line)
    admins_match = 'admins' not in desired or (sorted(owners) == desired['admins'] and not moderators)
    if sorted(current) == desired['players'] and admins_match:
        return text
    output = ''.join(kept)
    if output and not output.endswith('\n'):
        output += '\n'
    output += ''.join(f'ownerid {value} "admin" "managed admin"\n' for value in desired.get('admins', []))
    return output + ''.join(f'skipqueueid {value} "friend" "managed access"\n' for value in desired['players'])


def atomic_write(path, text, uid, gid):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, uid, gid)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    identity = sys.argv[1]
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', identity):
        raise ValueError('Invalid identity')
    request = json.load(sys.stdin)
    root = Path('/srv/rust/data')
    cfg = root / identity / 'cfg'
    state = root / 'access.json'
    users = cfg / 'users.cfg'
    with open('/run/lock/pi5-rust.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for path in (root, root / identity, cfg, users, state, cfg / 'server.cfg'):
            if path.is_symlink():
                raise ValueError('Access configuration must not contain symlinks')
        previous = json.loads(state.read_text()) if state.exists() else None
        desired = policy(request, previous)
        if desired['mode'] == 'restricted' and (cfg / 'server.cfg').exists():
            for line in (cfg / 'server.cfg').read_text().splitlines():
                if re.match(r'^\s*(?:server\.)?maxplayers\b', line):
                    raise ValueError('Remove the maxplayers override in server.cfg before restricting access')
        original = users.read_text() if users.exists() else ''
        updated = reconcile(original, desired)
        changed = desired != previous or updated != original
        if changed:
            # Wait for graceful shutdown before re-reading: Rust may save users.cfg.
            active = subprocess.run(['systemctl', 'is-active', '--quiet', 'rust.service']).returncode == 0
            if active:
                subprocess.run(['systemctl', 'stop', 'rust.service'], check=True)
            original = users.read_text() if users.exists() else ''
            updated = reconcile(original, desired)
            account = pwd.getpwnam('rust')
            cfg.mkdir(parents=True, exist_ok=True)
            for directory in (root / identity, cfg):
                os.chown(directory, account.pw_uid, account.pw_gid)
                os.chmod(directory, 0o750)
            if users.exists() and updated != original:
                # One preserved pre-management copy; never overwrite it.
                backup = cfg / 'users.cfg.pre-access'
                if not backup.exists():
                    with backup.open('x') as stream:
                        stream.write(original)
                    os.chmod(backup, 0o600)
            if updated != original:
                atomic_write(users, updated, account.pw_uid, account.pw_gid)
            atomic_write(state, json.dumps(desired, sort_keys=True) + '\n', 0, 0)
        print(json.dumps({'changed': changed, 'maxplayers': 0 if desired['mode'] == 'restricted' else 4}))


if __name__ == '__main__':
    main()
