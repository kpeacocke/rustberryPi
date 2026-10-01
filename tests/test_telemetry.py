"""Read-only boundary, privacy, staleness and fixed RCON command tests."""
import importlib.util
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('telemetry_server', ROOT / 'telemetry/server.py')
server = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {'psutil': MagicMock(), 'websocket': MagicMock()}):
    SPEC.loader.exec_module(server)


class TelemetryTests(unittest.TestCase):
    def test_exhausted_recovery_is_urgent_even_after_manual_recovery(self):
        suggestions = server.suggestions({'watchdog': {'blocked': True, 'status': 'healthy'}})
        self.assertTrue(any(item['severity'] == 'urgent' and item['title'] == 'Automatic recovery exhausted'
                            for item in suggestions))

    def test_archive_success_does_not_hide_game_recovery_failure(self):
        suggestions = server.suggestions({'maintenance': {'backup': {'backup_success': time.time(),
                                                                   'recovery_ok': False}}})
        self.assertTrue(any(item['title'] == 'Rust recovery after backup failed' for item in suggestions))

    def test_capacity_counts_five_unique_approved_friends_not_zero_public_slots(self):
        roster = ['one', 'two', 'three', 'four', 'five']
        self.assertEqual(server.player_capacity({'access_mode': 'restricted', 'allowed_players': roster,
                                                'public_capacity': 4}), {'mode': 'restricted', 'capacity': 5})
        self.assertEqual(server.player_capacity({'access_mode': 'public', 'public_capacity': 5}),
                         {'mode': 'public', 'capacity': 5})
        self.assertIsNone(server.player_capacity({})['capacity'])

    def test_history_persists_login_and_never_projects_steam_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'players.json'
            history = server.PlayerHistory(['approved', 'unseen'], {}, path)
            history.update([{'SteamID': 'approved', 'DisplayName': '<script>name</script>',
                             'ConnectedSeconds': 30}, {'SteamID': 'unapproved', 'DisplayName': 'hidden'}], 1000)
            self.assertEqual(history.public(True, True)[0]['last_login'], 970)
            self.assertEqual(history.public(True, True)[1]['last_login'], None)
            self.assertNotIn('hidden', path.read_text())
            restored = server.PlayerHistory(['approved', 'unseen'], {}, path)
            self.assertEqual(restored.public(True, False)[0]['last_login'], 970)
            self.assertEqual(restored.public(True, False)[0]['status'], 'unknown')
            restored.update([], 1100)
            self.assertEqual(restored.public(True, True)[0]['status'], 'offline')
            self.assertEqual(restored.public(True, True)[0]['last_seen'], 1000)
            self.assertNotIn('SteamID', json.dumps(restored.public(True, True)))
            self.assertNotIn('<script>', json.dumps(restored.public(False, True)))
            self.assertEqual(server.PlayerHistory([], {}, path).public(True, True), [])

    def test_history_reconnect_and_missing_duration_are_honest(self):
        history = server.PlayerHistory(['one'], {}, None)
        history.update([{'SteamID': 'one', 'ConnectedSeconds': 100}], 1000)
        history.update([{'SteamID': 'one', 'ConnectedSeconds': 2}], 1010)
        self.assertEqual(history.public(True, True)[0]['last_login'], 1008)
        history.update([], 1020)
        history.update([{'SteamID': 'one'}], 1030)
        self.assertIsNone(history.public(True, True)[0]['last_login'])

    def test_corrupt_history_is_preserved_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'players.json'
            path.write_text('corrupt original')
            history = server.PlayerHistory(['one'], {}, path)
            history.update([{'SteamID': 'one'}], 1000)
            self.assertEqual(path.read_text(), 'corrupt original')
            self.assertIsNotNone(history.error)

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

    def test_persistent_rcon_reuses_connection_and_unique_identifiers(self):
        client = MagicMock()
        client.recv.side_effect = [json.dumps({'Identifier': i, 'Message': '{}' if i % 2 else '[]'})
                                  for i in range(1, 5)]
        session = server.RconSession()
        with patch.object(server.Path, 'read_text', return_value='private'), \
                patch.object(server.websocket, 'create_connection', return_value=client) as connect:
            self.assertIsNotNone(session.poll('credential'))
            self.assertIsNotNone(session.poll('credential'))
            connect.assert_called_once()
        self.assertEqual([json.loads(call.args[0])['Identifier'] for call in client.send.call_args_list], [1, 2, 3, 4])

    def test_rcon_failure_closes_connection_and_waits_before_retry(self):
        client = MagicMock()
        client.recv.side_effect = ConnectionResetError('sensitive URL must not be logged')
        session = server.RconSession()
        with patch.object(server.Path, 'read_text', return_value='private'), \
                patch.object(server.websocket, 'create_connection', return_value=client) as connect, \
                patch.object(server.time, 'monotonic', return_value=100), \
                self.assertLogs(level='WARNING') as logs:
            self.assertIsNone(session.poll('credential'))
            self.assertIsNone(session.poll('credential'))
            connect.assert_called_once()
            client.close.assert_called_once()
            self.assertEqual(session.next_attempt, 160)
            self.assertNotIn('sensitive', ''.join(logs.output))
        with patch.object(server.Path, 'read_text', return_value='private'), \
                patch.object(server.websocket, 'create_connection', side_effect=ConnectionResetError) as connect, \
                patch.object(server.time, 'monotonic', return_value=161):
            self.assertIsNone(session.poll('credential'))
            connect.assert_called_once()

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


