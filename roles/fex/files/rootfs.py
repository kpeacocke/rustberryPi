#!/usr/bin/env python3
"""Adopt only byte-identical RootFS contents; never remove a nonmatching tree."""
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def matches(root, manifest):
    if not root.is_dir():
        return False
    for name, expected in manifest.items():
        path = root / name
        try:
            if expected[0] == 'link':
                if not path.is_symlink() or str(path.readlink()) != expected[1]:
                    return False
            elif expected[0] == 'dir':
                if path.is_symlink() or not path.is_dir():
                    return False
            else:
                if path.is_symlink() or not path.is_file():
                    return False
                with path.open('rb') as file:
                    if hashlib.file_digest(file, 'sha256').hexdigest() != expected[1]:
                        return False
        except (OSError, ValueError):
            return False
    return True


def main():
    target, legacy = map(Path, sys.argv[1:3])
    manifest = json.loads(gzip.decompress(Path(__file__).with_name('rootfs-manifest.json.gz').read_bytes()))
    if matches(target, manifest):
        return 0
    if target.exists():
        raise ValueError('Managed RootFS differs from pin; preserved for review. Choose a new versioned path.')
    adopt = matches(legacy, manifest)
    if not adopt and len(sys.argv) < 4:
        return 3
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.rootfs-', dir=target.parent))
    try:
        if adopt:
            shutil.copytree(legacy, stage / 'tree', symlinks=True)
        else:
            subprocess.run(['unsquashfs', '-no-progress', '-processors', '2', '-d', str(stage / 'tree'), sys.argv[3]], check=True, stdout=sys.stderr)
        if not matches(stage / 'tree', manifest):
            raise ValueError('Staged RootFS failed content validation')
        (stage / 'tree').rename(target)
    finally:
        shutil.rmtree(stage)
    print('adopted' if adopt else 'installed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
