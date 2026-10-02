#!/usr/bin/env python3
"""Restore approved skip-queue permissions after Rust finishes starting."""
from contextlib import closing
import json
from pathlib import Path
import re
import sys
import time

import websocket

from access import parse_ids
from health import query


def apply_access(verify_only=False, timeout=900):
    state = json.loads(Path('/srv/rust/data/access.json').read_text())
    if state['mode'] != 'restricted':
        return
    players = parse_ids(state['players'])
    if not 1 <= len(players) <= 5:
        raise ValueError('Invalid approved player policy')
    password = Path('/etc/rustberrypi/rcon-password').read_text().strip()
    if not re.fullmatch(r'[a-f0-9]{64}', password):
        raise ValueError('Invalid local RCON credential')

    deadline = time.monotonic() + timeout
    restored = 0
    last_error = 'Not ready'
    while True:
        try:
            if query():
                with closing(websocket.create_connection(
                    'ws://127.0.0.1:28016/' + password, timeout=5,
                    http_no_proxy=['127.0.0.1'])) as client:
                    for identifier, player in enumerate(players, 1):
                        client.send(json.dumps({
                            'Identifier': identifier,
                            'Message': f'global.skipqueueid {player} friend managed-access',
                            'Name': 'approved-access',
                        }))
                        response_deadline = time.monotonic() + 8
                        while True:
                            client.settimeout(max(0.1, response_deadline - time.monotonic()))
                            message = client.recv()
                            if len(message) > 1024 * 1024:
                                raise ValueError('Oversized RCON response')
                            packet = json.loads(message)
                            if packet.get('Identifier') == identifier:
                                break
                            if time.monotonic() >= response_deadline:
                                raise TimeoutError('RCON response timed out')
                        response = packet.get('Message', '')
                        if player not in response:
                            raise RuntimeError('Rust did not confirm approved player queue access')
                        if 'Added skip queue permission' in response:
                            restored += 1
                        elif 'already skip the queue' not in response:
                            raise RuntimeError('Rust rejected approved player queue access')
                if verify_only and restored:
                    raise RuntimeError(f'Restored {restored} missing queue grants; inspect rust-access.service')
                print(f'Approved queue access confirmed for {len(players)} players; restored {restored}')
                return
        except (OSError, websocket.WebSocketException, TimeoutError) as error:
            last_error = type(error).__name__
        if time.monotonic() >= deadline:
            raise RuntimeError('Rust game query or authenticated RCON unavailable: ' + last_error)
        time.sleep(5)


if __name__ == '__main__':
    try:
        apply_access(verify_only='--verify-only' in sys.argv[1:])
    except (OSError, ValueError, RuntimeError, websocket.WebSocketException) as error:
        # Only locally constructed RuntimeError messages are safe to print.
        reason = str(error) if type(error) is RuntimeError else type(error).__name__
        sys.exit('Could not confirm approved player queue access: ' + reason)
