#!/usr/bin/env python3
"""Guarded, offline gameplay and map-only transactions for an existing deployment."""
import argparse
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time

# Installed together in the existing libexec directory.
import access
import backup
import health
import maintenance

DATA = Path('/srv/rust/data')
UNIT = Path('/etc/systemd/system/rust.service')
STATE = Path('/var/lib/rustberrypi-maintenance')
CONVARS = {'decay.scale': 'decay_scale', 'decay.upkeep_period_minutes': 'upkeep',
           'relationshipmanager.maxteamsize': 'team_size', 'server.tutorialenabled': 'tutorial'}
PAUSED = ('rust-backup.timer', 'rust-maintenance-check.timer',
          'rust-watchdog.timer', 'rust-telemetry.service')


def validate(request):
    if request['operation'] not in ('configure', 'new-world'):
        raise ValueError('Invalid operation')
    for key, low, high in [('upkeep', 1, 10080), ('team_size', 1, 5),
                            ('worldsize', 1000, 2000), ('seed', 0, 2147483647),
                            ('warning', 0, 3600), ('timeout', 60, 3600)]:
        if type(request[key]) is not int or not low <= request[key] <= high:
            raise ValueError('Invalid ' + key)
    scale = request['decay_scale']
    if type(scale) not in (int, float) or not math.isfinite(scale) or not 0 <= scale <= 10:
        raise ValueError('Invalid decay scale')
    if type(request['tutorial']) is not bool or request['admins'] not in ([], ''):
        raise ValueError('Use a boolean tutorial value and an explicitly empty admin list')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', request['identity']):
        raise ValueError('Invalid identity')
    description = request.get('description')
    if description is not None and (not isinstance(description, str) or
                                    any(c in description for c in ('\r', '\x00'))):
        raise ValueError('Invalid server description')
    if request['operation'] == 'new-world':
        expected = f"WIPE {request['identity']} {request['seed']} {request['worldsize']} KEEP BLUEPRINTS"
        if request.get('confirm') != expected:
            raise ValueError('Exact per-launch wipe confirmation required: ' + expected)


def values(request):
    return {name: request[key] for name, key in CONVARS.items()}


def config_values(request):
    settings = values(request)
    if request.get('description') is not None:
        settings['server.description'] = request['description']
    return settings


def verify_description(request):
    if request.get('description') is None:
        return
    response = rcon('server.description').strip()
    prefix = 'server.description:'
    if not response.startswith(prefix):
        raise ValueError('Unsupported runtime description reply')
    raw = response[len(prefix):].strip()
    try:
        actual = json.loads(raw)
    except json.JSONDecodeError:
        actual = raw[1:-1].replace('\\n', '\n') if raw.startswith('"') and raw.endswith('"') else raw
    if actual != request['description']:
        raise ValueError('Runtime description differs')


def compound_command(line):
    quoted = escaped = False
    for char in line:
        if escaped:
            escaped = False
        elif char == '\\' and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == ';' and not quoted:
            return True
    if quoted:
        raise ValueError('Unterminated configuration string')
    return False


def reconcile(text, settings):
    """Remove every duplicate, including differently cased convars; keep other lines."""
    result = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('//'):
            if compound_command(stripped):
                raise ValueError('Compound server.cfg commands require review')
            key = stripped.split()[0].lower()
            if key in settings:
                continue
            if key in ('server.seed', 'server.worldsize', 'server.level', 'server.levelurl',
                       'server.identity', 'ownerid', 'moderatorid', 'global.ownerid',
                       'global.moderatorid', 'exec'):
                raise ValueError('Conflicting world/admin configuration requires review')
        result.append(line)
    return '\n'.join(result).rstrip('\n') + '\n' + ''.join(
        f'{key} {json.dumps(value, ensure_ascii=False) if isinstance(value, str) else str(value).lower()}\n'
        for key, value in settings.items())


def update_unit(text, old, new):
    lines = text.splitlines(keepends=True)
    starts = [line for line in lines if line.startswith('ExecStart=')]
    if len(starts) != 1 or '+server.level "Procedural Map"' not in starts[0]:
        raise ValueError('Only the managed Procedural Map unit is supported')
    line = starts[0]
    if any(re.search(r'\+' + re.escape(name) + r'\b', line, re.I) for name in
           (*CONVARS, 'ownerid', 'moderatorid', 'global.ownerid', 'global.moderatorid',
            'server.levelurl', 'exec')):
        raise ValueError('Conflicting startup overrides require review')
    for key in ('identity', 'seed', 'worldsize'):
        pattern = r'(\+server\.' + key + r'\s+)([^\s]+)'
        matches = re.findall(pattern, line)
        if len(matches) != 1 or matches[0][1] != str(old[key]):
            raise ValueError('Unit and deployment metadata disagree: ' + key)
        line = re.sub(pattern, lambda m: m[1] + str(new[key]), line)
    return ''.join(line if item == starts[0] else item for item in lines)


