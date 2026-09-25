"""Catch YAML mappings/booleans in conditions that syntax-check does not reject."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class ConditionalTypesTests(unittest.TestCase):
    def test_task_conditions_are_strings(self):
        def inspect(value, path):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ('when', 'failed_when', 'changed_when', 'until', 'that'):
                        # Literal booleans are legitimate changed_when/failed_when values.
                        if key in ('changed_when', 'failed_when') and isinstance(item, bool):
                            continue
                        conditions = item if isinstance(item, list) else [item]
                        for condition in conditions:
                            self.assertIsInstance(condition, str, f'{path}: {key}: {condition!r}')
                    inspect(item, path)
            elif isinstance(value, list):
                for item in value:
                    inspect(item, path)

        for base in ('roles', 'playbooks'):
            for path in (ROOT / base).rglob('*.yml'):
                if base == 'roles' and not any(part in path.parts for part in ('tasks', 'handlers')):
                    continue
                inspect(yaml.safe_load(path.read_text()), path.relative_to(ROOT))
