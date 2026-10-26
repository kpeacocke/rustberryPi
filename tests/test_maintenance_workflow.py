"""Maintenance announcements and independently verified backup boundaries."""
import ast
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('maintenance_control', ROOT / 'maintenance/control.py')
control = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {'websocket': MagicMock()}):
    SPEC.loader.exec_module(control)


class MaintenanceTests(unittest.TestCase):
    def test_telemetry_only_deploy_discovers_versioned_game_fex(self):
        defaults = yaml.safe_load(
            (ROOT / 'roles/rust_telemetry/defaults/main.yml').read_text()
        )
        tasks = yaml.safe_load(
            (ROOT / 'roles/rust_telemetry/tasks/main.yml').read_text()
        )
        extraction = next(
            task['ansible.builtin.set_fact']
            ['rust_telemetry_deployed_fex_paths']
            for task in tasks
            if task['name'] ==
            'Extract FEX selected by the deployed game service'
        )
        env = Environment(undefined=StrictUndefined)
        env.filters['b64decode'] = lambda text: base64.b64decode(text).decode()
        env.filters['regex_findall'] = lambda text, pattern: re.findall(
            pattern, text
        )

        for fex in ('/usr/bin/FEX', '/opt/fex/' + 'a' * 40 + '/bin/FEX'):
            with self.subTest(fex=fex):
                unit = (
                    '[Service]\nExecStart=/usr/bin/python3 /helper/launch.py '
                    + fex + ' /srv/rust/server/RustDedicated -batchmode\n'
                )
                encoded = base64.b64encode(unit.encode()).decode()
                paths = ast.literal_eval(env.from_string(extraction).render(
                    rust_telemetry_game_unit={'content': encoded}
                ))
                self.assertEqual(paths, [fex])
                actual = env.from_string(defaults['rust_telemetry_fex'])
                self.assertEqual(actual.render(
                    rust_telemetry_deployed_fex_paths=paths
                ), fex)
        version = env.from_string(defaults['rust_telemetry_fex_version'])
        self.assertEqual(version.render(
            rust_telemetry_deployed_fex_version={'stdout': 'FEX-2610\n'}
        ), 'FEX-2610')

    def test_successful_maintenance_restarts_telemetry_before_fresh_rcon_check(self):
        tasks = yaml.safe_load((ROOT / 'roles/rust_maintenance/tasks/main.yml').read_text())
        workflow = next(task for task in tasks if task['name'] == 'Execute selected jobs inside one maintenance window')
        block = workflow['block']
        restart_index = next(
            index for index, task in enumerate(block)
            if task['name'] == 'Restart collector after maintenance'
        )
        self.assertEqual(
            block[restart_index]['ansible.builtin.systemd_service'],
            {'name': 'rust-telemetry.service', 'state': 'restarted'},
        )
        fresh_check_index = next(
            index for index, task in enumerate(block)
            if task['name'] == 'Wait for fresh RCON data'
        )
        self.assertLess(restart_index, fresh_check_index)
        record_index = next(
            index for index, task in enumerate(block)
            if task['name'] == 'Record verified maintenance for performance comparison'
        )
        self.assertLess(fresh_check_index, record_index)
        refresh_index = next(index for index, task in enumerate(tasks)
                             if task.get('ansible.builtin.include_role', {}).get('name') == 'rust_maintenance_refresh')
        self.assertGreater(refresh_index, tasks.index(workflow))
        refresh = yaml.safe_load((ROOT / 'roles/rust_maintenance_refresh/tasks/main.yml').read_text())
        self.assertIn('fex_installed', refresh[-1]['loop'])
        install = next(task for task in refresh if task['name'] == 'Install current dashboard maintenance probes')
        self.assertEqual(install['ansible.builtin.copy']['dest'], '/opt/rustberrypi/telemetry/maintenance.py')

    def test_fex_activation_preserves_service_world_and_previous_unit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            unit = root / 'rust.service'
            config = root / 'telemetry.json'
            metadata = root / 'deployment.json'
            original = ('[Service]\nExecStart=/usr/bin/python3 /helper/launch.py /usr/bin/FEX '
                        '/srv/rust/server/RustDedicated +server.identity keep-world +server.seed 42\n')
            unit.write_text(original)
            config.write_text(json.dumps({'rootfs': '/keep-rootfs', 'fex': '/usr/bin/FEX'}))
            metadata.write_text(json.dumps({'identity': 'keep-world', 'seed': 42, 'fex_commit': 'old'}))
            with patch.object(control, 'UNIT', unit), patch.object(control, 'CONFIG', config), \
                    patch.object(control, 'METADATA', metadata), patch.object(control, 'STATE', root), \
                    patch.object(control, 'inspect', return_value={'fex': '/usr/bin/FEX'}), \
                    patch.object(control, 'run', side_effect=['old', 'FEX-new']):
                control.activate_fex('a' * 40, 'FEX-new')
            self.assertEqual((root / 'rust.service.before-fex').read_text(), original)
            self.assertIn('+server.identity keep-world +server.seed 42', unit.read_text())
            self.assertIn('/opt/fex/' + 'a' * 40 + '/bin/FEX', unit.read_text())
            self.assertEqual(json.loads(metadata.read_text())['identity'], 'keep-world')
            self.assertEqual(json.loads(config.read_text())['rootfs'], '/keep-rootfs')

    def test_unverified_fex_never_changes_unit(self):
        with patch.object(control, 'run', side_effect=['old', 'unexpected']), \
                patch.object(control, 'inspect') as inspect:
            with self.assertRaises(ValueError):
                control.activate_fex('a' * 40, 'FEX-new')
            inspect.assert_not_called()

    def test_countdown_has_no_duplicate_or_out_of_range_notices(self):
        self.assertEqual(control.countdown_points(600), [600, 300, 60, 10, 0])
        self.assertEqual(control.countdown_points(60), [60, 10, 0])
        self.assertEqual(control.countdown_points(0), [0])

    def test_notification_checks_players_announces_then_saves(self):
        client = MagicMock(spec=['send', 'recv', 'settimeout', 'ping', 'close'])
        client.recv.side_effect = [json.dumps({'Identifier': 1, 'Message': '[]'}),
                                  json.dumps({'Identifier': 2, 'Message': ''}),
                                  json.dumps({'Identifier': 3, 'Message': 'Saved'})]
        with patch.object(control.websocket, 'create_connection', return_value=client) as connect, \
                patch.object(control.Path, 'read_text', return_value='secret'), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            control.notices(0, 'rust')
            connect.assert_called_once()
            self.assertNotIn('secret', output.getvalue())
        commands = [json.loads(call.args[0])['Message'] for call in client.send.call_args_list]
        self.assertEqual(commands[0], 'playerlist')
        self.assertTrue(commands[1].startswith('say '))
        self.assertEqual(commands[2], 'server.save')
        client.close.assert_called_once()

    def test_failed_notification_cannot_reach_save(self):
        client = MagicMock(spec=['send', 'recv', 'settimeout', 'ping', 'close'])
        client.recv.side_effect = ConnectionResetError()
        with patch.object(control.websocket, 'create_connection', return_value=client), \
                patch.object(control.Path, 'read_text', return_value='secret'):
            with self.assertRaises(ConnectionResetError):
                control.notices(0, 'rust')
        self.assertEqual(client.send.call_count, 1)
        client.close.assert_called_once()

    def test_archive_integrity_and_metadata_are_required(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'backup.tar.gz'
            def write_archive(name):
                with tarfile.open(path, 'w:gz') as archive:
                    info = tarfile.TarInfo(name)
                    info.size = 2
                    archive.addfile(info, io.BytesIO(b'{}'))
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                path.with_suffix('.gz.sha256').write_text(digest + '  backup.tar.gz\n')
            write_archive('data/deployment.json')
            control.verify_archive(path)
            path.with_suffix('.gz.sha256').write_text('0' * 64)
            with self.assertRaises(ValueError):
                control.verify_archive(path)
            write_archive('../outside')
            with self.assertRaises(ValueError):
                control.verify_archive(path)
            write_archive('data/other')
            with self.assertRaises(ValueError):
                control.verify_archive(path)
