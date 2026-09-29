import importlib.util
from pathlib import Path
import tempfile
import unittest
import yaml

SPEC = importlib.util.spec_from_file_location('rename_inventory', Path(__file__).resolve().parents[1] / 'tools/rename_inventory_host.py')
rename = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rename)


class RenameTests(unittest.TestCase):
    def test_preserves_private_world_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            variables = root / 'inventory/host_vars/oldhost'
            variables.mkdir(parents=True)
            (variables / '99-access.yml').write_text('rust_identity: keep-world\nrust_seed: 42\n')
            source = root / 'inventory/hosts.local.yml'
            source.write_text('rust_servers:\n  hosts:\n    oldhost:\n      ansible_host: 192.0.2.10\n      ansible_user: operator\n')
            self.assertTrue(rename.rename(root, '192.0.2.10', 'rustberrypi'))
            self.assertFalse(rename.rename(root, '192.0.2.10', 'rustberrypi'))
            host = yaml.safe_load(source.read_text())['rust_servers']['hosts']['rustberrypi']
            self.assertEqual(host['ansible_host'], '192.0.2.10')
            self.assertEqual(host['rust_system_hostname'], 'rustberrypi')
            self.assertEqual((variables.parent / 'rustberrypi/99-access.yml').read_text(), 'rust_identity: keep-world\nrust_seed: 42\n')
            self.assertEqual(len(list((variables.parent / '.rename-backups').glob('*.yml'))), 1)

    def test_conflicting_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'inventory/host_vars/rustberrypi'
            target.mkdir(parents=True)
            source = root / 'inventory/hosts.local.yml'
            original = 'rust_servers:\n  hosts:\n    oldhost:\n      ansible_host: 192.0.2.10\n'
            source.write_text(original)
            with self.assertRaises(ValueError):
                rename.rename(root, '192.0.2.10', 'rustberrypi')
            self.assertEqual(source.read_text(), original)
