"""Exercise live-process hangs, bounded recovery and post-backup health failures."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watchdog = load('watchdog', 'roles/rust_watchdog/files/watchdog.py')
backup = load('recovery_backup', 'roles/rust_backup/files/backup.py')
health = watchdog.health
CONFIG = {'startup_grace': 1800, 'failure_threshold': 3, 'max_restarts': 2,
          'require_rcon': True, 'storage_uuid': 'usb-uuid'}
SERVICE = {'ActiveState': 'active', 'InvocationID': 'first', 'MainPID': '123',
           'ActiveEnterTimestampMonotonic': '1'}


class WatchdogTests(unittest.TestCase):
    def ready_state(self):
        return watchdog.decide({}, SERVICE, True, 10000, 2000, CONFIG)[0]

    def fail_three(self, state, now=10000, age=2000):
        for n in range(3):
            state, action = watchdog.decide(state, SERVICE, False, now + n * 60, age, CONFIG)
        return state, action

    def test_live_process_hang_requires_three_failed_responses(self):
        state = self.ready_state()
        for n in range(3):
            state, action = watchdog.decide(state, SERVICE, False, 10000 + n * 60, 2000, CONFIG)
            self.assertEqual(action, 'restart' if n == 2 else None)

    def test_one_good_probe_clears_failure_streak(self):
        state, _ = watchdog.decide(self.ready_state(), SERVICE, False, 10000, 2000, CONFIG)
        state, _ = watchdog.decide(state, SERVICE, True, 10060, 2000, CONFIG)
        self.assertEqual(state['failures'], 0)

    def test_startup_grace_ends_early_after_readiness(self):
        state, action = self.fail_three({}, age=200)
        self.assertIsNone(action)
        self.assertEqual(state['status'], 'starting')
        state, _ = watchdog.decide(state, SERVICE, True, 10200, 200, CONFIG)
        self.assertEqual(self.fail_three(state, now=10300, age=300)[1], 'restart')

    def test_new_invocation_gets_grace_but_keeps_budget(self):
        state = self.ready_state()
        state['attempts'] = [9900]
        state, action = watchdog.decide(state, dict(SERVICE, InvocationID='second'), False, 10000, 10, CONFIG)
        self.assertIsNone(action)
        self.assertEqual(state['attempts'], [9900])
        self.assertFalse(state['seen_healthy'])

    def test_intentional_stops_and_transitions_are_not_restarted(self):
        for active in ('inactive', 'activating', 'deactivating'):
            state = self.ready_state()
            state['failures'] = 10
            state, action = watchdog.decide(state, dict(SERVICE, ActiveState=active), False, 10000, 2000, CONFIG)
            self.assertIsNone(action)

    def test_budget_latches_and_survives_time_and_reboot(self):
        state = self.ready_state()
        state['attempts'] = [9800, 9900]
        state, action = self.fail_three(state)
        self.assertEqual(action, 'capture')
        self.assertTrue(state['blocked'])
        state = json.loads(json.dumps(state))  # Persisted/reloaded across a host reboot.
        state, action = watchdog.decide(state, dict(SERVICE, InvocationID='after-reboot'), False, 20000, 2000, CONFIG)
        self.assertIsNone(action)
        self.assertTrue(state['blocked'])

    def test_slow_failed_starts_cannot_evade_budget(self):
        state = self.ready_state()
        state.update(attempts=[1000, 3000], unrecovered_attempts=2)
        state, action = self.fail_three(state, now=10000)
        self.assertEqual(action, 'capture')
        self.assertTrue(state['blocked'])

    def test_maintenance_skips_probes_and_actions(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(watchdog, 'MAINTENANCE', Path(folder)), \
                patch.object(health, 'probe') as probe:
            watchdog.check(CONFIG)
            probe.assert_not_called()

    def test_lock_busy_skips_probes_and_actions(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(watchdog, 'MAINTENANCE', Path(folder) / 'absent'), \
                patch.object(watchdog, 'LOCK', Path(folder) / 'lock'), \
                patch.object(watchdog.fcntl, 'flock', side_effect=BlockingIOError), \
                patch.object(health, 'probe') as probe:
            watchdog.check(CONFIG)
            probe.assert_not_called()

    def test_attempt_persisted_before_restart_and_reset_requires_health(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initial = self.ready_state()
            initial['failures'] = 2
            (root / 'state.json').write_text(json.dumps(initial))
            commands = []
            def run(command, **kwargs):
                commands.append(command)
                if 'restart' in command:
                    saved = json.loads((root / 'state.json').read_text())
                    self.assertEqual(len(saved['attempts']), 1)
                    self.assertEqual(saved['unrecovered_attempts'], 1)
                return SimpleNamespace(stdout='usb-uuid\n', returncode=0)
            with patch.object(watchdog, 'STATE', root / 'state.json'), \
                    patch.object(watchdog, 'LOCK', root / 'lock'), \
                    patch.object(watchdog, 'MAINTENANCE', root / 'absent'), \
                    patch.object(watchdog, 'capture'), \
                    patch.object(health, 'service_status', return_value=SERVICE), \
                    patch.object(health, 'probe', return_value=False), \
                    patch.object(watchdog.subprocess, 'run', side_effect=run):
                watchdog.check(CONFIG)
                self.assertIn(['systemctl', 'restart', 'rust.service'], commands)
                with self.assertRaises(RuntimeError):
                    watchdog.check(CONFIG, reset=True)

    def test_corrupt_state_does_not_reset_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            path.write_text('{broken')
            with patch.object(watchdog, 'STATE', path), self.assertRaises(ValueError):
                watchdog.read_state()


class RecoveryTests(unittest.TestCase):
    def test_backup_recovery_failure_preserves_archive_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'status.json'
            path.write_text(json.dumps({'backup_success': 123}))
            real_path = Path
            def mapped(value):
                return path if str(value) == '/var/lib/rustberrypi-backup.json' else real_path(value)
            with patch.object(backup, 'Path', side_effect=mapped), \
                    patch.object(backup.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0),
                                SimpleNamespace(returncode=1, stderr='readiness failed')]):
                with self.assertRaisesRegex(RuntimeError, 'recovery failed'):
                    backup.restart_and_verify(1800, True)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['backup_success'], 123)
            self.assertFalse(saved['recovery_ok'])

    def test_success_requires_health_helper_and_records_recovery(self):
        with patch.object(backup, 'record_status') as record, \
                patch.object(backup.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            backup.restart_and_verify(1800, True)
            self.assertIn('--rcon', run.call_args.args[0])
            self.assertTrue(record.call_args.kwargs['recovery_ok'])

    def test_bound_health_timeout_fails_recovery(self):
        with patch.object(backup, 'record_status') as record, \
                patch.object(backup.subprocess, 'run', side_effect=subprocess.TimeoutExpired('health', 1830)):
            with self.assertRaises(subprocess.TimeoutExpired):
                backup.restart_and_verify(1800, True)
            self.assertFalse(record.call_args.kwargs['recovery_ok'])

    def test_live_process_and_a2s_alone_do_not_pass_required_rcon(self):
        with patch.object(health, 'query', return_value=True), patch.object(health, 'rcon_ready', return_value=False):
            self.assertFalse(health.probe(require_rcon=True))
            self.assertTrue(health.probe(require_rcon=False))

    def test_rcon_errors_and_diagnostics_do_not_expose_credentials(self):
        with patch.object(health, 'query', return_value=True), \
                patch.object(health, 'rcon_ready', side_effect=RuntimeError('ws://secret')):
            self.assertFalse(health.probe(True))
        with patch.object(health.Path, 'read_text', return_value='secret'):
            text = health.redact('rcon.password secret\nws://host/secret\nargument secret\nnormal log')
        self.assertNotIn('secret', text)
        self.assertNotIn('ws://', text)
        self.assertIn('normal log', text)


if __name__ == '__main__':
    unittest.main()