def world_files(identity):
    selected = []
    for path in identity.iterdir():
        if '.sav' in path.name or path.suffix == '.map':
            if not re.fullmatch(r'proceduralmap\.\d+\.\d+\.\d+\.(?:map|sav(?:\.\d+)?)', path.name):
                raise ValueError('Unknown world filename; inspect before wiping: ' + path.name)
            selected.append(path)
    if not any(path.suffix == '.sav' and path.stat().st_size > 0 for path in selected):
        raise ValueError('No existing nonempty procedural world save')
    return selected


def clear_admins(text, desired):
    # Refuse indirect grants (for example exec) even in public mode.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('//'):
            command = stripped.split()[0].lower().removeprefix('global.')
            if command not in ('ownerid', 'moderatorid', 'banid', 'skipqueueid'):
                raise ValueError('Unknown users.cfg command requires review')
    # Rust command spelling is case-insensitive; normalise only admin commands.
    text = re.sub(r'(?im)^(\s*)(?:global\.)?(ownerid|moderatorid)(?=\s)',
                  lambda m: m[1] + m[2].lower(), text)
    return access.reconcile(text, desired)


def require_current_save(identity, metadata):
    pattern = f"proceduralmap.{metadata['worldsize']}.{metadata['seed']}.*.sav"
    if not any(p.stat().st_size > 0 for p in identity.glob(pattern)):
        raise ValueError('No nonempty save matches deployed seed and size')


def safe_tree(root):
    for path in [root, *root.rglob('*')]:
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('State tree contains a link or special file')


def rcon(command):
    import websocket
    password = Path('/etc/rustberrypi/rcon-password').read_text().strip()
    if not re.fullmatch('[a-f0-9]{64}', password):
        raise ValueError('Invalid managed credential')
    with closing(websocket.create_connection('ws://127.0.0.1:28016/' + password,
                                             timeout=15, http_no_proxy=['127.0.0.1'])) as client:
        return maintenance.exchange(client, command, 825)


def read_value(name):
    response = rcon(name)
    match = re.fullmatch(r'\s*' + re.escape(name) + r'\s*:\s*"?(-?\d+(?:\.\d+)?|true|false)"?\s*',
                         response, re.I)
    if not match:
        raise ValueError('Unsupported or ambiguous runtime convar: ' + name)
    value = match[1].lower()
    return value == 'true' if value in ('true', 'false') else float(value)


def verify(settings):
    for key, value in settings.items():
        if read_value(key) != value:
            raise ValueError('Runtime value differs: ' + key)


def run(*args):
    return subprocess.check_output(args, text=True, timeout=240).strip()


def active(unit):
    return run('systemctl', 'show', unit, '--property=ActiveState', '--value') == 'active'


def stop_clean():
    run('systemctl', 'stop', 'rust.service')
    if active('rust.service') or run('systemctl', 'show', 'rust.service', '--property=Result', '--value') != 'success':
        raise RuntimeError('Unclean shutdown; state must remain untouched')


def write(path, text, mode=0o600):
    stat = path.stat() if path.exists() else None
    access.atomic_write(path, text, stat.st_uid if stat else os.getuid(), stat.st_gid if stat else os.getgid(), mode)


def archive_state(destination, stamp):
    target = destination / ('rust-' + stamp + '.tar.gz')
    temporary = target.with_suffix('.partial')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        with tarfile.open(fileobj=stream, mode='w:gz', dereference=False) as archive:
            archive.add(DATA, arcname='data')
    digest = backup.sha(temporary)
    backup.checked_archive(temporary, digest)  # Fully read every member before publication.
    temporary.rename(target)
    write(target.with_suffix('.gz.sha256'), digest + '  ' + target.name + '\n')
    backup.checked_archive(target, digest)
    return {'archive': str(target), 'sha256': digest}


