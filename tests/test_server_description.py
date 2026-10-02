"""Exercise the managed server.cfg settings against an isolated config."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ServerConfigTests(unittest.TestCase):
    def test_settings_are_persistent_optional_and_idempotent(self):
        tasks = yaml.safe_load(
            (ROOT / 'roles/rust_server/tasks/main.yml').read_text(),
        )
        selected = [task for task in tasks if task['name'] in (
            'Resolve optional server.cfg values before serializing them',
            'Plan server.cfg settings without writing while Rust runs',
            'Stop game gracefully only when server.cfg settings change',
            'Keep server.cfg settings on persistent storage',
        )]
        self.assertEqual(len(selected), 4)

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'server.cfg'
            original = (
                'server.hostname "Existing"\n'
                'server.description "Old"\n'
                'rcon.password "Unmanaged"\n'
            )
            config.write_text(original)
            for task in selected:
                if 'ansible.builtin.lineinfile' in task:
                    module = task['ansible.builtin.lineinfile']
                    module['path'] = str(config)
                    module.pop('owner')
                    module.pop('group')
            selected[2].pop('ansible.builtin.command')
            selected[2].pop('changed_when')
            selected[2]['ansible.builtin.debug'] = {
                'msg': 'Graceful stop requested',
            }
            selected[3]['register'] = 'rust_server_cfg_write'
            selected.append({
                'name': 'Report planned and applied changes',
                'ansible.builtin.debug': {
                    'msg': (
                        "{{ {'planned': rust_server_cfg_plan.results "
                        "| selectattr('changed') | list | length, "
                        "'applied': rust_server_cfg_write.results "
                        "| selectattr('changed') | list | length} | to_json }}"
                    ),
                },
            })
            playbook = Path(directory) / 'config.yml'
            playbook.write_text(yaml.safe_dump([{
                'hosts': 'localhost',
                'gather_facts': False,
                'tasks': selected,
            }]))

            def converge(settings):
                result = subprocess.run(
                    ['ansible-playbook', '-i', 'localhost,', '-c', 'local',
                     str(playbook), '-e', json.dumps(settings)],
                    capture_output=True, text=True, check=True, cwd=ROOT,
                )
                return result.stdout

            output = converge({})
            self.assertIn('\\"planned\\": 0, \\"applied\\": 0', output)
            self.assertNotIn('Graceful stop requested', output)
            self.assertEqual(config.read_text(), original)

            settings = {
                'rust_description': 'Welcome "friends"!\nNo griefing.',
                'rust_headerimage': 'https://example.org/banner.png',
                'rust_logoimage': 'https://example.org/logo.png',
                'rust_url': 'https://example.org/community',
                'rust_saveinterval': 600,
            }
            output = converge(settings)
            self.assertIn('\\"planned\\": 5, \\"applied\\": 5', output)
            self.assertEqual(output.count('Graceful stop requested'), 1)
            self.assertEqual(
                config.read_text(),
                'server.hostname "Existing"\n'
                'server.description "Welcome \\"friends\\"!\\nNo griefing."\n'
                'rcon.password "Unmanaged"\n'
                'server.headerimage "https://example.org/banner.png"\n'
                'server.logoimage "https://example.org/logo.png"\n'
                'server.url "https://example.org/community"\n'
                'server.saveinterval 600\n',
            )
            output = converge(settings)
            self.assertIn('\\"planned\\": 0, \\"applied\\": 0', output)
            self.assertNotIn('Graceful stop requested', output)
            settings['rust_url'] = 'https://discord.gg/community'
            output = converge(settings)
            self.assertIn('\\"planned\\": 1, \\"applied\\": 1', output)
            self.assertEqual(output.count('Graceful stop requested'), 1)
            self.assertIn(
                'server.url "https://discord.gg/community"\n',
                config.read_text(),
            )
            self.assertEqual(config.read_text().count('server.url '), 1)
            config.unlink()
            self.assertIn(
                '\\"planned\\": 5, \\"applied\\": 5', converge(settings),
            )
            self.assertEqual(config.read_text().count('server.url '), 1)

    def test_rejects_invalid_optional_settings(self):
        tasks = yaml.safe_load(
            (ROOT / 'roles/rust_server/tasks/main.yml').read_text(),
        )
        conditions = [
            item for item in tasks[0]['ansible.builtin.assert']['that']
            if any(name in item for name in (
                'rust_headerimage', 'rust_logoimage', 'rust_url',
                'rust_saveinterval',
            ))
        ]
        with tempfile.TemporaryDirectory() as directory:
            playbook = Path(directory) / 'validate.yml'
            playbook.write_text(yaml.safe_dump([{
                'hosts': 'localhost',
                'gather_facts': False,
                'tasks': [{
                    'ansible.builtin.assert': {'that': conditions},
                }],
            }]))
            cases = [
                ({'rust_headerimage': 'https://example.org/image.gif'}, False),
                ({'rust_headerimage': 'https://example.org/image.png'}, True),
                ({'rust_logoimage': 'https://example.org/logo.gif'}, False),
                ({'rust_logoimage': 'https://example.org/logo.png'}, True),
                ({'rust_url': 'javascript:alert(1)'}, False),
                ({'rust_url': 'https://discord.gg/community'}, True),
                ({'rust_saveinterval': -1}, False),
                ({'rust_saveinterval': 300}, True),
            ]
            for variables, valid in cases:
                with self.subTest(variables=variables):
                    result = subprocess.run(
                        ['ansible-playbook', '-i', 'localhost,', '-c', 'local',
                         str(playbook), '-e', json.dumps(variables)],
                        capture_output=True, text=True, cwd=ROOT,
                    )
                    self.assertEqual(result.returncode == 0, valid, result.stdout)


if __name__ == '__main__':
    unittest.main()
