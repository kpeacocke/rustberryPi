from pathlib import Path
import unittest


class AwxDefaultsTests(unittest.TestCase):
    def test_local_and_awx_defaults_match(self):
        root = Path(__file__).resolve().parents[1]
        local = root / 'inventory/group_vars/rust_servers.yml'
        awx = root / 'playbooks/group_vars/rust_servers.yml'
        self.assertEqual(local.read_text(), awx.read_text())