def preflight(request, mount, destination):
    validate(request)
    maintenance.inspect()  # Verify deployed USB UUID and existing managed runtime.
    backup.mounted(mount)
    if mount.resolve() not in destination.resolve().parents:
        raise ValueError('Backup destination must be below NAS mount')
    if os.stat(mount).st_dev in (os.stat('/srv/rust').st_dev, os.stat('/').st_dev):
        raise ValueError('NAS must be separate from USB and root')
    destination.mkdir(parents=True, exist_ok=True)
    if os.stat(destination).st_dev != os.stat(mount).st_dev:
        raise ValueError('Destination escaped NAS filesystem')
    safe_tree(DATA)
    old = json.loads((DATA / 'deployment.json').read_text())
    if old['identity'] != request['identity']:
        raise ValueError('Identity differs from deployment')
    new = dict(old)
    if request['operation'] == 'new-world':
        new.update(seed=request['seed'], worldsize=request['worldsize'])
        world_files(DATA / old['identity'])
    unit_text = UNIT.read_text()
    effective = run('systemctl', 'cat', 'rust.service')
    starts = [line for line in effective.splitlines() if line.startswith('ExecStart=')]
    if starts != [line for line in unit_text.splitlines() if line.startswith('ExecStart=')]:
        raise ValueError('Service drop-in overrides require review')
    unit = update_unit(unit_text, old, new)
    if request.get('description') is not None and re.search(r'\+server\.description\b', unit, re.I):
        raise ValueError('Startup description override requires review')
    cfg = DATA / old['identity'] / 'cfg'
    reconcile((cfg / 'server.cfg').read_text(), config_values(request))
    previous = json.loads((DATA / 'access.json').read_text())
    desired = access.policy({'mode': 'preserve', 'admins': []}, previous)
    clear_admins((cfg / 'users.cfg').read_text() if (cfg / 'users.cfg').exists() else '', desired)
    required = sum(p.stat().st_size for p in DATA.rglob('*') if p.is_file())
    for path in (DATA.parent, destination):
        if shutil.disk_usage(path).free < required * 2 + 512 * 1024**2:
            raise ValueError('Insufficient space for archive/staging and recovery margin')
    if not active('rust.service'):
        raise ValueError('A running managed server is required')
    return old, new, unit


def prepare(stage, request, new):
    identity = stage / new['identity']
    removed = []
    if request['operation'] == 'new-world':
        for path in world_files(identity):
            removed.append(path.name)
            path.unlink()
    cfg = identity / 'cfg'
    write(cfg / 'server.cfg', reconcile((cfg / 'server.cfg').read_text(), config_values(request)))
    policy_path = stage / 'access.json'
    desired = access.policy({'mode': 'preserve', 'admins': []}, json.loads(policy_path.read_text()))
    users = cfg / 'users.cfg'
    write(users, clear_admins(users.read_text() if users.exists() else '', desired))
    # A newly created users.cfg must be owned by the game account, like cfg.
    os.chown(users, cfg.stat().st_uid, cfg.stat().st_gid)
    write(policy_path, json.dumps(desired, sort_keys=True) + '\n')
    new['gameplay'] = values(request)
    write(stage / 'deployment.json', json.dumps(new, indent=2) + '\n', 0o640)
    return removed


