#!/usr/bin/env python3
"""Local, read-only telemetry. Browser input never becomes a command."""
import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import socket
import subprocess
import threading
import time

import psutil
import websocket


def run(argv):
    return subprocess.check_output(argv, text=True, timeout=5, stderr=subprocess.DEVNULL).strip()


def unit_status():
    text = run(['systemctl', 'show', 'rust.service', '--property=ActiveState,SubState,NRestarts,ActiveEnterTimestampMonotonic'])
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def a2s_ready():
    request = b'\xff\xff\xff\xffTSource Engine Query\x00'
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(('127.0.0.1', 28017))
            sock.send(request)
            reply = sock.recv(65535)
            if reply[:5] == b'\xff\xff\xff\xffA' and len(reply) == 9:
                sock.send(request + reply[5:])
                reply = sock.recv(65535)
            return reply[:5] == b'\xff\xff\xff\xffI' and len(reply) > 10
    except OSError:
        return False


def rcon_query(client, first_identifier=1):
    # Only these two fixed read-only commands can be issued by this service.
    result = {}
    for identifier, command in enumerate(('serverinfo', 'playerlist'), first_identifier):
        client.send(json.dumps({'Identifier': identifier, 'Message': command, 'Name': 'telemetry'}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            message = client.recv()
            if len(message) > 1024 * 1024:
                raise ValueError('Oversized RCON response')
            packet = json.loads(message)
            if packet.get('Identifier') == identifier:
                result[command] = json.loads(packet['Message'])
                break
        else:
            raise TimeoutError('RCON command timed out')
    return result


def rcon(password):
    with websocket.create_connection('ws://127.0.0.1:28016/' + password, timeout=3,
                                     http_no_proxy=['127.0.0.1']) as client:
        return rcon_query(client)


class RconUnavailable(Exception):
    """No fresh RCON sample during a connection failure or its retry interval."""


class RconSession:
    """Reuse successful connections; leave a quiet interval after any failure."""
    def __init__(self):
        self.client = None
        self.next_attempt = 0
        self.identifier = 1
        self.error = None

    def poll(self, password_file):
        if time.monotonic() < self.next_attempt:
            return None
        try:
            if self.client is None:
                password = Path(password_file).read_text().strip()
                self.client = websocket.create_connection(
                    'ws://127.0.0.1:28016/' + password, timeout=3,
                    http_no_proxy=['127.0.0.1'])
            identifier = self.identifier
            self.identifier += 2
            result = rcon_query(self.client, identifier)
            if not isinstance(result['serverinfo'], dict) or not isinstance(result['playerlist'], list):
                raise ValueError('Unexpected RCON response shape')
            self.error = None
            return result
        except Exception as error:
            category = type(error).__name__
            if category != self.error:
                logging.warning('Local RCON unavailable (%s); retry in 60 seconds', category)
            self.error = category
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    pass
            self.client = None
            self.next_attempt = time.monotonic() + 60
            return None


def public_players(rows, show_names):
    # Never pass IPs, Steam IDs or arbitrary RCON fields through to the browser.
    return [{'name': str(row.get('DisplayName', 'Player'))[:80] if show_names else 'Player ' + str(index + 1),
             'ping': row.get('Ping'), 'connected_seconds': row.get('ConnectedSeconds')}
            for index, row in enumerate(rows)]


def suggestions(data):
    items = []
    host = data.get('host', {})
    if host.get('disk_free_gib', 99) < 5:
        items.append(('urgent', 'USB space below 5 GiB', 'Make space before a Rust update.'))
    if (host.get('temperature') or 0) >= 80:
        items.append(('urgent', 'Pi temperature is high', 'Check airflow and cooling.'))
    flags = host.get('throttled')
    if flags is not None and flags & 1:
        items.append(('urgent', 'Undervoltage now', 'Check the power supply and cables.'))
    elif flags is not None and flags & 0x10000:
        items.append(('review', 'Undervoltage occurred since boot', 'Check power history; this is not necessarily current.'))
    if flags is not None and flags & 4:
        items.append(('urgent', 'CPU is throttling now', 'Check temperature and power.'))
    if host.get('swap_used_mib', 0) > 256:
        items.append(('review', 'Swap usage exceeds 256 MiB', 'Inspect memory pressure alongside server FPS.'))
    if int(data.get('service', {}).get('NRestarts', 0)) > 0:
        items.append(('review', 'Rust has automatically restarted', 'Inspect the Rust journal; the count is cumulative.'))
    maintenance = data.get('maintenance', {})
    for name in ('packages', 'rust'):
        check = maintenance.get(name, {})
        if not check.get('ok') or time.time() - check.get('checked_at', 0) > 86400:
            items.append(('review', name.title() + ' update status unknown or stale', 'Check the maintenance probe journal.'))
    packages = maintenance.get('packages', {})
    if packages.get('security_count', 0):
        items.append(('urgent', 'Security package updates available', 'Schedule OS maintenance and verify Rust afterwards.'))
    elif packages.get('count', 0):
        items.append(('review', 'Package updates available', 'Review packages during a maintenance window.'))
    rust = maintenance.get('rust', {})
    if rust.get('ok') and rust.get('available') != rust.get('installed'):
        items.append(('review', 'Rust update available', 'Back up and schedule downtime; check connected players first.'))
    if maintenance.get('reboot_required'):
        items.append(('review', 'OS requests a reboot', 'Schedule a reboot and verify the server returns.'))
    backup = maintenance.get('backup', {})
    if backup.get('service_result') not in (None, '', 'success'):
        items.append(('urgent', 'Backup service failed', 'Inspect journalctl -u rust-backup; a prior backup may still exist.'))
    if time.time() - backup.get('backup_success', 0) > 86400:
        items.append(('urgent', 'Backup missing or older than 24 hours', 'Run and verify an off-Pi backup.'))
    if not backup.get('restore_success'):
        items.append(('review', 'No restore success recorded', 'Test recovery on a spare system; do not overwrite the live world.'))
    return sorted([{'severity': a, 'title': b, 'detail': c} for a, b, c in items],
                  key=lambda item: item['severity'] != 'urgent')


class PlayerHistory:
    """Persist observed sessions for approved IDs; project names and dates only."""
    def __init__(self, allowed, names, path):
        self.allowed = list(dict.fromkeys(str(value) for value in allowed))
        self.names = names
        self.path = Path(path) if path else None
        self.records = {}
        self.online = set()
        self.last_write = 0
        self.error = None
        self.load_failed = False
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if data.get('version') != 1 or not isinstance(data.get('players'), dict):
                    raise ValueError('Unsupported player history')
                for key in self.allowed:
                    row = data['players'].get(key)
                    if isinstance(row, dict):
                        for field in ('last_login', 'last_seen'):
                            if row.get(field) is not None and not isinstance(row[field], (int, float)):
                                raise ValueError('Invalid history timestamp')
                        self.records[key] = row
            except (OSError, ValueError, TypeError) as error:
                self.error = type(error).__name__
                self.load_failed = True  # Preserve the unreadable file for recovery.

    def update(self, rows, now):
        online = set()
        for row in rows:
            key = str(row.get('SteamID'))
            if key not in self.allowed:
                continue
            online.add(key)
            record = self.records.setdefault(key, {})
            try:
                seconds = float(row.get('ConnectedSeconds'))
                login = now - seconds if 0 <= seconds <= now else None
            except (TypeError, ValueError):
                login = None
            if (key not in self.online or record.get('last_login') is None or
                    (login is not None and login > record['last_login'] + 10)):
                record['last_login'] = login
            record.update(name=str(row.get('DisplayName', 'Approved player'))[:80], last_seen=now)
        changed_session = online != self.online
        self.online = online
        if self.path and not self.load_failed and (changed_session or now - self.last_write >= 60):
            temporary = self.path.with_suffix('.tmp')
            try:
                temporary.write_text(json.dumps({'version': 1, 'players': self.records}))
                temporary.chmod(0o600)
                temporary.replace(self.path)
                self.last_write = now
                self.error = None
            except OSError as error:
                self.error = type(error).__name__

    def public(self, show_names, fresh):
        output = []
        for index, key in enumerate(self.allowed):
            record = self.records.get(key, {})
            fallback = 'Approved player ' + str(index + 1)
            output.append({'name': str(self.names.get(key) or record.get('name') or fallback)[:80] if show_names else fallback,
                           'last_login': record.get('last_login'), 'last_seen': record.get('last_seen'),
                           'status': ('online' if key in self.online else 'offline') if fresh else 'unknown'})
        return output


class Collector:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.snapshot = {'sampled_at': 0, 'status': 'Waiting for first sample'}
        self.history = deque(maxlen=120)
        self.events = deque(maxlen=12)
        self.previous_ids = None
        self.last_rcon = 0
        self.game = {}
        self.previous_io = None
        self.rcon_error = None
        self.rcon_session = RconSession()
        self.player_history = PlayerHistory(config.get('allowed_players', []), config.get('player_names', {}),
                                            config.get('player_history_file'))

    def collect(self):
        now = time.time()
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage('/srv/rust')
        host = {'cpu': psutil.cpu_percent(percpu=True), 'memory_percent': memory.percent,
                'memory_used_gib': (memory.total - memory.available) / 1024**3,
                'memory_total_gib': memory.total / 1024**3, 'swap_used_mib': swap.used / 1024**2,
                'disk_free_gib': disk.free / 1024**3, 'disk_percent': disk.percent,
                'uptime_seconds': now - psutil.boot_time()}
        try:
            host['temperature'] = int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000
        except (OSError, ValueError):
            host['temperature'] = None
        try:
            host['throttled'] = int(run(['vcgencmd', 'get_throttled']).split('=')[1], 16)
        except (OSError, ValueError, subprocess.SubprocessError):
            host['throttled'] = None
        network = psutil.net_io_counters()
        disk_io = psutil.disk_io_counters()
        current = (now, network.bytes_recv, network.bytes_sent, disk_io.read_bytes, disk_io.write_bytes)
        if self.previous_io:
            seconds = max(now - self.previous_io[0], 0.01)
            for index, key in enumerate(('rx_kib_s', 'tx_kib_s', 'read_kib_s', 'write_kib_s'), 1):
                host[key] = max(0, current[index] - self.previous_io[index]) / seconds / 1024
        self.previous_io = current
        try:
            lines = Path('/proc/net/wireless').read_text().splitlines()[2:]
            host['wifi_dbm'] = float(lines[0].split()[3].rstrip('.')) if lines else None
        except (OSError, ValueError, IndexError):
            host['wifi_dbm'] = None
        service = unit_status()
        try:
            reply = self.rcon_session.poll(self.config['password_file'])
            if reply is None:
                raise RconUnavailable()
            rows = reply['playerlist']
            info = reply['serverinfo']
            self.player_history.update(rows, now)
            current_ids = {str(row.get('SteamID')): str(row.get('DisplayName', 'Player'))[:80] for row in rows}
            if self.previous_ids is not None:
                for ids, action in ((current_ids.keys() - self.previous_ids.keys(), 'joined'),
                                    (self.previous_ids.keys() - current_ids.keys(), 'left')):
                    for player in ids:
                        name = (current_ids if action == 'joined' else self.previous_ids)[player]
                        self.events.appendleft({'at': now, 'text': (name if self.config['show_names'] else 'A player') + ' ' + action})
            self.previous_ids = current_ids
            self.last_rcon = now
            self.rcon_error = None
            self.game = {'players': public_players(rows, self.config['show_names']),
                         'fps': info.get('Framerate'), 'entities': info.get('EntityCount'),
                         'uptime_seconds': info.get('Uptime'), 'world_size': info.get('WorldSize')}
        except Exception as error:
            # Do not log errors containing the RCON URL/password or player addresses.
            category = self.rcon_session.error if isinstance(error, RconUnavailable) else type(error).__name__
            if category != self.rcon_error and not isinstance(error, RconUnavailable):
                logging.warning('Local RCON unavailable (%s)', category)
            self.rcon_error = category
            self.previous_ids = None
        maintenance = {}
        try:
            maintenance = json.loads(Path('/var/lib/rustberrypi/maintenance.json').read_text())
        except (OSError, ValueError):
            pass
        try:
            maintenance['backup'] = json.loads(Path('/var/lib/rustberrypi-backup.json').read_text())
        except (OSError, ValueError):
            pass
        try:
            maintenance.setdefault('backup', {})['service_result'] = run(
                ['systemctl', 'show', 'rust-backup.service', '--property=Result', '--value'])
        except (OSError, subprocess.SubprocessError):
            pass
        with socket.socket() as sock:
            sock.settimeout(0.2)
            companion = sock.connect_ex(('127.0.0.1', self.config['companion_port'])) == 0
        self.history.append({'at': now, 'cpu': sum(host['cpu']) / max(1, len(host['cpu'])),
                             'memory': memory.percent, 'temperature': host['temperature'], 'fps': self.game.get('fps') if self.last_rcon == now else None})
        data = {'sampled_at': now, 'rcon_at': self.last_rcon, 'rcon_error': self.rcon_error, 'game': self.game, 'host': host,
                'game_query_ready': a2s_ready(),
                'service': service, 'maintenance': maintenance, 'companion_listener': companion,
                'events': list(self.events), 'history': list(self.history)}
        data['suggestions'] = suggestions(data)
        data['allowed_players'] = self.player_history.public(self.config['show_names'], self.last_rcon == now)
        data['player_history_error'] = self.player_history.error
        with self.lock:
            self.snapshot = data

    def loop(self):
        while True:
            try:
                self.collect()
            except Exception:
                # Last sample retains its timestamp, so the UI reports staleness.
                pass
            time.sleep(5)


def handler(collector, assets, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
                self.send_error(403)
                return
            route = self.path.split('?', 1)[0]
            if route == '/api/status':
                with collector.lock:
                    body = json.dumps(collector.snapshot).encode()
                mime = 'application/json'
            elif route in ('/', '/game', '/system', '/touch', '/style.css', '/app.js'):
                file = {'/style.css': 'style.css', '/app.js': 'app.js'}.get(route, 'index.html')
                body = (assets / file).read_bytes()
                mime = {'style.css': 'text/css', 'app.js': 'text/javascript'}.get(file, 'text/html')
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/rustberrypi/telemetry.json')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    collector = Collector(config)
    threading.Thread(target=collector.loop, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', config['port']), handler(collector, Path(config['assets']), config['port']))
    server.serve_forever()
