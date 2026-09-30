#!/usr/bin/env python3
"""Require an A2S_INFO response, including the optional challenge exchange."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
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
        ['systemctl', 'show', 'rust.service', '--property=ActiveState,SubState,Result,NRestarts,ExecMainStatus,MainPID,InvocationID,ActiveEnterTimestampMonotonic'],
        capture_output=True, text=True, timeout=10, check=True)
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def rcon_ready(password_file='/etc/rustberrypi/rcon-password'):
    # Import only when RCON is configured; non-telemetry deployments need no websocket package.
    import websocket
    password = Path(password_file).read_text().strip()
    if not re.fullmatch(r'[a-f0-9]{64}', password):
        raise ValueError('Invalid managed RCON credential')
    with closing(websocket.create_connection('ws://127.0.0.1:28016/' + password, timeout=3,
                                             http_no_proxy=['127.0.0.1'])) as client:
        client.send(json.dumps({'Identifier': 741, 'Message': 'serverinfo', 'Name': 'health'}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            client.settimeout(max(0.01, deadline - time.monotonic()))
            message = client.recv()
            if len(message) > 1024 * 1024:
                raise ValueError('Oversized RCON response')
            packet = json.loads(message)
            if packet.get('Identifier') == 741:
                info = json.loads(packet['Message'])
                return isinstance(info, dict) and isinstance(info.get('Uptime'), (int, float))
    return False


def probe(require_rcon=False):
    try:
        return query() and (not require_rcon or rcon_ready())
    except Exception:
        # Credential-bearing websocket exceptions must never enter logs or state.
        return False


def redact(text):
    try:
        secret = Path('/etc/rustberrypi/rcon-password').read_text().strip()
        if secret:
            text = text.replace(secret, '[redacted]')
    except OSError:
        pass
    return '\n'.join('[credential-bearing line omitted]' if re.search(r'rcon[._ ]?password|ws://', line, re.I)
                     else line for line in text.splitlines())


def diagnose(reason):
    lines = [reason]
    for command in [
        ['systemctl', 'show', 'rust.service', '--property=ActiveState,SubState,Result,NRestarts,MainPID,ExecMainStatus'],
        ['journalctl', '-u', 'rust.service', '-n', '100', '--no-pager'],
    ]:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            lines.append(result.stdout[-24000:] + result.stderr[-2000:])
        except (OSError, subprocess.SubprocessError) as error:
            lines.append('Could not collect diagnostics: ' + str(error))
    return redact('\n'.join(lines))


def wait_ready(timeout, require_rcon=False):
    deadline = time.monotonic() + timeout
    initial_restarts = None
    last_error = 'No valid A2S_INFO response'
    next_rcon = 0
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
                if require_rcon:
                    if time.monotonic() < next_rcon:
                        time.sleep(5)
                        continue
                    next_rcon = time.monotonic() + 60
                    if not rcon_ready():
                        last_error = 'No authenticated serverinfo response'
                        time.sleep(5)
                        continue
                # Do not mistake another UDP responder for a healthy stopped service.
                if service_status().get('ActiveState') == 'active':
                    return
        except OSError as error:
            last_error = type(error).__name__
        except Exception as error:
            last_error = type(error).__name__
        time.sleep(5)
    raise RuntimeError('Rust readiness timed out after ' + str(timeout) + ' seconds. Last probe: ' + last_error)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('timeout', type=int, nargs='?', default=1800)
    parser.add_argument('--rcon', action='store_true')
    args = parser.parse_args()
    try:
        wait_ready(args.timeout, args.rcon)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(diagnose(str(error)))
    print('Rust responds to A2S_INFO on UDP 28017')
