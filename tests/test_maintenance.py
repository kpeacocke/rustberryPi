"""Maintenance probes may refresh indexes, but never install or stop the game."""
import importlib.util
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
