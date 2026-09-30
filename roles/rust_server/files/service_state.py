#!/usr/bin/env python3
"""Serialize Ansible lifecycle changes with offline backups and updates."""
import fcntl
import subprocess
import sys


def main(action):
    if action not in {'start', 'stop', 'restart'}:
        raise ValueError('Unsupported service action')
    with open('/run/lock/pi5-rust.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = subprocess.run(['systemctl', 'is-active', '--quiet', 'rust.service']).returncode == 0
        if (action == 'start' and active) or (action == 'stop' and not active):
            return
        if action in {'start', 'restart'} and not active:
            failed = subprocess.run(['systemctl', 'is-failed', '--quiet', 'rust.service']).returncode == 0
            if failed:
                # Only an explicit deployment retry resets the crash/start limit.
                subprocess.run(['systemctl', 'reset-failed', 'rust.service'], check=True)
        subprocess.run(['systemctl', action, 'rust.service'], check=True)
        print('changed')


if __name__ == '__main__':
    main(sys.argv[1])
