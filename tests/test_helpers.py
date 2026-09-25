"""Run on Linux without a Pi or root. Exercise destructive-operation boundaries."""
import hashlib
import gzip
import importlib.util
import json
from contextlib import ExitStack
from pathlib import Path
import socket
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kernel = load('kernel', 'roles/raspberry_pi/files/kernel_config.py')
storage = load('storage', 'roles/rust_storage/files/storage.py')
rootfs = load('rootfs', 'roles/fex/files/rootfs.py')
steam = load('steam', 'roles/rust_server/files/steam_manage.py')
backup = load('backup', 'roles/rust_backup/files/backup.py')
health = load('health', 'roles/rust_server/files/health.py')
service = load('service', 'roles/rust_server/files/service_state.py')


class ServiceTests(unittest.TestCase):
    def test_already_started_has_no_change(self):
        with tempfile.TemporaryFile(mode='w') as lock, patch('builtins.open', return_value=lock), patch.object(service.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            service.main('start')
            self.assertEqual(run.call_count, 1)

    def test_start_when_stopped(self):
        with tempfile.TemporaryFile(mode='w') as lock, patch('builtins.open', return_value=lock), patch.object(service.subprocess, 'run', side_effect=[SimpleNamespace(returncode=3), SimpleNamespace(returncode=0)]) as run:
            service.main('start')
            self.assertEqual(run.call_args.args[0], ['systemctl', 'start', 'rust.service'])

    def test_invalid_action(self):
        with self.assertRaises(ValueError):
            service.main('delete')


class KernelTests(unittest.TestCase):
    def test_adopt_correct(self):
        text = '[all]\nkernel=kernel8.img\ndtparam=audio=on\n'
        self.assertEqual(kernel.normalize(text), text)

    def test_sections_duplicates_and_second_run(self):
        text = '[pi5]\nkernel=kernel_2712.img\n[all]\nkernel=kernel8.img\n'
        result = kernel.normalize(text)
        self.assertEqual(result.count('kernel='), 1)
        self.assertTrue(result.endswith('[all]\nkernel=kernel8.img\n'))
        self.assertEqual(kernel.normalize(result), result)

    def test_includes_fail_closed(self):
        with self.assertRaises(ValueError):
            kernel.normalize('include custom.txt\n')


class StorageTests(unittest.TestCase):
    def safe(self, **changes):
        node = dict(type='part', fstype=None, mountpoints=[None])
        node.update(changes)
        return storage.blank_usb_partition(node, [node, dict(tran='usb', mountpoints=[None])])

    def test_blank_usb(self):
        self.assertTrue(self.safe())

    def test_no_whole_disk_format(self):
        self.assertFalse(self.safe(type='disk'))

    def test_no_existing_filesystem_format(self):
        self.assertFalse(self.safe(fstype='ext4'))

    def test_no_mounted_partition_format(self):
        self.assertFalse(self.safe(mountpoints=['/']))

    def test_no_sd_format(self):
        node = dict(type='part', fstype=None, mountpoints=[None])
        self.assertFalse(storage.blank_usb_partition(node, [dict(tran='mmc', mountpoints=[None])]))

    def test_no_mounted_ancestor(self):
        node = dict(type='part', fstype=None, mountpoints=[None])
        self.assertFalse(storage.blank_usb_partition(node, [dict(tran='usb', mountpoints=['/'])]))


class RootFSTests(unittest.TestCase):
    def test_adoption_and_second_run_are_non_destructive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / 'legacy'
            legacy.mkdir()
            (legacy / 'test').write_bytes(b'content')
            manifest = {'test': ['file', hashlib.sha256(b'content').hexdigest()]}
            (root / 'rootfs-manifest.json.gz').write_bytes(gzip.compress(json.dumps(manifest).encode()))
            target = root / 'managed'
            with patch.object(rootfs, '__file__', str(root / 'rootfs.py')), patch.object(rootfs.sys, 'argv', ['rootfs', str(target), str(legacy)]):
                self.assertEqual(rootfs.main(), 0)
                before = (target / 'test').stat().st_mtime_ns
                self.assertEqual(rootfs.main(), 0)
                self.assertEqual(before, (target / 'test').stat().st_mtime_ns)
            self.assertEqual((legacy / 'test').read_bytes(), b'content')

    def test_corrupt_managed_tree_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'managed'
            target.mkdir()
            (target / 'test').write_bytes(b'wrong')
            (root / 'rootfs-manifest.json.gz').write_bytes(gzip.compress(json.dumps({'test': ['file', 'expected']}).encode()))
            with patch.object(rootfs, '__file__', str(root / 'rootfs.py')), patch.object(rootfs.sys, 'argv', ['rootfs', str(target), str(root / 'legacy')]):
                with self.assertRaises(ValueError):
                    rootfs.main()
            self.assertEqual((target / 'test').read_bytes(), b'wrong')

    def test_matches_and_detects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            (root / 'bin/bash').write_bytes(b'ELF-test')
            (root / 'sh').symlink_to('bin/bash')
            manifest = {'bin': ['dir'], 'bin/bash': ['file', hashlib.sha256(b'ELF-test').hexdigest()],
                        'sh': ['link', 'bin/bash']}
            self.assertTrue(rootfs.matches(root, manifest))
            (root / 'bin/bash').write_bytes(b'changed')
            self.assertFalse(rootfs.matches(root, manifest))

    def test_wrong_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sh').symlink_to('/etc/passwd')
            self.assertFalse(rootfs.matches(root, {'sh': ['link', 'bin/bash']}))


class SteamTests(unittest.TestCase):
    def run_maintenance(self, mode, desired='42', fail_update=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            manifest = root / 'server/steamapps/appmanifest_258550.acf'
            manifest.parent.mkdir(parents=True)
            manifest.write_text('"AppState" { "buildid" "42" "StateFlags" "4" }')
            (root / 'server/RustDedicated').write_text('binary')
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                if '+app_info_print' in command:
                    output = '"258550" { "depots" { "branches" { "public" { "buildid" "' + desired + '" } } } }'
                    return SimpleNamespace(returncode=0, stdout=output, stderr='')
                if '+app_update' in command:
                    if fail_update:
                        return SimpleNamespace(returncode=1, stdout='download failed', stderr='')
                    manifest.write_text('"AppState" { "buildid" "' + desired + '" "StateFlags" "4" }')
                    return SimpleNamespace(returncode=0, stdout="Success! App '258550' fully installed.", stderr='')
                return SimpleNamespace(returncode=0)

            lock = stack.enter_context((root / 'lock').open('w'))
            stack.enter_context(patch('builtins.open', return_value=lock))
            stack.enter_context(patch.object(steam, 'Path', side_effect=lambda p: root / p.removeprefix('/srv/rust/')))
            stack.enter_context(patch.object(steam.subprocess, 'check_output', side_effect=lambda cmd, **kw: 'usb-uuid' if cmd[0] == 'findmnt' else 'success'))
            stack.enter_context(patch.object(steam.subprocess, 'run', side_effect=fake_run))
            stack.enter_context(patch.object(steam.shutil, 'disk_usage', return_value=SimpleNamespace(free=20 * 1024**3)))
            if fail_update:
                with self.assertRaises(RuntimeError):
                    steam.main('/rootfs', '/FEX', 'usb-uuid', mode)
            else:
                steam.main('/rootfs', '/FEX', 'usb-uuid', mode)
            return calls

    def test_normal_converge_does_not_call_steam_or_stop(self):
        self.assertEqual(self.run_maintenance('install'), [])

    def test_unchanged_explicit_update_does_not_stop(self):
        calls = self.run_maintenance('update')
        self.assertEqual(len(calls), 1)
        self.assertIn('+app_info_print', calls[0])

    def test_changed_build_stops_before_download(self):
        calls = self.run_maintenance('update', desired='43')
        stop = calls.index(['systemctl', 'stop', 'rust.service'])
        install = next(i for i, cmd in enumerate(calls) if '+app_update' in cmd)
        self.assertLess(stop, install)

    def test_failed_download_never_restarts(self):
        calls = self.run_maintenance('update', desired='43', fail_update=True)
        self.assertIn(['systemctl', 'stop', 'rust.service'], calls)
        self.assertFalse(any('start' in cmd or 'restart' in cmd for cmd in calls))

    def test_parse_build_with_log_noise(self):
        output = 'Steam log\n"258550" { "depots" { "branches" { "public" { "buildid" "123456" } } } }\nQuit\n'
        self.assertEqual(steam.public_build(output), '123456')

    def test_missing_or_truncated_metadata(self):
        for output in ['No connection', '"258550" { "depots" {']:
            with self.assertRaises(ValueError):
                steam.public_build(output)

    def test_partial_install_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'appmanifest.acf'
            path.write_text('"AppState" { "buildid" "42" "StateFlags" "1026" }')
            self.assertIsNone(steam.installed_build(path))
            path.write_text('"AppState" { "buildid" "42" "StateFlags" "4" }')
            self.assertEqual(steam.installed_build(path), '42')


class BackupTests(unittest.TestCase):
    def test_safe_member(self):
        backup.validate_members([tarfile.TarInfo('data/kp-pi5/player.db')])

    def test_unsafe_paths(self):
        for name in ['/etc/passwd', 'data/../../escape', 'other/file']:
            with self.assertRaises(ValueError):
                backup.validate_members([tarfile.TarInfo(name)])

    def test_links_and_devices_refused(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE]:
            member = tarfile.TarInfo('data/escape')
            member.type = kind
            with self.assertRaises(ValueError):
                backup.validate_members([member])


class HealthTests(unittest.TestCase):
    def test_failed_service_stops_without_waiting(self):
        with patch.object(health, 'service_status', return_value={'ActiveState': 'failed'}), patch.object(health, 'query') as query:
            with self.assertRaisesRegex(RuntimeError, 'not running'):
                health.wait_ready(1800)
            query.assert_not_called()

    def test_healthy_service_and_query_pass(self):
        with patch.object(health, 'service_status', return_value={'ActiveState': 'active', 'NRestarts': '0'}), patch.object(health, 'query', return_value=True):
            health.wait_ready(1800)

    def test_restart_loop_stops_early(self):
        states = [{'ActiveState': 'activating', 'NRestarts': str(n)} for n in range(4)]
        with patch.object(health, 'service_status', side_effect=states), patch.object(health, 'query', return_value=False), patch.object(health.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'repeatedly restarted'):
                health.wait_ready(1800)

    def test_timeout_keeps_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            health.wait_ready(0)

    def test_diagnostics_include_journal_even_when_status_exits_nonzero(self):
        outputs = [SimpleNamespace(stdout='failed status', stderr='', returncode=3),
                   SimpleNamespace(stdout='actual startup error', stderr='', returncode=0)]
        with patch.object(health.subprocess, 'run', side_effect=outputs):
            message = health.diagnose('not ready')
        self.assertIn('failed status', message)
        self.assertIn('actual startup error', message)

    def test_challenge_round_trip(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(('127.0.0.1', 0))
            server.settimeout(5)
            received = []

            def respond():
                request, address = server.recvfrom(1024)
                server.sendto(b'\xff\xff\xff\xffA1234', address)
                challenged, address = server.recvfrom(1024)
                received.extend([request, challenged])
                server.sendto(b'\xff\xff\xff\xffI' + b'\x11server\x00', address)

            worker = threading.Thread(target=respond)
            worker.start()
            self.assertTrue(health.query(port=server.getsockname()[1]))
            worker.join()
            self.assertEqual(received[1], received[0] + b'1234')


if __name__ == '__main__':
    unittest.main()
