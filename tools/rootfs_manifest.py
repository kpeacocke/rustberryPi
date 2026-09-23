#!/usr/bin/env python3
"""Generate reference hashes from a verified SquashFS image. Requires Linux."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('image', type=Path)
    parser.add_argument('sha256')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    with args.image.open('rb') as file:
        if hashlib.file_digest(file, 'sha256').hexdigest() != args.sha256:
            raise ValueError('Image checksum mismatch')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'tree'
        subprocess.run(['unsquashfs', '-no-progress', '-d', str(root), str(args.image)], check=True)
        manifest = {}
        for parent, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                path = Path(parent) / name
                key = str(path.relative_to(root))
                if path.is_symlink():
                    manifest[key] = ['link', str(path.readlink())]
                elif path.is_dir():
                    manifest[key] = ['dir']
                else:
                    with path.open('rb') as file:
                        manifest[key] = ['file', hashlib.file_digest(file, 'sha256').hexdigest()]
        args.output.write_bytes(gzip.compress(json.dumps(manifest, sort_keys=True).encode(), mtime=0))


if __name__ == '__main__':
    main()
