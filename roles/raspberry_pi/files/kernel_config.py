#!/usr/bin/env python3
"""Preserve unrelated firmware settings; normalize kernel directives once."""
import re
import shutil
import sys
from pathlib import Path


def normalize(text):
    lines = text.splitlines()
    active = [(i, line) for i, line in enumerate(lines)
              if re.match(r"^\s*kernel\s*=", line)]
    section = "all"
    contexts = {}
    for i, line in enumerate(lines):
        if re.match(r"^\s*\[.*\]", line):
            section = line.strip().split(']')[0][1:]
        contexts[i] = section
    # A later included file could override this directive: never guess.
    if any(re.match(r"^\s*include\s+", line) for line in lines):
        raise ValueError("config.txt includes other files; review and flatten kernel settings first")
    if len(active) == 1 and active[0][1].split('#')[0].strip() == 'kernel=kernel8.img' and contexts[active[0][0]] == 'all':
        return text
    kept = [line for line in lines if not re.match(r"^\s*kernel\s*=", line)]
    return '\n'.join(kept).rstrip() + '\n\n[all]\nkernel=kernel8.img\n'


if __name__ == '__main__':
    path = Path(sys.argv[1])
    before = path.read_text()
    after = normalize(before)
    if before != after:
        backup = path.with_name(path.name + '.pre-ansible')
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(after)
        print('changed')
