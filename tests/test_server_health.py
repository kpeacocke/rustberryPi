"""Advisory analysis must not mistake unavailable or startup data for health."""
import json
from pathlib import Path
import tempfile
import unittest

from telemetry.health import HealthMonitor, MIN_SAMPLES


def sample(at, **values):
    return {'schema': 1, 'at': at, 'active_state': 'active', 'rcon_ok': True,
            'a2s_ok': True, 'game_uptime_seconds': 900, 'player_count': 2,
            'entities': 1000, 'fps': 60, 'cpu_mean': 30, 'memory_percent': 50,
            'swap_used_mib': 0, 'temperature': 60, 'read_kib_s': 10,
            'write_kib_s': 10, 'a2s_latency_ms': 2, 'rcon_latency_ms': 10,
            'throttled_now': False, 'throttle_cpu_now': False,
            'watchdog_blocked': False, **values}


class ServerHealthTests(unittest.TestCase):
    def trained(self, path=None):
        monitor = HealthMonitor(path)
        for index in range(MIN_SAMPLES):
            monitor.update(sample(1000 + index * 5))
        return monitor

    def test_learning_requires_real_time_and_comparable_workload(self):
        monitor = self.trained()
        self.assertEqual(monitor.update(sample(2000))['status'], 'normal')
        self.assertEqual(monitor.update(sample(2005, player_count=3))['status'], 'learning')
        self.assertEqual(monitor.update(sample(2010, entities=7000))['status'], 'learning')

    def test_stale_rcon_and_startup_never_train(self):
        monitor = HealthMonitor()
        for row in [sample(1000, rcon_ok=False), sample(1005, a2s_ok=False),
                    sample(1010, game_uptime_seconds=20), sample(1015, fps=float('nan')),
                    sample(1020, player_count=None)]:
            self.assertEqual(monitor.update(row)['status'], 'unavailable')
        self.assertEqual(monitor.update(sample(1030), maintenance=True)['status'], 'maintenance')
        self.assertEqual(monitor.update(sample(1035))['samples'], 1)

    def test_persistence_and_sustained_evidence_not_single_spike(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'health.json'
            monitor = self.trained(path)
            self.assertEqual(monitor.update(sample(2000, fps=10))['status'], 'watching')
            for index in range(1, 6):
                result = monitor.update(sample(2000 + index * 5, fps=10, swap_used_mib=512))
            self.assertEqual(result['status'], 'review')
            self.assertTrue(any(item['metric'] == 'fps' for item in result['evidence']))
            restored = HealthMonitor(path)
            self.assertEqual(restored.update(sample(3000))['status'], 'normal')

    def test_gaps_duplicate_samples_and_missing_metrics_break_persistence(self):
        monitor = self.trained()
        for index in range(5):
            monitor.update(sample(2000 + index * 5, fps=10))
        self.assertEqual(monitor.update(sample(2100, fps=10))['status'], 'watching')
        self.assertEqual(monitor.update(sample(2100, fps=10))['status'], 'unavailable')
        result = monitor.update(sample(2105, fps=10, temperature=None))
        self.assertNotIn('temperature', [item['metric'] for item in result['evidence']])

    def test_corrupt_state_is_reported_and_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'health.json'
            path.write_text('broken')
            monitor = HealthMonitor(path)
            result = monitor.update(sample(1000))
            self.assertIsNotNone(result['storage_error'])
            self.assertEqual(path.read_text(), 'broken')

    def test_maintenance_comparison_requires_matching_context_and_new_samples(self):
        monitor = self.trained()
        event = {'version': 1, 'completed_at': 2000, 'target': 'rust'}
        for index in range(MIN_SAMPLES):
            result = monitor.update(sample(2005 + index * 5, fps=30), event=event)
        comparison = result['comparison']
        self.assertEqual(comparison['status'], 'review')
        self.assertEqual(comparison['target'], 'rust')
        fps = next(item for item in comparison['metrics'] if item['metric'] == 'fps')
        self.assertEqual((fps['before'], fps['after'], fps['change']), (60, 30, -30))
        self.assertEqual(monitor.update(sample(3000, player_count=4), event=event)['comparison']['status'],
                         'unmatched')

    def test_state_contains_no_raw_sample_or_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'health.json'
            self.trained(path)
            state = json.loads(path.read_text())
            self.assertEqual(state['version'], 1)
            self.assertNotIn('players', path.read_text())
            self.assertLess(path.stat().st_size, 1024 * 1024)

    def test_invalid_maintenance_evidence_is_visible_without_stopping_analysis(self):
        monitor = self.trained()
        result = monitor.update(sample(2000), event={'version': 1, 'target': 'rust'})
        self.assertEqual(result['status'], 'normal')
        self.assertEqual(result['maintenance_error'], 'ValueError')

    def test_next_maintenance_compares_against_recent_not_original_baseline(self):
        monitor = self.trained()
        first = {'version': 1, 'completed_at': 2000, 'target': 'rust'}
        for index in range(MIN_SAMPLES):
            monitor.update(sample(2005 + index * 5, fps=30), event=first)
        second = {'version': 1, 'completed_at': 3000, 'target': 'os'}
        for index in range(MIN_SAMPLES):
            result = monitor.update(sample(3005 + index * 5, fps=15), event=second)
        fps = next(item for item in result['comparison']['metrics'] if item['metric'] == 'fps')
        self.assertEqual((fps['before'], fps['after']), (30, 15))

    def test_missing_sensor_is_not_silently_reported_as_assessed(self):
        monitor = HealthMonitor()
        for index in range(MIN_SAMPLES + 1):
            result = monitor.update(sample(1000 + index * 5, temperature=None))
        self.assertEqual(result['status'], 'normal')
        self.assertIn('temperature', result['missing_metrics'])
        self.assertNotIn('temperature', result['covered_metrics'])
