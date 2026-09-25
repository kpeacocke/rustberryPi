"""Migration preserves old settings without overwriting local operator data."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

SOURCE = Path(__file__).resolve().parents[1] / 'tools/migrate_inventory.py'
SPEC = importlib.util.spec_from_file_location('migration', SOURCE)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class MigrationTests(unittest.TestCase):
    def test_preserves_settings_and_refuses_second_write(self):
        documents = {
            'inventory/hosts.yml': {'rust_servers': {'hosts': {'example': {'ansible_user': 'operator'}}}},
            'inventory/group_vars/rust_servers.yml': {'rust_identity': 'existing-world', 'rust_uid': 2001},
            'roles/fex/defaults/main.yml': {'fex_existing_rootfs': '/example/rootfs'},
        }

        def git_read(command, **kwargs):
            return yaml.safe_dump(documents[command[2].split(':', 1)[1]])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(migration, '__file__', str(root / 'tools/migrate_inventory.py')), \
                    patch.object(migration.subprocess, 'check_output', side_effect=git_read), \
                    patch('sys.argv', ['migrate', '--ref', 'old']):
                migration.main()
                settings = yaml.safe_load((root / 'inventory/host_vars/example/00-migrated.yml').read_text())
                self.assertEqual(settings['rust_identity'], 'existing-world')
                self.assertEqual(settings['fex_existing_rootfs'], '/example/rootfs')
                local = root / 'inventory/hosts.local.yml'
                local.write_text('operator changes')
                with self.assertRaises(ValueError):
                    migration.main()
                self.assertEqual(local.read_text(), 'operator changes')

    def test_refuses_generic_commit_without_identity(self):
        with patch.object(migration.subprocess, 'check_output', return_value='rust_identity: ""\nfex_existing_rootfs: /none\n'), \
                patch('sys.argv', ['migrate', '--ref', 'generic']):
            with self.assertRaisesRegex(ValueError, 'no deployment identity'):
                migration.main()
