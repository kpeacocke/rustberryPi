#!/usr/bin/env python3
"""Require an A2S_INFO response, including the optional challenge exchange."""
import socket
import subprocess
import sys
import time


def query(host='127.0.0.1', port=28017):
    request = b'\xff\xff\xff\xffTSource Engine Query\x00'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.connect((host, port))
        sock.send(request)
        response = sock.recv(65535)
        if response[:5] == b'\xff\xff\xff\xffA' and len(response) == 9:
            sock.send(request + response[5:])
            response = sock.recv(65535)
        return response[:5] == b'\xff\xff\xff\xffI' and len(response) > 10


def service_status():
    result = subprocess.run(
        ['systemctl', 'show', 'rust.service', '--property=ActiveState,SubState,Result,NRestarts,ExecMainStatus'],
        capture_output=True, text=True, timeout=10, check=True)
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def diagnose(reason):
    lines = [reason]
    for command in [
        ['systemctl', 'status', 'rust.service', '--no-pager', '-l'],
        ['journalctl', '-u', 'rust.service', '-n', '100', '--no-pager'],
    ]:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            lines.append(result.stdout[-24000:] + result.stderr[-2000:])
        except (OSError, subprocess.SubprocessError) as error:
            lines.append('Could not collect diagnostics: ' + str(error))
    return '\n'.join(lines)


def wait_ready(timeout):
    deadline = time.monotonic() + timeout
    initial_restarts = None
    last_error = 'No valid A2S_INFO response'
    while time.monotonic() < deadline:
        state = service_status()
        if state.get('ActiveState') in {'failed', 'inactive', 'deactivating'}:
            raise RuntimeError('Rust service is not running: ' + str(state))
        restarts = int(state.get('NRestarts', '0'))
        if initial_restarts is None:
            initial_restarts = restarts
        if restarts - initial_restarts >= 3:
            raise RuntimeError('Rust repeatedly restarted during readiness checks: ' + str(state))
        try:
            if query():
                # Do not mistake another UDP responder for a healthy stopped service.
                if service_status().get('ActiveState') == 'active':
                    return
        except OSError as error:
            last_error = str(error)
        time.sleep(5)
    raise RuntimeError('Rust readiness timed out after ' + str(timeout) + ' seconds. Last probe: ' + last_error)


if __name__ == '__main__':
    try:
        wait_ready(int(sys.argv[1] if len(sys.argv) > 1 else 1800))
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(diagnose(str(error)))
    print('Rust responds to A2S_INFO on UDP 28017')
