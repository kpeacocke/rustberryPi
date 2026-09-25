import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location('check_nas', Path(__file__).resolve().parents[1] / 'roles/rust_nas/files/check_nas.py')
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class NasTests(unittest.TestCase):
    def test_unmounted_wrong_share_and_autofs_never_write(self):
        for fs, source, target in [('ext4', '/dev/root', '/'), ('autofs', 'systemd-1', '/mnt/nas'),
                                   ('cifs', '//wrong/share', '/mnt/nas')]:
            reply = {'filesystems': [{'target': target, 'source': source, 'fstype': fs}]}
            with patch.object(nas.os, 'scandir') as scan, \
                    patch.object(nas.subprocess, 'check_output', return_value=json.dumps(reply)), \
                    patch.object(nas.tempfile, 'NamedTemporaryFile') as temporary:
                scan.return_value.__enter__.return_value = iter([])
                with self.assertRaises(RuntimeError):
                    nas.check('/mnt/nas', '//nas/share')
                temporary.assert_not_called()

    def test_activation_precedes_identity_check_and_verified_write(self):
        events = []
        scan = MagicMock()
        scan.__enter__.side_effect = lambda: events.append('activate')
        def lookup(*args, **kwargs):
            events.append('identity')
            return json.dumps({'filesystems': [{'target': '/mnt/nas', 'source': '//nas/share', 'fstype': 'cifs'}]})
        stream = MagicMock()
        stream.read.return_value = b'rustberryPi backup storage check'
        temporary = MagicMock()
        temporary.__enter__.return_value = stream
        with patch.object(nas.os, 'scandir', return_value=scan), \
                patch.object(nas.subprocess, 'check_output', side_effect=lookup), \
                patch.object(nas.Path, 'stat', side_effect=[SimpleNamespace(st_dev=3), SimpleNamespace(st_dev=1), SimpleNamespace(st_dev=2)]), \
                patch.object(nas.tempfile, 'NamedTemporaryFile', return_value=temporary), \
                patch.object(nas.os, 'fsync'):
            nas.check('/mnt/nas', '//nas/share')
        self.assertEqual(events, ['activate', 'identity'])
        stream.write.assert_called_once()
        temporary.__exit__.assert_called_once()