def execute(request, mount, destination):
    validate(request)  # No lock, directory or service mutation before explicit approval.
    with open('/run/lock/pi5-rust.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if STATE.exists():
            raise ValueError('Maintenance window exists; inspect interrupted job first')
        old, new, unit = preflight(request, mount, destination)
        STATE.mkdir(mode=0o700)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        previous = DATA.with_name('data.pre-gameplay-' + stamp)
        stage = DATA.with_name('.gameplay-' + stamp)
        record = {'operation': request['operation'], 'previous': str(previous),
                  'stage': str(stage), 'phase': 'preflight', 'old': old, 'new': new, 'paused': []}
        paused = []
        offline = False
        swapped = False
        original_unit = UNIT.read_text()
        write(STATE / 'rust.service.before', original_unit, 0o644)
        def journal(phase):
            record['phase'] = phase
            write(STATE / 'gameplay.json', json.dumps(record, indent=2) + '\n')
        try:
            for name in ('rust.service', 'rust-backup.service', 'rust-maintenance-check.service'):
                directory = UNIT.parent / (name + '.d')
                directory.mkdir(exist_ok=True)
                write(directory / 'maintenance-guard.conf',
                      '[Unit]\nConditionPathExists=!/var/lib/rustberrypi-maintenance/offline\n', 0o644)
            run('systemctl', 'daemon-reload')
            for name in PAUSED:
                if active(name):
                    paused.append(name)
                    record['paused'] = list(paused)
                    journal('pausing')
                    run('systemctl', 'stop', name)
            # Already running jobs must not overlap. They cannot acquire our lock.
            for name in ('rust-backup.service', 'rust-maintenance-check.service', 'rust-watchdog.service'):
                if active(name):
                    raise ValueError('Background operation still running: ' + name)
            for name in values(request):
                read_value(name)  # Reject absent/renamed convars on the installed build.
            verify({'server.seed': int(old['seed']), 'server.worldsize': int(old['worldsize'])})
            with redirect_stdout(sys.stderr):
                maintenance.notices(request['warning'], 'new world' if request['operation'] == 'new-world' else 'gameplay')
            write(STATE / 'offline', 'Guarded gameplay operation; see awx/GAMEPLAY.md\n')
            offline = True
            journal('stopping')
            stop_clean()  # SIGINT and successful systemd Result are required after server.save.
            safe_tree(DATA)
            require_current_save(DATA / old['identity'], old)
            record.update(archive_state(destination, stamp))
            journal('backup-verified')
            shutil.copytree(DATA, stage, copy_function=shutil.copy2)
            # copytree runs as root: preserve ownership for every original file/directory.
            for source in [DATA, *DATA.rglob('*')]:
                target = stage / source.relative_to(DATA)
                os.chown(target, source.stat().st_uid, source.stat().st_gid)
            blueprints = {str(p.relative_to(DATA)): backup.sha(p)
                          for p in DATA.rglob('player.blueprints*') if p.is_file()}
            record['removed'] = prepare(stage, request, new)
            if any(backup.sha(stage / name) != digest for name, digest in blueprints.items()):
                raise ValueError('Blueprint preservation failed')
            record['blueprints'] = blueprints
            journal('staged')
            DATA.rename(previous)
            try:
                stage.rename(DATA)
            except BaseException:
                previous.rename(DATA)
                raise
            swapped = True
            write(UNIT, unit, 0o644)
            run('systemctl', 'daemon-reload')
            journal('starting')
            (STATE / 'offline').unlink()
            run('systemctl', 'start', '--no-block', 'rust.service')
            # Re-arm the persistent guard as soon as systemd starts the process.
            # Conditions do not stop an active unit, but block unattended restarts.
            start_deadline = time.monotonic() + 30
            while not active('rust.service'):
                if time.monotonic() >= start_deadline:
                    raise RuntimeError('Rust did not enter active state')
                time.sleep(0.2)
            write(STATE / 'offline', 'Startup verification in progress.\n')
            health.wait_ready(request['timeout'], require_rcon=True)
            run('/usr/bin/python3', str(Path(__file__).with_name('access_runtime.py')))
            verify(values(request))
            verify_description(request)
            users = DATA / new['identity'] / 'cfg/users.cfg'
            desired = json.loads((DATA / 'access.json').read_text())
            if desired.get('admins') != [] or clear_admins(users.read_text(), desired) != users.read_text():
                raise ValueError('Administrator removal did not persist')
            verify({'server.seed': int(new['seed']), 'server.worldsize': int(new['worldsize'])})
            rcon('server.save')
            deadline = time.monotonic() + 60
            pattern = f"proceduralmap.{new['worldsize']}.{new['seed']}.*.sav"
            while not any(p.stat().st_size > 0 for p in (DATA / new['identity']).glob(pattern)):
                if time.monotonic() > deadline:
                    raise ValueError('No nonempty save for the requested world')
                time.sleep(2)
            journal('verified')
            evidence = DATA.parent / ('gameplay-' + stamp + '.json')
            write(evidence, json.dumps(record, indent=2) + '\n')
            shutil.rmtree(STATE)
            print(json.dumps({'verified': True, 'evidence': str(evidence), **record}))
        except BaseException:
            if offline:
                write(STATE / 'offline', 'Operation failed; recovery review required.\n')
                run('systemctl', 'stop', 'rust.service')
                if swapped:
                    DATA.rename(DATA.with_name('data.failed-gameplay-' + stamp))
                    previous.rename(DATA)
                    write(UNIT, original_unit, 0o644)
                    run('systemctl', 'daemon-reload')
                    journal('rolled-back-stopped')
                else:
                    journal('failed-stopped')
            else:
                shutil.rmtree(STATE)
            raise
        finally:
            for name in paused:
                run('systemctl', 'start', name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mount', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    execute(json.load(sys.stdin), args.mount, args.destination)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # RCON exceptions may embed credentials. Never print their messages.
        sys.exit('Gameplay operation failed: ' + type(error).__name__ + '; inspect guarded recovery state')
