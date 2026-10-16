"""Post-maintenance refresh stays outside rollback and requires fresh probe evidence."""
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceRefreshTests(unittest.TestCase):
    def tasks(self, role):
        return yaml.safe_load((ROOT / f'roles/{role}/tasks/main.yml').read_text())

    def test_gameplay_report_accepts_progress_before_final_json(self):
        report = self.tasks('rust_gameplay')[-1]
        with tempfile.TemporaryDirectory() as directory:
            playbook = Path(directory) / 'report.yml'
            playbook.write_text(yaml.safe_dump([{
                'hosts': 'localhost', 'gather_facts': False,
                'vars': {'rust_gameplay_result': {'stdout_lines': [
                    '{"connected_players": 2}', '{"verified": true, "phase": "verified"}']}},
                'tasks': [report],
            }]))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(playbook)],
                                    capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"verified": true', result.stdout)

    def test_gameplay_refresh_waits_for_transaction_exit(self):
        tasks = self.tasks('rust_gameplay')
        operation = next(i for i, task in enumerate(tasks) if 'ansible.builtin.command' in task)
        refresh = next(i for i, task in enumerate(tasks) if task.get('ansible.builtin.include_role', {}).get('name') == 'rust_maintenance_refresh')
        self.assertGreater(refresh, operation)
        self.assertGreater(tasks[operation]['poll'], 0)
        self.assertNotIn('ignore_errors', tasks[operation])

    def test_maintenance_refresh_is_outside_rescue_and_after_release(self):
        tasks = self.tasks('rust_maintenance')
        window = next(i for i, task in enumerate(tasks) if 'rescue' in task)
        refresh = next(i for i, task in enumerate(tasks) if task.get('ansible.builtin.include_role', {}).get('name') == 'rust_maintenance_refresh')
        self.assertGreater(refresh, window)
        self.assertEqual(tasks[window]['block'][-1]['ansible.builtin.file'],
                         {'path': '/var/lib/rustberrypi-maintenance', 'state': 'absent'})
        self.assertIn('ansible.builtin.fail', tasks[window]['rescue'][-1])

    def test_freshness_assertions_accept_success_and_reject_failed_or_stale_probes(self):
        assertion = self.tasks('rust_maintenance_refresh')[-1]
        with tempfile.TemporaryDirectory() as directory:
            playbook = Path(directory) / 'verify.yml'
            playbook.write_text(yaml.safe_dump([{
                'hosts': 'localhost', 'gather_facts': False, 'tasks': [assertion],
            }]))
            for failure in (None, 'failed', 'stale', 'old_summary'):
                evidence = {'checked_at': 101, **{name: {'ok': True, 'checked_at': 101}
                            for name in ('packages', 'rust', 'fex')}}
                if failure == 'failed':
                    evidence['rust']['ok'] = False
                elif failure == 'stale':
                    evidence['rust']['checked_at'] = 99
                elif failure == 'old_summary':
                    evidence['checked_at'] = 99
                variables = {
                    'rust_maintenance_refresh_started': {'stdout': '100'},
                    'rust_maintenance_refresh_evidence': {
                        'content': base64.b64encode(json.dumps(evidence).encode()).decode()},
                }
                result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local',
                                         str(playbook), '-e', json.dumps(variables)],
                                        capture_output=True, text=True, cwd=ROOT)
                with self.subTest(failure=failure):
                    self.assertEqual(result.returncode == 0, failure is None, result.stdout + result.stderr)

    def test_refresh_waits_and_verifies_each_probe_not_just_service_exit(self):
        tasks = self.tasks('rust_maintenance_refresh')
        self.assertEqual(tasks[1]['ansible.builtin.systemd_service'],
                         {'name': 'rust-maintenance-check.service', 'state': 'restarted'})
        self.assertGreater(tasks[1]['async'], 900)
        self.assertGreater(tasks[1]['poll'], 0)
        verify = tasks[-1]
        self.assertEqual(verify['loop'], ['packages', 'rust', 'fex'])
        conditions = ' '.join(verify['ansible.builtin.assert']['that'])
        self.assertIn('.ok', conditions)
        self.assertIn('.checked_at', conditions)
        self.assertIn('rust_maintenance_refresh_started', conditions)
        self.assertNotIn('rescue', verify)


if __name__ == '__main__':
    unittest.main()
