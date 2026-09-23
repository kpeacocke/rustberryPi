#!/usr/bin/env python3
"""Serialize update/backup/restore; never stop a running server for an unchanged build."""
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def parse_vdf(text):
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    stream = iter(a if a else b for a, b in tokens)

    def obj():
        result = {}
        for key in stream:
            if key == '}':
                return result
            value = next(stream)
            result[key] = obj() if value == '{' else value
        return result
    return obj()


def installed_build(path):
    if not path.exists():
        return None
    state = parse_vdf(path.read_text())['AppState']
    return state.get('buildid') if state.get('StateFlags') == '4' else None


def public_build(output):
    match = re.search(r'"258550"\s*\{', output)
    if not match:
        raise ValueError('Steam app-info response did not contain app 258550')
    # Parse just this balanced object, ignoring Steam log lines after it.
    start = match.start()
    depth, quoted, escaped = 0, False, False
    for i in range(match.end() - 1, len(output)):
        char = output[i]
        if escaped:
            escaped = False
        elif char == '\\' and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted:
            depth += (char == '{') - (char == '}')
            if depth == 0:
                return parse_vdf(output[start:i + 1])['258550']['depots']['branches']['public']['buildid']
    raise ValueError('Truncated app-info response')


def main(rootfs, fex, uuid, mode):
    with open('/run/lock/pi5-rust.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mounted = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '--mountpoint', '/srv/rust'], text=True).strip()
        if mounted != uuid:
            raise ValueError('Persistent mount UUID mismatch')
        manifest = Path('/srv/rust/server/steamapps/appmanifest_258550.acf')
        current = installed_build(manifest)
        if current and Path('/srv/rust/server/RustDedicated').is_file() and mode == 'install':
            return
        env = dict(os.environ, HOME='/srv/rust/home', FEX_ROOTFS=rootfs)
        command = ['runuser', '-u', 'rust', '--', fex, '/bin/bash',
                   '/srv/rust/steamcmd/steamcmd.sh']

        def steam(args):
            p = subprocess.run(command + args, env=env, cwd='/srv/rust/steamcmd',
                               capture_output=True, text=True, timeout=7200)
            if p.returncode:
                raise RuntimeError(p.stdout[-4000:] + p.stderr[-4000:])
            return p.stdout

        desired = None
        if current and mode == 'update':
            desired = public_build(steam(['+login', 'anonymous', '+app_info_update', '1',
                                          '+app_info_print', '258550', '+quit']))
            if current == desired:
                return
        if shutil.disk_usage('/srv/rust').free < 5 * 1024**3:
            raise ValueError('At least 5 GiB free headroom required; larger USB may be needed')
        active = subprocess.run(['systemctl', 'is-active', '--quiet', 'rust.service']).returncode == 0
        if active:
            subprocess.run(['systemctl', 'stop', 'rust.service'], check=True)
            result = subprocess.check_output(['systemctl', 'show', '-p', 'Result', '--value', 'rust.service'], text=True).strip()
            if result != 'success':
                raise RuntimeError('Server did not stop cleanly; refusing update')
        # On failure deliberately leave it stopped; a partial install must not auto-restart.
        output = steam(['+force_install_dir', '/srv/rust/server', '+login', 'anonymous',
                        '+app_update', '258550', 'validate', '+quit'])
        after = installed_build(manifest)
        if not after or not Path('/srv/rust/server/RustDedicated').is_file() or "Success! App '258550'" not in output:
            raise RuntimeError('Steam did not confirm a complete installation: ' + output[-4000:])
        print('updated ' + after)
        # Ansible restarts after applying the current service configuration.


if __name__ == '__main__':
    main(*sys.argv[1:])
