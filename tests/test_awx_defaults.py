import json
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest

import yaml


class AwxDefaultsTests(unittest.TestCase):
    def test_local_and_awx_defaults_match(self):
        root = Path(__file__).resolve().parents[1]
        local = root / 'inventory/group_vars/rust_servers.yml'
        awx = root / 'playbooks/group_vars/rust_servers.yml'
        self.assertEqual(local.read_text(), awx.read_text())
        example = root / 'inventory/server.example.yml'
        self.assertNotIn('\nrust_hostname:', example.read_text())

    def test_name_and_description_are_read_from_controller_files(self):
        root = Path(__file__).resolve().parents[1]
        hostname = (root / 'inventory/descriptions/rustpi-hostname.txt').read_text()
        description = (root / 'inventory/descriptions/rustpi.txt').read_text()
        self.assertTrue(hostname.strip())
        self.assertNotIn('\n', hostname.strip())
        self.assertIn('FEATURES AND RULES\n', description)
        banner = root / 'inventory/descriptions/rustbanner.png'
        with banner.open('rb') as image:
            self.assertEqual(image.read(16)[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', image.read(8)), (1024, 512))
        logo = root / 'inventory/descriptions/logo.png'
        with logo.open('rb') as image:
            self.assertEqual(image.read(16)[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', image.read(8)), (256, 256))
        banner_url = (
            'https://raw.githubusercontent.com/kpeacocke/rustberryPi/main/'
            'inventory/descriptions/rustbanner.png'
        )
        logo_url = (
            'https://raw.githubusercontent.com/kpeacocke/rustberryPi/main/'
            'inventory/descriptions/logo.png'
        )

        tasks = yaml.safe_load(
            (root / 'roles/rust_server/tasks/main.yml').read_text()
        )
        selected = [task for task in tasks if task['name'] in (
            'Resolve optional server.cfg values before serializing them',
            'Keep server.cfg settings on persistent storage',
        )]
        self.assertEqual(len(selected), 2)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'server.cfg'
            module = selected[1]['ansible.builtin.lineinfile']
            module['path'] = str(config)
            module.pop('owner')
            module.pop('group')
            playbook = [
                {'hosts': 'rust_servers', 'gather_facts': False, 'tasks': [
                {'ansible.builtin.assert': {'that': [
                    'rust_hostname == ' + json.dumps(hostname.strip()),
                    "'FEATURES AND RULES\\n' in rust_description",
                    """rust_hostname is match("^[a-zA-Z0-9 ._,|'-]+$")""",
                    'rust_headerimage == ' + json.dumps(banner_url),
                    'rust_logoimage == ' + json.dumps(logo_url),
                    'rust_url == "https://discord.gg/UV6D43jQY"',
                ]}},
                *selected,
                ]},
            ]
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.yml', dir=root / 'playbooks',
            ) as temporary:
                # JSON is valid YAML, and avoids extra syntax in the fixture.
                temporary.write(json.dumps(playbook))
                temporary.flush()
                result = subprocess.run(
                    ['ansible-playbook', '-i', str(root / 'inventory/hosts.yml'),
                     '--connection', 'local', '--limit', 'rustberrypi',
                     temporary.name],
                    capture_output=True, text=True, cwd=root,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            contents = config.read_text()
            self.assertIn('Now with added RUST+!!', contents)
            self.assertIn('FEATURES AND RULES\\n', contents)
            self.assertNotIn("lookup('ansible.builtin.file'", contents)
            self.assertIn('server.headerimage "' + banner_url + '"', contents)
            self.assertIn('server.logoimage "' + logo_url + '"', contents)
            self.assertIn('server.url "https://discord.gg/UV6D43jQY"', contents)
