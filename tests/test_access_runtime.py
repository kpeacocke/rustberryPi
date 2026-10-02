import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'roles/rust_server/files'))
SPEC = importlib.util.spec_from_file_location('access_runtime', ROOT / 'roles/rust_server/files/access_runtime.py')
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)

PLAYER = '76561198000000001'


class FakeRcon:
    def __init__(self, response, unsolicited=False):
        self.response = response
        self.command = None
        self.unsolicited = unsolicited

    def send(self, message):
        self.command = json.loads(message)

    def recv(self):
        if self.unsolicited:
            self.unsolicited = False
            return json.dumps({'Identifier': -1, 'Message': 'Unrelated event'})
        return json.dumps({'Identifier': self.command['Identifier'],
                           'Message': self.response.format(player=PLAYER)})

    def settimeout(self, timeout):
        pass

    def close(self):
        pass


class AccessRuntimeTests(unittest.TestCase):
    def test_applies_saved_policy_and_detects_missing_grants(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'access.json'
            secret = Path(directory) / 'rcon-password'
            state.write_text(json.dumps({'mode': 'restricted', 'players': [PLAYER]}))
            secret.write_text('a' * 64)
            paths = {'/srv/rust/data/access.json': state,
                     '/etc/rustberrypi/rcon-password': secret}
            with patch.object(runtime, 'Path', side_effect=paths.__getitem__), \
                    patch.object(runtime, 'query', return_value=True), \
                    patch.object(runtime.websocket, 'create_connection') as connect, \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                connect.return_value = FakeRcon('Added skip queue permission for friend ({player})')
                runtime.apply_access()
                self.assertIn('restored 1', output.getvalue())
                with self.assertRaisesRegex(RuntimeError, 'Restored 1 missing queue grants'):
                    runtime.apply_access(verify_only=True)
                connect.return_value = FakeRcon('User {player} will already skip the queue',
                                                unsolicited=True)
                runtime.apply_access(verify_only=True)
                self.assertIn('restored 0', output.getvalue())

    def test_public_policy_does_not_require_rcon(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'access.json'
            state.write_text(json.dumps({'mode': 'public', 'players': []}))
            with patch.object(runtime, 'Path', return_value=state), \
                    patch.object(runtime.websocket, 'create_connection') as connect:
                runtime.apply_access()
                connect.assert_not_called()

    def test_rejects_unconfirmed_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'access.json'
            secret = Path(directory) / 'rcon-password'
            state.write_text(json.dumps({'mode': 'restricted', 'players': [PLAYER]}))
            secret.write_text('a' * 64)
            paths = {'/srv/rust/data/access.json': state,
                     '/etc/rustberrypi/rcon-password': secret}
            with patch.object(runtime, 'Path', side_effect=paths.__getitem__), \
                    patch.object(runtime, 'query', return_value=True), \
                    patch.object(runtime.websocket, 'create_connection',
                                 return_value=FakeRcon('Unknown command')):
                with self.assertRaisesRegex(RuntimeError, 'did not confirm'):
                    runtime.apply_access()


if __name__ == '__main__':
    unittest.main()
