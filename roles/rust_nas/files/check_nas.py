#!/usr/bin/env python3
"""Activate an automount and verify its identity before attempting any write."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def check(path, expected):
    path = Path(path)
    # stat(mountpoint) can describe the covered directory before autofs triggers.
    # Opening the directory forces resolution; do not create anything yet.
    with os.scandir(path):
        pass
    result = subprocess.check_output(
        ['findmnt', '--json', '--target', str(path), '--output', 'TARGET,SOURCE,FSTYPE'],
        text=True, timeout=35)
    mounts = json.loads(result).get('filesystems', [])
    if len(mounts) != 1:
        raise RuntimeError('Could not identify a single active NAS filesystem; no write attempted')
    mount = mounts[0]
    if (mount.get('fstype') != 'cifs' or mount.get('source') != expected
            or Path(mount.get('target', '/')) != path):
        raise RuntimeError('Expected SMB share is not active at the NAS mountpoint; no write attempted')
    if path.stat().st_dev in (Path('/').stat().st_dev, Path('/srv/rust').stat().st_dev):
        raise RuntimeError('NAS filesystem is not separate from OS/Rust storage; no write attempted')
    # Named temporary files are portable to SMB servers without O_TMPFILE support.
    with tempfile.NamedTemporaryFile(dir=path, prefix='.rustberrypi-check-') as stream:
        stream.write(b'rustberryPi backup storage check')
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        if stream.read() != b'rustberryPi backup storage check':
            raise RuntimeError('NAS readback did not match the test write')
    print('Verified expected SMB share and temporary write/read access')


if __name__ == '__main__':
    try:
        check(*sys.argv[1:])
    except RuntimeError as error:
        sys.exit(str(error))
    except Exception as error:
        sys.exit('NAS activation or access failed: ' + type(error).__name__ +
                 '. Check the mount unit journal and Synology share permissions.')
