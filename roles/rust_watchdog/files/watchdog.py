#!/usr/bin/env python3
"""Bounded recovery of a live but unresponsive Rust process."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

# Installed beside the existing health helper, not beside this source file.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'rust_server/files'))
import health

CONFIG = Path('/etc/rustberrypi-watchdog.json')
STATE = Path('/var/lib/rustberrypi-watchdog.json')
MAINTENANCE = Path('/var/lib/rustberrypi-maintenance')
LOCK = Path('/run/lock/pi5-rust.lock')
DIAGNOSTICS = Path('/var/log/rustberrypi-watchdog')


def save(path, data, mode=0o644):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.fchmod(stream.fileno(), mode)
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_state():
    try:
        state = json.loads(STATE.read_text())
    except FileNotFoundError:
        return {'attempts': [], 'failures': 0, 'blocked': False}
    # Corrupt state must not silently reset a recovery budget.
    if not isinstance(state, dict) or not isinstance(state.get('attempts'), list):
        raise ValueError('Invalid watchdog state; inspect before resetting')
    if any(not isinstance(value, (int, float)) for value in state['attempts']):
        raise ValueError('Invalid recovery timestamps')
    return state


def decide(state, service, healthy, now, age, config):
    state = dict(state)
    state['attempts'] = [stamp for stamp in state.get('attempts', []) if stamp > now - 3600]
    state['checked_at'] = now
    invocation = service.get('InvocationID', '')
    if invocation != state.get('invocation'):
        state.update(invocation=invocation, failures=0, seen_healthy=False)
    active = service.get('ActiveState')
    if active not in {'active', 'failed'}:
        state.update(status='stopped', failures=0)
        return state, None  # Never undo an intentional stop or an in-progress start/stop.
    if healthy and active == 'active':
        state.update(status='healthy', failures=0, seen_healthy=True, healthy_at=now, unrecovered_attempts=0)
        return state, None
    if state.get('blocked'):
        state.update(status='exhausted')
        return state, None
    if active == 'active' and not state.get('seen_healthy') and age < config['startup_grace']:
        state.update(status='starting', failures=0)
        return state, None
    state['failures'] = state.get('failures', 0) + 1
    state['status'] = 'unresponsive'
    if state['failures'] < config['failure_threshold']:
        return state, None
    if (len(state['attempts']) >= config['max_restarts'] or
            state.get('unrecovered_attempts', 0) >= config['max_restarts']):
        state.update(status='exhausted', blocked=True)
        return state, 'capture'
    return state, 'restart'


def capture(state):
    DIAGNOSTICS.mkdir(mode=0o700, parents=True, exist_ok=True)
    DIAGNOSTICS.chmod(0o700)
    text = health.diagnose('Rust watchdog: ' + state['status'])
    pid = health.service_status().get('MainPID', '0')
    if pid.isdigit() and int(pid) > 0:
        result = subprocess.run(['ps', '-L', '-p', pid, '-o', 'pid,tid,stat,pcpu,wchan:28,comm'],
                                text=True, capture_output=True, timeout=10, check=False)
        text += '\n' + result.stdout
    target = DIAGNOSTICS / (str(time.time_ns()) + '.log')
    with target.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(health.redact(text))
    # Bound diagnostics storage; this directory contains only this helper's reports.
    for old in sorted(DIAGNOSTICS.glob('*.log'))[:-10]:
        old.unlink()
    state['diagnostic_at'] = time.time()


def check(config, reset=False):
    # The maintenance directory covers countdown, updates, restore and health verification.
    if MAINTENANCE.exists():
        if reset:
            raise RuntimeError('Cannot reset recovery limits while maintenance is active')
        return
    with LOCK.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if reset:
                raise RuntimeError('Cannot reset recovery limits while another operation is active')
            return  # Backups, updates and lifecycle operations own this lock.
        if MAINTENANCE.exists():
            if reset:
                raise RuntimeError('Cannot reset recovery limits while maintenance is active')
            return
        mount = subprocess.run(['findmnt', '-n', '-o', 'UUID', '--mountpoint', '/srv/rust'],
                               capture_output=True, text=True, timeout=10, check=True)
        if mount.stdout.strip() != config['storage_uuid']:
            raise ValueError('Storage UUID mismatch; recovery refused')
        state = read_state()
        service = health.service_status()
        healthy = service.get('ActiveState') == 'active' and health.probe(config['require_rcon'])
        if reset:
            if not healthy:
                raise RuntimeError('Repair and verify the game before resetting recovery limits')
            state = {'attempts': [], 'failures': 0, 'blocked': False}
        age = max(0, time.monotonic() - int(service.get('ActiveEnterTimestampMonotonic', '0')) / 1000000)
        state, action = decide(state, service, healthy, time.time(), age, config)
        save(STATE, state)
        if action is None:
            return
        capture(state)
        save(STATE, state)
        # Recheck ownership and service generation after probes/diagnostics.
        current = health.service_status()
        if (MAINTENANCE.exists() or current.get('InvocationID', '') != service.get('InvocationID', '') or
                current.get('ActiveState') not in {'active', 'failed'}):
            return
        if action == 'capture':
            print('Rust recovery budget exhausted; repair then run watchdog.py --reset', file=sys.stderr)
            return
        state['attempts'].append(time.time())
        state.update(status='restarting', failures=0, seen_healthy=False,
                     unrecovered_attempts=state.get('unrecovered_attempts', 0) + 1)
        save(STATE, state)  # Charge the attempt before doing anything; survives helper/host failure.
        try:
            subprocess.run(['systemctl', 'reset-failed', 'rust.service'], check=True, timeout=10)
            subprocess.run(['systemctl', 'restart', 'rust.service'], check=True, timeout=240)
        except (OSError, subprocess.SubprocessError):
            state['status'] = 'restart-failed'
            save(STATE, state)
            raise RuntimeError('Watchdog restart failed; inspect systemd journal') from None
        print('Rust watchdog requested recovery; readiness will be checked on subsequent probes')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reset', action='store_true')
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text())
    if not 1 <= config['max_restarts'] <= 2 or not 1 <= config['failure_threshold'] <= 10:
        raise ValueError('Invalid watchdog recovery limits')
    if not 60 <= config['startup_grace'] <= 3600 or not isinstance(config['require_rcon'], bool):
        raise ValueError('Invalid watchdog health configuration')
    check(config, args.reset)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Rust watchdog failed: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
