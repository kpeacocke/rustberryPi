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


class DashboardLauncherTests(unittest.TestCase):
    """The kiosk launcher must never block desktop startup on a keyring unlock prompt."""

    def setUp(self):
        script = Path(__file__).resolve().parents[1] / 'dashboard/launch.sh'
        # Join shell line continuations so each browser invocation is one logical command.
        self.commands = script.read_text().replace('\\\n', ' ').splitlines()

    def test_every_browser_invocation_avoids_the_secret_service(self):
        invocations = [line for line in self.commands if '"$browser"' in line]
        self.assertTrue(invocations, 'launcher no longer invokes the browser')
        for invocation in invocations:
            # Desktop auto-login leaves the login keyring locked; libsecret would prompt.
            self.assertIn('--password-store=basic', invocation)

    def test_all_three_displays_are_still_launched(self):
        joined = ' '.join(self.commands)
        for view in ('game', 'touch', 'system'):
            self.assertIn(view, joined)


if __name__ == '__main__':
    unittest.main()