class FeatureCaptureTests(unittest.TestCase):
    """The sample archive feeds later offline analysis, so its schema must stay frozen."""

    SAMPLE = {'sampled_at': 1700000000.0, 'rcon_at': 1700000000.0, 'game_query_ready': True,
              'companion_listener': True,
              'host': {'cpu': [10.0, 20.0, 30.0, 40.0], 'memory_percent': 55.5, 'swap_used_mib': 12.0,
                       'temperature': 61.5, 'throttled': 0x50005, 'disk_percent': 71.0,
                       'disk_free_gib': 8.5, 'read_kib_s': 1.0, 'write_kib_s': 2.0,
                       'rx_kib_s': 3.0, 'tx_kib_s': 4.0, 'wifi_dbm': -52.0, 'uptime_seconds': 3600.0},
              'game': {'players': [{'id': '7656119'}, {'id': '7656120'}], 'fps': 58.0, 'entities': 1234,
                       'uptime_seconds': 900.0},
              'service': {'ActiveState': 'active', 'SubState': 'running', 'NRestarts': '2'},
              'maintenance': {'backup': {'backup_success': 1699996400.0, 'recovery_ok': True}},
              'watchdog': {'blocked': False, 'status': 'healthy', 'failures': 0},
              'suggestions': [{'severity': 'urgent'}, {'severity': 'advice'}]}

    def test_row_captures_latency_and_decodes_throttle_flags(self):
        row = server.feature_row(self.SAMPLE, {'a2s': 3.5, 'rcon': 12.25})
        self.assertEqual(row['schema'], server.FEATURE_SCHEMA)
        self.assertEqual((row['a2s_latency_ms'], row['rcon_latency_ms']), (3.5, 12.25))
        self.assertEqual((row['cpu_mean'], row['cpu_max']), (25.0, 40.0))
        self.assertEqual(row['n_restarts'], 2.0)
        self.assertEqual(row['backup_age_s'], 3600.0)
        self.assertEqual((row['suggestion_urgent'], row['suggestion_total']), (1, 2))
        self.assertTrue(row['throttled_now'] and row['throttled_since_boot'] and row['throttle_cpu_now'])
        self.assertFalse(row['capped_now'])

    def test_row_records_counts_only_and_never_player_identity(self):
        row = server.feature_row(self.SAMPLE, {})
        self.assertEqual(row['player_count'], 2)
        self.assertNotIn('7656119', json.dumps(row))
        self.assertTrue(all(value is None or isinstance(value, (int, float, bool, str))
                            for value in row.values()))

    def test_stale_rcon_reports_unknown_rather_than_a_stale_player_count(self):
        row = server.feature_row(dict(self.SAMPLE, rcon_at=1699999000.0), {})
        self.assertIsNone(row['player_count'])
        self.assertFalse(row['rcon_ok'])

    def test_missing_sample_fields_degrade_to_unknown(self):
        row = server.feature_row({}, {})
        self.assertEqual(row['schema'], server.FEATURE_SCHEMA)
        self.assertIsNone(row['temperature'])
        self.assertIsNone(row['throttled_now'])
        self.assertIsNone(row['active_state'])

    def test_unexpected_service_state_is_not_recorded_as_a_known_value(self):
        row = server.feature_row({'service': {'ActiveState': 'surprise'}}, {})
        self.assertIsNone(row['active_state'])

    def test_rows_append_to_a_private_daily_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'features'
            writer = server.FeatureWriter(target)
            writer.write(server.feature_row(self.SAMPLE, {}))
            writer.write(server.feature_row(self.SAMPLE, {}))
            files = writer.files()
            self.assertEqual(len(files), 1)
            self.assertEqual(len(files[0].read_text().strip().splitlines()), 2)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            self.assertIsNone(writer.error)

    def test_archive_is_pruned_to_the_retention_window(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for day in range(1, 6):
                (target / f'features-2024-01-0{day}.jsonl').write_text('{}\n')
            writer = server.FeatureWriter(target, retain_days=2)
            writer.prune()
            self.assertEqual([path.name for path in writer.files()],
                             ['features-2024-01-04.jsonl', 'features-2024-01-05.jsonl'])

    def test_archive_is_pruned_to_the_byte_ceiling_without_losing_the_live_day(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for day in range(1, 4):
                (target / f'features-2024-01-0{day}.jsonl').write_text('x' * 2 * 1024 * 1024)
            writer = server.FeatureWriter(target, max_bytes=1024 * 1024)
            writer.prune()
            self.assertEqual([path.name for path in writer.files()], ['features-2024-01-03.jsonl'])

    def test_unwritable_archive_reports_the_fault_instead_of_stalling_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / 'file'
            blocked.write_text('not a directory')
            writer = server.FeatureWriter(blocked / 'features')
            writer.write(server.feature_row(self.SAMPLE, {}))
            self.assertIsNotNone(writer.error)

    def test_capture_can_be_disabled_entirely(self):
        writer = server.FeatureWriter(None)
        writer.write(server.feature_row(self.SAMPLE, {}))
        self.assertIsNone(writer.error)
