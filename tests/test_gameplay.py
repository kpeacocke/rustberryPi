"""Isolated filesystem and failure-injection tests; never contact a real game."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
for folder in ('roles/rust_server/files', 'roles/rust_backup/files', 'maintenance'):
    sys.path.insert(0, str(ROOT / folder))
spec = importlib.util.spec_from_file_location('maintenance', ROOT / 'maintenance/control.py')
maintenance = importlib.util.module_from_spec(spec)
sys.modules['maintenance'] = maintenance
with patch.dict(sys.modules, {'websocket': MagicMock()}):
    spec.loader.exec_module(maintenance)
spec = importlib.util.spec_from_file_location('gameplay', ROOT / 'roles/rust_gameplay/files/gameplay.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


def request(**changes):
    result = dict(operation='new-world', identity='test', seed=1433876491, worldsize=1500,
                  decay_scale=.25, upkeep=5760, team_size=5, tutorial=False, admins=[],
                  warning=0, timeout=60, confirm='WIPE test 1433876491 1500 KEEP BLUEPRINTS')
    result.update(changes)
    return result


class GameplayTests(unittest.TestCase):
    def test_invalid_inputs_and_confirmation(self):
        g.validate(request())
        for change in ({'confirm': ''}, {'confirm': True}, {'seed': -1}, {'seed': '123'},
                       {'worldsize': 4000}, {'decay_scale': float('nan')}, {'decay_scale': True},
                       {'upkeep': 0}, {'team_size': 6}, {'tutorial': 'false'},
                       {'admins': 'preserve'}, {'identity': '../test'}, {'timeout': 0}):
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                g.validate(request(**change))

    def test_config_duplicate_removal_and_idempotence(self):
        text = 'rcon.password "secret"\ndecay.scale 1\nDecay.Scale 2\nserver.description "keep"\n'
        updated = g.reconcile(text, g.values(request()))
        self.assertEqual(updated.count('decay.scale'), 1)
        self.assertIn('rcon.password "secret"', updated)
        self.assertIn('server.tutorialenabled false', updated)
        self.assertEqual(g.reconcile(updated, g.values(request())), updated)
        for line in ('server.seed 123', 'exec other.cfg', 'ownerid 123', 'decay.scale 1; quit'):
            with self.assertRaises(ValueError):
                g.reconcile(line, g.values(request()))

    def test_unit_update_is_narrow_and_fails_on_drift(self):
        text = 'Environment=FEX_ROOTFS=/keep\nExecStart=/keep/FEX +server.level "Procedural Map" +server.identity test +server.seed 123 +server.worldsize 1500\n'
        old = dict(identity='test', seed=123, worldsize=1500)
        new = dict(old, seed=1433876491)
        self.assertEqual(g.update_unit(text, old, new), text.replace('seed 123', 'seed 1433876491'))
        with self.assertRaises(ValueError):
            g.update_unit(text, dict(old, seed=42), new)
        for override in ('+decay.scale 1', '+global.ownerid 123', '+GLOBAL.MODERATORID 123'):
            with self.subTest(override=override), self.assertRaises(ValueError):
                g.update_unit(text.rstrip() + ' ' + override + '\n', old, new)

    def test_admin_case_and_aliases_are_removed(self):
        self.assertEqual(g.clear_admins('Global.OwnerID 123\nMODERATORID 456\nbanid 789\n',
                                       {'mode': 'public', 'players': [], 'admins': []}), 'banid 789\n')

    def test_indirect_admin_grants_fail_closed_in_public_mode(self):
        with self.assertRaises(ValueError):
            g.clear_admins('exec admins.cfg\n', {'mode': 'public', 'players': [], 'admins': []})

    def test_runtime_values_fail_closed(self):
        for response, expected in [('decay.scale: "0.25"', .25), ('decay.scale: 1', 1)]:
            with patch.object(g, 'rcon', return_value=response):
                self.assertEqual(g.read_value('decay.scale'), expected)
        for response in ('Unknown command', '', 'decay.scale: 0.25\nother: 5'):
            with patch.object(g, 'rcon', return_value=response), self.assertRaises(ValueError):
                g.read_value('decay.scale')

    def fixture(self, root):
        data = root / 'data'
        cfg = data / 'test/cfg'
        cfg.mkdir(parents=True)
        (cfg / 'server.cfg').write_text('rcon.password "secret"\n')
        (cfg / 'users.cfg').write_text('ownerid 76561190000000001 "admin"\nbanid 76561190000000002\n')
        (data / 'access.json').write_text(json.dumps(dict(mode='restricted', players=['76561190000000001'], admins=['76561190000000001'])))
        (data / 'deployment.json').write_text(json.dumps(dict(identity='test', seed=123, worldsize=1500, uid=os.getuid())))
        for name in ('proceduralmap.1500.123.999.sav', 'proceduralmap.1500.123.999.sav.1',
                     'proceduralmap.1500.123.999.map', 'player.blueprints.5.db', 'player.blueprints.5.db-wal', 'player.identities.5.db'):
            (data / 'test' / name).write_bytes(name.encode())
        return data

    def test_staging_preserves_blueprints_roster_bans_and_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            data = self.fixture(Path(directory))
            before = {p.name: p.read_bytes() for p in (data / 'test').glob('player.*')}
            removed = g.prepare(data, request(), dict(identity='test', seed=1433876491, worldsize=1500))
            self.assertEqual(len(removed), 3)
            self.assertEqual(before, {p.name: p.read_bytes() for p in (data / 'test').glob('player.*')})
            self.assertNotIn('ownerid', (data / 'test/cfg/users.cfg').read_text())
            self.assertIn('banid', (data / 'test/cfg/users.cfg').read_text())
            policy = json.loads((data / 'access.json').read_text())
            self.assertEqual(policy['players'], ['76561190000000001'])
            self.assertEqual(policy['admins'], [])

    def test_configure_does_not_remove_any_world_file(self):
        with tempfile.TemporaryDirectory() as directory:
            data = self.fixture(Path(directory))
            old = json.loads((data / 'deployment.json').read_text())
            self.assertEqual(g.prepare(data, request(operation='configure'), old), [])
            self.assertTrue((data / 'test/proceduralmap.1500.123.999.sav').exists())
            self.assertEqual(json.loads((data / 'deployment.json').read_text())['seed'], 123)

    def test_unknown_saves_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            data = self.fixture(Path(directory))
            (data / 'test/custom.sav').touch()
            with self.assertRaises(ValueError):
                g.world_files(data / 'test')
            (data / 'linked').symlink_to(data / 'access.json')
            with self.assertRaises(ValueError):
                g.safe_tree(data)

    def test_partial_archive_is_private_before_compression_and_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self.fixture(root)
            stamp = '20261016T000000.000000Z'
            partial = root / ('rust-' + stamp + '.tar.partial')
            def compression_failure(**kwargs):
                self.assertEqual(partial.stat().st_mode & 0o777, 0o600)
                raise OSError('fixture write failure')
            with patch.object(g, 'DATA', data), patch.object(g.tarfile, 'open', side_effect=compression_failure):
                with self.assertRaises(OSError):
                    g.archive_state(root, stamp)
            self.assertEqual(partial.stat().st_mode & 0o777, 0o600)
            with patch.object(g, 'DATA', data), self.assertRaises(FileExistsError):
                g.archive_state(root, stamp)

    def test_backup_detects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self.fixture(root)
            with patch.object(g, 'DATA', data):
                result = g.archive_state(root, '20261016T000000.000000Z')
            archive = Path(result['archive'])
            g.backup.checked_archive(archive, result['sha256'])
            archive.write_bytes(archive.read_bytes()[:-10])
            with self.assertRaises(ValueError):
                g.backup.checked_archive(archive, result['sha256'])


if __name__ == '__main__':
    unittest.main()

class TransactionTests(unittest.TestCase):
    fixture = GameplayTests.fixture
    """Exercise real staging/rollback with all system and RCON calls isolated."""
    def transaction(self, fail=None, operation='new-world'):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            data = self.fixture(root)
            state = root / 'maintenance'
            units = root / 'units'
            units.mkdir()
            unit = units / 'rust.service'
            original = 'ExecStart=/keep/FEX +server.level "Procedural Map" +server.identity test +server.seed 123 +server.worldsize 1500\n'
            unit.write_text(original)
            old = json.loads((data / 'deployment.json').read_text())
            new = dict(old, seed=1433876491) if operation == 'new-world' else dict(old)
            stack.enter_context(patch.object(g, 'DATA', data))
            stack.enter_context(patch.object(g, 'STATE', state))
            stack.enter_context(patch.object(g, 'UNIT', unit))
            stack.enter_context(patch.object(g, 'open', side_effect=lambda *a: open(root / 'lock', 'w'), create=True))
            stack.enter_context(patch.object(g, 'preflight', return_value=(old, new, g.update_unit(original, old, new))))
            stack.enter_context(patch.object(g, 'active', side_effect=lambda name: name == 'rust.service'))
            stack.enter_context(patch.object(g, 'stop_clean'))
            stack.enter_context(patch.object(g, 'read_value', return_value=1))
            stack.enter_context(patch.object(g, 'verify'))
            stack.enter_context(patch.object(g, 'rcon'))
            stack.enter_context(patch.object(g.maintenance, 'notices'))
            stack.enter_context(patch.object(g, 'run', return_value='success'))
            stack.enter_context(patch('builtins.print'))
            def ready(*args, **kwargs):
                if fail == 'health':
                    raise RuntimeError('fixture health failure')
                (data / f"test/proceduralmap.1500.{new['seed']}.999.sav").write_bytes(b'new save')
            stack.enter_context(patch.object(g.health, 'wait_ready', side_effect=ready))
            if fail == 'backup':
                stack.enter_context(patch.object(g, 'archive_state', side_effect=ValueError('bad archive')))
            if fail == 'shutdown':
                stack.enter_context(patch.object(g, 'stop_clean', side_effect=RuntimeError('unclean')))
            if fail:
                with self.assertRaises((RuntimeError, ValueError)):
                    g.execute(request(operation=operation), root, root)
                self.assertTrue((state / 'offline').exists())
                self.assertEqual(unit.read_text(), original)
                self.assertTrue((data / 'test/proceduralmap.1500.123.999.sav').exists())
                self.assertEqual(json.loads((data / 'deployment.json').read_text())['seed'], 123)
                self.assertIn('ownerid', (data / 'test/cfg/users.cfg').read_text())
                if fail == 'health':
                    self.assertEqual(json.loads((state / 'gameplay.json').read_text())['phase'], 'rolled-back-stopped')
                    self.assertEqual(len(list(root.glob('data.failed-gameplay-*'))), 1)
            else:
                g.execute(request(operation=operation), root, root)
                self.assertFalse(state.exists())
                self.assertEqual(json.loads((data / 'deployment.json').read_text())['seed'], new['seed'])
                self.assertEqual(len(list(root.glob('data.pre-gameplay-*'))), 1)
                evidence = json.loads(next(root.glob('gameplay-*.json')).read_text())
                self.assertEqual(evidence['phase'], 'verified')
                self.assertEqual(len(evidence['blueprints']), 2)
                self.assertNotIn('ownerid', (data / 'test/cfg/users.cfg').read_text())

    def test_successful_wipe(self):
        self.transaction()

    def test_successful_configuration(self):
        self.transaction(operation='configure')

    def test_corrupt_backup_never_swaps_state(self):
        self.transaction(fail='backup')

    def test_unclean_shutdown_never_swaps_state(self):
        self.transaction(fail='shutdown')

    def test_failed_start_rolls_back_data_and_unit_but_stays_guarded(self):
        self.transaction(fail='health')
