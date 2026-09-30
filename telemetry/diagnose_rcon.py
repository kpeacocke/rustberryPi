#!/usr/bin/env python3
"""One local HTTP upgrade probe; never print credentials or request headers."""
import base64
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import time


def run(*args):
    return subprocess.check_output(args, text=True, timeout=15).strip()


def main():
    secret = Path('/etc/rustberrypi/rcon-password').read_text().strip()
    if len(secret) != 64 or any(c not in '0123456789abcdef' for c in secret):
        raise ValueError('Unexpected credential format')
    pid = run('systemctl', 'show', 'rust', '-p', 'MainPID', '--value')
    since = str(int(time.time()) - 1)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (f'GET /{secret} HTTP/1.1\r\nHost: 127.0.0.1:28016\r\n'
               'Upgrade: websocket\r\nConnection: Upgrade\r\n'
               f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
    stage = 'TCP connect'
    started = time.monotonic()
    try:
        with socket.create_connection(('127.0.0.1', 28016), timeout=15) as sock:
            print('TCP connected', flush=True)
            stage = 'HTTP upgrade'
            sock.sendall(request.encode('ascii'))
            reply = b''
            while b'\r\n\r\n' not in reply and len(reply) < 16384:
                part = sock.recv(4096)
                if not part:
                    break
                reply += part
            if not reply:
                print('Server closed without an HTTP response')
            else:
                header = reply.split(b'\r\n\r\n', 1)[0].decode('ascii', errors='replace')
                print('HTTP status:', header.split('\r\n')[0].replace(secret, '[REDACTED]'))
                expected = base64.b64encode(hashlib.sha1(
                    (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                fields = dict(line.split(':', 1) for line in header.split('\r\n')[1:] if ':' in line)
                fields = {name.lower(): value.strip() for name, value in fields.items()}
                print('Valid WebSocket accept:', fields.get('sec-websocket-accept') == expected)
                if header.startswith('HTTP/1.1 101'):
                    # Masked, empty close frame: do not deliberately reset the peer.
                    sock.sendall(b'\x88\x80' + os.urandom(4))
    except (OSError, ValueError) as error:
        print('Probe failed at', stage, ':', type(error).__name__)
    print('Elapsed seconds:', round(time.monotonic() - started, 2))
    time.sleep(2)
    logs = run('journalctl', '-b', '_PID=' + pid, '--since=@' + since, '-n', '200', '--no-pager')
    lines = logs.splitlines()
    selected = set()
    for index, line in enumerate(lines):
        if any(word in line.lower() for word in ('exception', 'rcon', 'websocket', 'handshake')):
            selected.update(range(max(0, index - 3), min(len(lines), index + 9)))
    print('Fresh server diagnostics (concurrent collector attempts may also appear):')
    if not selected:
        print('No matching journal messages')
    for index in sorted(selected):
        if 'command line:' not in lines[index].lower():
            print(lines[index].replace(secret, '[REDACTED]'))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Exception messages can contain sensitive paths or request data.
        print('Diagnostic could not complete:', type(error).__name__)
        raise SystemExit(1) from None
