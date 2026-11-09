"""Maintenance probes may refresh indexes, but never install or stop the game."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('maintenance', ROOT / 'telemetry/maintenance.py')
maintenance = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {'apt': MagicMock(), 'steam_manage': MagicMock()}):
    SPEC.loader.exec_module(maintenance)


class MaintenanceTests(unittest.TestCase):
    def test_hailo_probe_only_runs_read_only_validator(self):
        with patch.object(maintenance.subprocess, 'check_output',
                          return_value='{"candidate_ready": true, "available": "4.24.0"}') as command:
            result = maintenance.check_hailo()
        self.assertTrue(result['candidate_ready'])
        self.assertEqual(command.call_args.args[0],
                         ['/usr/bin/python3', '/usr/local/libexec/rustberrypi-hailo-install.py', '--status'])
        with patch.object(maintenance.subprocess, 'check_output', side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                maintenance.check_hailo()

    def test_latest_fex_inputs_resolve_release_tag_not_target_branch(self):
        release = {'tag_name': 'FEX-2611', 'published_at': '2026-11-01T00:00:00Z',
                   'target_commitish': 'main'}
        responses = [io.StringIO(json.dumps(release)),
                     io.StringIO(json.dumps({'sha': 'a' * 40}))]
        with patch.object(maintenance.urllib.request, 'urlopen', side_effect=responses) as request:
            result = maintenance.check_fex({})
        self.assertEqual(result, {'latest_release': 'FEX-2611', 'published_at': release['published_at'],
                                  'latest_commit': 'a' * 40})
        self.assertEqual(request.call_args_list[1].args[0].full_url,
                         'https://api.github.com/repos/FEX-Emu/FEX/commits/FEX-2611')

    def test_latest_fex_inputs_refuse_invalid_tag_or_unresolved_commit(self):
        for tag, commit in [('main/other', 'a' * 40), ('FEX-2611', 'main'),
                            ('FEX-2611', 'a' * 39)]:
            with self.subTest(tag=tag, commit=commit):
                responses = [io.StringIO(json.dumps({'tag_name': tag, 'published_at': 'today'})),
                             io.StringIO(json.dumps({'sha': commit}))]
                with patch.object(maintenance.urllib.request, 'urlopen', side_effect=responses):
                    with self.assertRaises(ValueError):
                        maintenance.check_fex({})

    def test_installed_fex_is_read_from_selected_binary_not_configured_label(self):
        with patch.object(maintenance.subprocess, 'check_output', return_value='FEX-2610\n') as command:
            result = maintenance.check_fex_installed({
                'fex': '/opt/fex/new/bin/FEX', 'fex_version': 'FEX-old',
            })
        self.assertEqual(result, {'version': 'FEX-2610'})
        command.assert_called_once_with(
            ['/opt/fex/new/bin/FEXGetConfig', '--version'], text=True, timeout=15)

    def test_invalid_or_failed_installed_fex_probe_is_not_a_success(self):
        for version in ('', 'FEX-2610\nunexpected output'):
            with self.subTest(version=version), \
                    patch.object(maintenance.subprocess, 'check_output', return_value=version):
                with self.assertRaises(ValueError):
                    maintenance.check_fex_installed({'fex': '/usr/bin/FEX'})
        with patch.object(maintenance.subprocess, 'check_output', side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                maintenance.check_fex_installed({'fex': '/missing/FEX'})

    def test_release_network_failure_does_not_hide_new_installed_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'telemetry.json').write_text('{}')
            (root / 'maintenance.json').write_text(json.dumps({
                'fex': {'latest_release': 'FEX-old', 'ok': True, 'checked_at': 1},
            }))
            with patch.object(maintenance, 'Path', side_effect=lambda value: root / Path(value).name), \
                    patch.object(maintenance, 'check_fex_installed', return_value={'version': 'FEX-2610'}), \
                    patch.object(maintenance, 'check_packages', return_value={}), \
                    patch.object(maintenance, 'check_rust', return_value={}), \
                    patch.object(maintenance, 'check_fex', side_effect=OSError('offline')), \
                    patch.object(maintenance, 'check_hailo', return_value={'candidate_ready': False}), \
                    patch.object(maintenance.subprocess, 'check_output', return_value=''):
                maintenance.main()
            result = json.loads((root / 'maintenance.json').read_text())
            self.assertEqual(result['fex_installed']['version'], 'FEX-2610')
            self.assertTrue(result['fex_installed']['ok'])
            self.assertEqual(result['fex']['installed_pin'], 'FEX-2610')
            self.assertFalse(result['fex']['ok'])
            self.assertEqual(result['fex']['checked_at'], 1)

    def test_package_probe_counts_security_and_only_refreshes_indexes(self):
        package = SimpleNamespace(name='example', is_upgradable=True,
                                  candidate=SimpleNamespace(origins=[SimpleNamespace(archive='trixie-security', label='Debian')]))
        with patch.object(maintenance.apt, 'Cache', return_value=[package]), \
                patch.object(maintenance.subprocess, 'run') as run:
            result = maintenance.check_packages()
        self.assertEqual(result['security_count'], 1)
        self.assertEqual(run.call_args.args[0][-1], 'update')
        self.assertNotIn('install', run.call_args.args[0])

    def test_steam_probe_only_reads_app_info(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(maintenance, 'open', side_effect=lambda *args: open(Path(directory) / 'lock', 'w'), create=True), \
                    patch.object(maintenance.subprocess, 'check_output', side_effect=['uuid\n', 'reply']) as command, \
                    patch.object(maintenance, 'installed_build', return_value='1'), \
                    patch.object(maintenance, 'public_build', return_value='2'):
                result = maintenance.check_rust({'storage_uuid': 'uuid', 'rootfs': '/rootfs', 'fex': '/fex'})
        self.assertEqual(result, {'installed': '1', 'available': '2'})
        argv = command.call_args.args[0]
        self.assertIn('+app_info_print', argv)
        self.assertNotIn('+app_update', argv)
        self.assertEqual(argv[:4], ['runuser', '-u', 'rust', '--'])

    def test_wrong_mount_refuses_steam_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(maintenance, 'open', side_effect=lambda *args: open(Path(directory) / 'lock', 'w'), create=True), \
                    patch.object(maintenance.subprocess, 'check_output', return_value='wrong') as command:
                with self.assertRaises(ValueError):
                    maintenance.check_rust({'storage_uuid': 'expected'})
                self.assertEqual(command.call_count, 1)
