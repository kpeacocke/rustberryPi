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
    while True:
        try:
            if query():
                client = websocket.create_connection(
                    'ws://127.0.0.1:28016/' + password, timeout=5,
                    http_no_proxy=['127.0.0.1'])
                break
        except (OSError, websocket.WebSocketException):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('Rust game query and authenticated RCON did not become ready')
        time.sleep(5)

    added = 0
    with closing(client):
        for identifier, player in enumerate(players, 1):
            client.send(json.dumps({
                'Identifier': identifier,
                'Message': f'global.skipqueueid {player} friend managed-access',
                'Name': 'approved-access',
            }))
            packet = json.loads(client.recv())
            message = packet.get('Message', '')
            if packet.get('Identifier') != identifier or player not in message:
                raise RuntimeError('Rust did not confirm approved player queue access')
            if 'Added skip queue permission' in message:
                added += 1
            elif 'already skip the queue' not in message:
                raise RuntimeError('Rust rejected approved player queue access')
    if verify_only and added:
        raise RuntimeError(f'Restored {added} missing queue grants; inspect rust-access.service')
    print(f'Approved queue access confirmed for {len(players)} players; restored {added}')


if __name__ == '__main__':
    try:
        apply_access(verify_only='--verify-only' in sys.argv[1:])
    except (OSError, ValueError, RuntimeError, websocket.WebSocketException):
        # Never print a credential-bearing websocket exception or player IDs.
        sys.exit('Could not confirm approved player queue access')
