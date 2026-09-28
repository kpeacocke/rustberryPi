"""Restore selection and preservation tests using real temporary archives."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('restore_backup', ROOT / 'roles/rust_backup/files/backup.py')
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
META = {'identity': 'world', 'seed': 1, 'worldsize': 1500, 'uid': 2001}


def archive_at(root, stamp, metadata=None):
    path = root / ('rust-' + stamp + '.tar.gz')
    with tarfile.open(path, 'w:gz') as archive:
        for name, value in [('data/deployment.json', json.dumps(metadata or META).encode()),
                            ('data/world/save.sav', b'backed up world')]:
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix('.gz.sha256').write_text(digest + '  ' + path.name + '\n')
    return path, digest


class RestoreTests(unittest.TestCase):
    def test_latest_completed_selected_and_unpublished_archive_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive_at(root, '20260101T010000.000000Z')
            latest, digest = archive_at(root, '20260102T010000.000000Z')
            (root / 'rust-20260103T010000.000000Z.tar.gz').write_bytes(b'incomplete')
            self.assertEqual(backup.latest_archive(root), {'archive': str(latest), 'sha256': digest})

    def test_corrupt_latest_does_not_silently_choose_older_world(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive_at(root, '20260101T010000.000000Z')
            latest, _ = archive_at(root, '20260102T010000.000000Z')
            latest.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                backup.latest_archive(root)

    def test_no_archive_and_unsafe_members_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError):
                backup.latest_archive(root)
            path = root / 'unsafe.tar.gz'
            with tarfile.open(path, 'w:gz') as archive:
                archive.addfile(tarfile.TarInfo('../outside'))
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                backup.checked_archive(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_restore_preserves_current_world_and_defers_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            data.mkdir()
            (data / 'deployment.json').write_text(json.dumps(META))
            (data / 'current.sav').write_text('current world')
            archive, digest = archive_at(root, '20260101T010000.000000Z')
            def mapped(value):
                return {'/srv/rust': root, '/srv/rust/data': data,
                        '/var/lib/rustberrypi-backup.json': root / 'status.json'}.get(str(value), Path(value))
            real_mkdtemp = tempfile.mkdtemp
            with patch.object(backup.tempfile, 'mkdtemp', side_effect=lambda **kw: real_mkdtemp(prefix=kw['prefix'], dir=root)), \
                    patch.object(backup, 'Path', side_effect=mapped), \
                    patch.object(backup, 'mounted'), \
                    patch.object(backup, 'open', side_effect=lambda *a: open(root / 'lock', 'w'), create=True), \
                    patch.object(backup.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                    patch.object(backup.subprocess, 'check_output', return_value='success'), \
                    patch.object(sys, 'argv', ['backup', 'restore', '--archive', str(archive),
                                              '--sha256', digest, '--replace', '--leave-stopped']):
                backup.main()
            self.assertEqual((data / 'world/save.sav').read_bytes(), b'backed up world')
            previous = list(root.glob('data.pre-restore-*'))
            self.assertEqual(len(previous), 1)
            self.assertEqual((previous[0] / 'current.sav').read_text(), 'current world')
            self.assertTrue(json.loads((root / 'status.json').read_text())['restore_success'])
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(['systemctl', 'stop', 'rust.service'], commands)
            self.assertNotIn(['systemctl', 'start', 'rust.service'], commands)
