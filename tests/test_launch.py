"""Regression coverage for early RCON initialization and credential handling."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('launch', Path(__file__).resolve().parents[1] / 'roles/rust_server/files/launch.py')
launch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch)


class LaunchTests(unittest.TestCase):
    def test_managed_rcon_is_passed_before_exec_with_loopback_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = root / 'world/cfg/server.cfg'
            cfg.parent.mkdir(parents=True)
            args = ['/usr/bin/FEX', '/srv/rust/server/RustDedicated', '+server.identity', 'world']
            self.assertEqual(launch.startup_args(args, root), args)
            cfg.write_text('server.description "Unmanaged settings"\n')
            self.assertEqual(launch.startup_args(args, root), args)
            cfg.write_text('// BEGIN rustberryPi local telemetry\nrcon.password "' + 'a' * 64 +
                           '"\n// END rustberryPi local telemetry\n')
            result = launch.startup_args(args, root)
            self.assertEqual(result[len(args):], ['+rcon.ip', '127.0.0.1', '+rcon.port', '28016',
                                                 '+rcon.web', '1', '+rcon.password', 'a' * 64])
            cfg.write_text('// BEGIN rustberryPi local telemetry\nrcon.password "SECRET"\n')
            with self.assertRaises(ValueError) as raised:
                launch.startup_args(args, root)
            self.assertNotIn('SECRET', str(raised.exception))

    def test_identity_cannot_escape_state_directory(self):
        with self.assertRaises(ValueError):
            launch.startup_args(['FEX', '+server.identity', '../elsewhere'])
