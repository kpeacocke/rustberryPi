import importlib.util
from pathlib import Path
import unittest
import io
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('access', Path(__file__).resolve().parents[1] / 'roles/rust_server/files/access.py')
access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(access)
A = '76561198000000001'
B = '76561198000000002'


class AccessTests(unittest.TestCase):
    def test_five_approved_players_supported_without_granting_admin(self):
        players = ['7656119800000000' + str(i) for i in range(1, 6)]
        desired = access.policy({'mode': 'restricted', 'players': players, 'admins': [A]}, None)
        self.assertEqual(len(desired['players']), 5)
        self.assertEqual(desired['admins'], [A])
        with self.assertRaises(ValueError):
            access.policy({'mode': 'restricted', 'players': players + ['76561198000000006']}, None)

    def test_admin_must_be_approved(self):
        with self.assertRaises(ValueError):
            access.policy({'mode': 'restricted', 'players': [A], 'admins': [B]}, None)

    def test_admin_assignment_and_revocation(self):
        desired = access.policy({'mode': 'restricted', 'players': [A, B], 'admins': [A]}, None)
        original = f'moderatorid {B} "old" ""\nbanid {B} "banned" "reason"\n'
        result = access.reconcile(original, desired)
        self.assertIn(f'ownerid {A}', result)
        self.assertNotIn(f'moderatorid {B}', result)
        self.assertIn(f'banid {B}', result)
        self.assertEqual(access.reconcile(result, desired), result)
        cleared = access.policy({'admins': ''}, desired)
        self.assertNotIn('ownerid', access.reconcile(result, cleared))

    def test_unattended_preserves_managed_admins(self):
        previous = {'mode': 'restricted', 'players': [A], 'admins': [A]}
        self.assertEqual(access.policy({}, previous), previous)

    def test_admin_invalid_input_rejected(self):
        with self.assertRaises(ValueError):
            access.policy({'admins': 'not-a-steam-id'}, None)

    def test_restricted_requires_ids(self):
        for invalid in ('', 'not-an-id', 'https://steamcommunity.com/id/example', A + ';quit'):
            with self.assertRaises(ValueError):
                access.policy({'mode': 'restricted', 'players': invalid}, None)

    def test_parse_deduplicate(self):
        result = access.policy({'mode': 'restricted', 'players': f'{A},\nhttps://steamcommunity.com/profiles/{B}/ {A}'}, None)
        self.assertEqual(result['players'], [A, B])

    def test_limit_and_invalid_mode(self):
        with self.assertRaises(ValueError):
            access.policy({'mode': 'restricted', 'players': [str(int(A) + i) for i in range(6)]}, None)
        with self.assertRaises(ValueError):
            access.policy({'mode': 'anon'}, None)

    def test_unattended_preserves_restriction(self):
        previous = {'mode': 'restricted', 'players': [A]}
        self.assertEqual(access.policy({}, previous), previous)

    def test_revocation_preserves_admin_and_ban(self):
        original = f'ownerid {A} "owner" ""\nbanid {B} "blocked" "reason"\nskipqueueid {B} "old" ""\n'
        desired = {'mode': 'restricted', 'players': [A]}
        result = access.reconcile(original, desired)
        self.assertIn(f'ownerid {A}', result)
        self.assertIn(f'banid {B}', result)
        self.assertNotIn(f'skipqueueid {B}', result)
        self.assertEqual(access.reconcile(result, desired), result)

    def test_unlisted_admin_refused(self):
        with self.assertRaises(ValueError):
            access.reconcile(f'ownerid {B} "owner" ""\n', {'mode': 'restricted', 'players': [A]})

    def test_server_reformat_does_not_restart(self):
        original = f'skipqueueid {B} "renamed" ""\nskipqueueid {A} "renamed" ""\n'
        self.assertEqual(access.reconcile(original, {'mode': 'restricted', 'players': [A, B]}), original)

    def test_public_revokes_queue_entries(self):
        self.assertEqual(access.reconcile(f'skipqueueid {A} "friend" ""\n', {'mode': 'public', 'players': []}), '')

    def test_apply_is_idempotent_and_stops_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if command[1] == 'stop':
                    self.assertFalse((root / 'world/cfg/users.cfg').exists())
                return SimpleNamespace(returncode=0)

            with patch.object(access, 'Path', return_value=root), \
                    patch.object(access, 'open', side_effect=lambda *a: open(root / 'lock', a[1]), create=True), \
                    patch.object(access.os, 'chown'), \
                    patch.object(access.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=2001, pw_gid=2001)), \
                    patch.object(access.subprocess, 'run', side_effect=run), \
                    patch('sys.argv', ['access', 'world']):
                for expected in (True, False):
                    output = io.StringIO()
                    request = json.dumps({'mode': 'restricted', 'players': [A]})
                    with patch('sys.stdin', io.StringIO(request)), patch('sys.stdout', output):
                        access.main()
                    self.assertEqual(json.loads(output.getvalue())['changed'], expected)
                self.assertEqual(sum(command[1] == 'stop' for command in calls), 1)
                self.assertIn(A, (root / 'world/cfg/users.cfg').read_text())
