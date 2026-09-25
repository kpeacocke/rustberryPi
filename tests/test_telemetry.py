"""Read-only boundary, privacy, staleness and fixed RCON command tests."""
import importlib.util
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('telemetry_server', ROOT / 'telemetry/server.py')
server = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {'psutil': MagicMock(), 'websocket': MagicMock()}):
    SPEC.loader.exec_module(server)


class TelemetryTests(unittest.TestCase):
    def test_player_projection_never_exposes_addresses_or_ids(self):
        rows = [{'DisplayName': '<script>name</script>', 'SteamID': 'private', 'Address': 'private',
                 'Ping': 21, 'ConnectedSeconds': 12}]
        result = server.public_players(rows, True)[0]
        self.assertEqual(set(result), {'name', 'ping', 'connected_seconds'})
        self.assertEqual(server.public_players(rows, False)[0]['name'], 'Player 1')

    def test_missing_sensors_do_not_break_suggestions(self):
        result = server.suggestions({'host': {'temperature': None, 'throttled': None}})
        self.assertTrue(any('unknown' in item['title'] for item in result))

    def test_security_and_stale_checks_are_distinct(self):
        result = server.suggestions({'maintenance': {'packages': {'ok': False, 'checked_at': 1, 'security_count': 2}}})
        self.assertTrue(any('unknown' in item['title'] for item in result))
        self.assertTrue(any('Security' in item['title'] for item in result))

    def test_current_and_historical_undervoltage_distinguished(self):
        now = server.suggestions({'host': {'throttled': 1}})
        old = server.suggestions({'host': {'throttled': 0x10000}})
        self.assertTrue(any(item['title'] == 'Undervoltage now' for item in now))
        self.assertTrue(any('since boot' in item['title'] for item in old))

    def test_no_false_rust_update_when_check_failed(self):
        result = server.suggestions({'maintenance': {'rust': {'ok': False, 'installed': '1', 'available': '2'}}})
        self.assertFalse(any(item['title'] == 'Rust update available' for item in result))

    def test_rcon_only_issues_two_fixed_commands(self):
        client = MagicMock()
        client.recv.side_effect = [json.dumps({'Identifier': 0, 'Message': 'unsolicited log'}),
                                  json.dumps({'Identifier': 1, 'Message': '{"Framerate":30}'}),
                                  json.dumps({'Identifier': 2, 'Message': '[]'})]
        connection = MagicMock()
        connection.__enter__.return_value = client
        with patch.object(server.websocket, 'create_connection', return_value=connection):
            result = server.rcon('test-secret')
        self.assertEqual(result['playerlist'], [])
        self.assertEqual([json.loads(call.args[0])['Message'] for call in client.send.call_args_list],
                         ['serverinfo', 'playerlist'])

    def test_http_read_only_and_host_allowlist(self):
        collector = SimpleNamespace(lock=threading.Lock(), snapshot={'sampled_at': time.time()})
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.handler(collector, ROOT / 'dashboard', 0))
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = server.handler(collector, ROOT / 'dashboard', port)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            for method, path, host, expected in [('GET', '/api/status', f'127.0.0.1:{port}', 200),
                                               ('GET', '/api/status', 'attacker.example', 403),
                                               ('POST', '/api/status', f'127.0.0.1:{port}', 501),
                                               ('GET', '/../../etc/passwd', f'127.0.0.1:{port}', 404)]:
                connection = http.client.HTTPConnection('127.0.0.1', port)
                connection.request(method, path, headers={'Host': host})
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
                connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()
