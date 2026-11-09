"""Hailo release selection, candidate integrity and DKMS layout checks."""

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


candidate = load_module("hailo_candidate", "tools/hailo_candidate.py")
installer = load_module("hailo_install", "tools/hailo_install.py")
release_source = load_module(
    "hailo_release_source",
    "extensions/eda/plugins/event_source/hailo_release.py",
)


class HailoEventSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_source_emits_release_without_installation(self):
        queue = asyncio.Queue()
        with patch.object(
            release_source, "release_version", return_value="4.24.0"
        ), patch.object(
            release_source.asyncio, "sleep", side_effect=asyncio.CancelledError
        ):
            with self.assertRaises(asyncio.CancelledError):
                await release_source.main(queue, {"poll_interval": 3600})
        self.assertEqual(
            queue.get_nowait(), {"hailo_release": {"version": "4.24.0"}}
        )
        rule = yaml.safe_load(
            (ROOT / "rulebooks/hailo-candidate.yml").read_text()
        )[0]
        self.assertEqual(
            rule["rules"][0]["action"]["run_job_template"]["name"],
            "RustberryPi - Hailo candidate",
        )

    async def test_failed_release_check_never_emits_available_event(self):
        queue = asyncio.Queue()
        with patch.object(
            release_source, "release_version", side_effect=RuntimeError("API")
        ), patch.object(
            release_source.asyncio, "sleep", side_effect=asyncio.CancelledError
        ), self.assertLogs(level="ERROR"):
            with self.assertRaises(asyncio.CancelledError):
                await release_source.main(queue, {})
        self.assertTrue(queue.empty())


class HailoReleaseTests(unittest.TestCase):
    def test_only_common_stable_hailo8_releases_are_selected(self):
        self.assertEqual(candidate.version_key("v4.24.0"), (4, 24, 0))
        for invalid in ("5.4.0", "v4.25.0-rc1", "4.24", "latest"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(candidate.version_key(invalid))
        self.assertEqual(
            candidate.latest_common_release(
                {"v4.23.0", "v4.24.0", "v5.0.0"},
                {"v4.24.0", "v5.0.0"},
            ),
            "4.24.0",
        )

    def test_no_common_compatible_release_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "No common stable"):
            candidate.latest_common_release({"v4.24.0"}, {"v5.0.0"})

    def test_firmware_gate_rejects_before_hardware_or_network_access(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            candidate.platform, "machine"
        ) as machine:
            with self.assertRaisesRegex(RuntimeError, "EULA"):
                candidate.build_candidate(Path(directory), "4.24.0", False)
            machine.assert_not_called()

    def test_candidate_storage_cannot_be_redirected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "fixed at"):
                candidate.build_candidate(Path(directory), "4.24.0", True)

    def test_runtime_configures_from_upstream_repository_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(candidate, "ROOT_DEFAULT", root), patch.object(
                candidate.os, "geteuid", return_value=0
            ), patch.object(
                candidate.platform, "machine", return_value="aarch64"
            ), patch.object(
                Path, "is_file", lambda path: path.name == "Makefile"
            ), patch.object(
                candidate, "clone_repository"
            ), patch.object(
                candidate, "run", side_effect=RuntimeError("configure probe")
            ) as command:
                with self.assertRaisesRegex(RuntimeError, "configure probe"):
                    candidate.build_candidate(root, "4.24.0", True)
            argv = command.call_args.args[0]
            self.assertEqual(
                argv[argv.index("-S") + 1],
                str(root / "candidates/4.24.0/source/hailort"),
            )

    def test_ready_candidate_for_old_kernel_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "candidates/4.24.0"
            path.mkdir(parents=True)
            (path / "READY").touch()
            (path / "manifest.json").write_text(json.dumps({
                "architecture": "aarch64", "kernel_release": "older-kernel",
            }))
            is_file = Path.is_file
            with patch.object(candidate, "ROOT_DEFAULT", root), \
                    patch.object(candidate.os, "geteuid", return_value=0), \
                    patch.object(candidate.platform, "machine", return_value="aarch64"), \
                    patch.object(Path, "is_file", lambda p: p.name == "Makefile" or is_file(p)), \
                    patch.object(candidate, "clone_repository"), \
                    patch.object(candidate, "run", side_effect=RuntimeError("build reached")):
                with self.assertRaisesRegex(RuntimeError, "build reached"):
                    candidate.build_candidate(root, "4.24.0", True)
            self.assertFalse((path / "READY").exists())


class HailoCandidateIntegrityTests(unittest.TestCase):
    def test_selection_requires_newer_candidate_for_running_kernel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for version in ("4.24.0", "4.25.0"):
                path = root / "candidates" / version
                path.mkdir(parents=True)
                (path / "READY").touch()

            def verify(storage, version):
                return storage / "candidates" / version, {
                    "architecture": "aarch64",
                    "kernel_release": (
                        "older-kernel"
                        if version == "4.25.0"
                        else candidate.os.uname().release
                    ),
                }

            with patch.object(
                installer, "verify_candidate", side_effect=verify
            ), patch.object(
                installer,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="4.23.0"),
            ):
                self.assertEqual(installer.select_candidate(root), "4.24.0")
                with self.assertRaisesRegex(ValueError, "running kernel"):
                    installer.select_candidate(root, "4.25.0")

    def test_selection_rejects_missing_or_already_installed_candidates(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            installer,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="4.24.0"),
        ):
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "No ready"):
                installer.select_candidate(root)
            with patch.object(
                installer,
                "verify_candidate",
                return_value=(
                    root,
                    {
                        "architecture": "aarch64",
                        "kernel_release": candidate.os.uname().release,
                    },
                ),
            ):
                with self.assertRaisesRegex(ValueError, "newer"):
                    installer.select_candidate(root, "4.24.0")

    def make_candidate(self, root, version="4.24.0"):
        path = root / "candidates" / version
        files = {
            "driver/hailo_pci.ko": b"driver",
            "firmware/hailo8_fw.bin": b"firmware",
            "runtime/bin/hailortcli": b"cli",
            "runtime/lib/libhailort.so.4.24.0": b"library",
            "venv/bin/python": b"python",
            "source/driver/linux/pcie/dkms.conf.in": b"config",
            (
                "source/hailort/libhailort/bindings/python/"
                "platform/dist/hailort.whl"
            ): b"wheel",
        }
        hashes = {}
        for relative, contents in files.items():
            file_path = path / relative
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(contents)
            hashes[relative] = hashlib.sha256(contents).hexdigest()
        (path / "READY").write_text("candidate-built\n")
        (path / "manifest.json").write_text(
            json.dumps(
                {
                    "version": version,
                    "architecture": "aarch64",
                    "kernel_release": candidate.os.uname().release,
                    "candidate_only": True,
                    "firmware_eula_accepted": True,
                    "firmware_url": (
                        "https://hailo-hailort.s3.eu-west-2.amazonaws.com/"
                        f"Hailo8/{version}/FW/hailo8_fw.{version}.bin"
                    ),
                    "source_commits": {
                        "hailort": "a" * 40,
                        "driver": "a" * 40,
                    },
                    "sha256": hashes,
                }
            )
        )
        return path

    def test_status_requires_integrity_not_just_ready_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.make_candidate(root)

            def command(argv, **kwargs):
                return SimpleNamespace(
                    returncode=0, stdout="4.23.0" if argv[0] == "modinfo" else "a" * 40)

            with patch.object(installer, "run", side_effect=command):
                status = installer.candidate_status(root)
                self.assertTrue(status["candidate_ready"])
                self.assertEqual(status["available"], "4.24.0")
                (path / "firmware/hailo8_fw.bin").write_bytes(b"corrupt")
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    installer.candidate_status(root)

    def test_no_candidate_is_distinct_from_failed_installed_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(installer, "run",
                              return_value=SimpleNamespace(returncode=0, stdout="4.23.0")):
                status = installer.candidate_status(root)
                self.assertFalse(status["candidate_ready"])
                self.assertIsNone(status["available"])
            with patch.object(installer, "run",
                              return_value=SimpleNamespace(returncode=1, stdout="")):
                with self.assertRaisesRegex(RuntimeError, "installed"):
                    installer.candidate_status(root)

    def test_candidate_manifest_and_hashes_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.make_candidate(root)
            with patch.object(
                installer,
                "run",
                return_value=SimpleNamespace(stdout="a" * 40 + "\n"),
            ):
                verified, manifest = installer.verify_candidate(root, "4.24.0")
            self.assertEqual(verified, path)
            self.assertEqual(manifest["version"], "4.24.0")
            (path / "firmware/hailo8_fw.bin").write_bytes(b"changed")
            with patch.object(
                installer,
                "run",
                return_value=SimpleNamespace(stdout="a" * 40 + "\n"),
            ), self.assertRaisesRegex(ValueError, "checksum mismatch"):
                installer.verify_candidate(root, "4.24.0")

    def test_candidate_cannot_be_activated_from_nonstable_or_incomplete_path(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "stable Hailo"):
                installer.verify_candidate(root, "../other")
            with self.assertRaisesRegex(ValueError, "No complete local"):
                installer.verify_candidate(root, "4.24.0")

    def test_dkms_source_tree_matches_upstream_module_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "driver"
            for relative in (
                "linux/pcie/dkms.conf.in",
                "common/include",
                "common/src",
                "linux/vdma",
                "linux/utils",
            ):
                path = driver / relative
                if relative.endswith(".in"):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        "PACKAGE_NAME=hailo_pci\n"
                        "PACKAGE_VERSION=@PCIE_DRIVER_VERSION@\n"
                    )
                else:
                    path.mkdir(parents=True, exist_ok=True)
                    (path / "source.c").write_text("source")
            installed = installer.copy_dkms_source(
                driver, "4.24.0", source_root=root / "usr-src"
            )
            self.assertTrue(
                (
                    installed / "hailort/drivers/linux/pcie/dkms.conf.in"
                ).is_file()
            )
            self.assertTrue(
                (installed / "hailort/drivers/common/src/source.c").is_file()
            )
            self.assertTrue((installed / "common/include/source.c").is_file())
            self.assertIn(
                "PACKAGE_VERSION=4.24.0",
                (installed / "dkms.conf").read_text(),
            )

    def test_rollback_restores_only_hailo_package_hold_changes(self):
        with patch.object(
            installer,
            "run",
            return_value=SimpleNamespace(
                stdout="hailort-pcie-driver\nunrelated-package\n"
            ),
        ) as command:
            installer.restore_package_holds({"hailort"})
        self.assertEqual(
            [call.args[0] for call in command.call_args_list],
            [
                ["apt-mark", "showhold"],
                ["apt-mark", "unhold", "hailort-pcie-driver"],
                ["apt-mark", "hold", "hailort"],
            ],
        )


class HailoAnsibleWorkflowTests(unittest.TestCase):
    def tasks(self, relative):
        return yaml.safe_load((ROOT / relative).read_text())

    def test_hailo_candidate_uses_eula_gate_and_never_installs(self):
        role = self.tasks("roles/hailo_candidate/tasks/main.yml")
        approved = role[0]["ansible.builtin.assert"]["that"]
        self.assertIn(
            "hailo_candidate_firmware_eula_accepted | bool", approved
        )
        build = next(
            task
            for task in role
            if task["name"].startswith("Check for and build")
        )
        self.assertIn(
            "--firmware-eula-accepted",
            build["ansible.builtin.command"]["argv"],
        )
        self.assertNotIn("systemd_service", str(role))

    def test_hailo_maintenance_checks_candidate_before_downtime(self):
        tasks = self.tasks("roles/rust_maintenance/tasks/main.yml")
        validation = tasks[0]["ansible.builtin.assert"]["that"]
        self.assertIn("'hailo'", validation[1])
        candidate_check = next(
            index
            for index, task in enumerate(tasks)
            if task["name"]
            == "Require completed Hailo candidate before downtime"
        )
        nas_check = next(
            index
            for index, task in enumerate(tasks)
            if task["name"]
            == "Verify NAS is mounted before announcing downtime"
        )
        self.assertLess(candidate_check, nas_check)
        workflow = next(
            task
            for task in tasks
            if task["name"]
            == "Execute selected jobs inside one maintenance window"
        )
        activation = next(
            task
            for task in workflow["block"]
            if task["name"] == "Activate approved Hailo source candidate"
        )
        self.assertEqual(
            activation["when"], "rust_maintenance_target == 'hailo'"
        )
        install_tasks = self.tasks("roles/rust_maintenance/tasks/hailo.yml")
        install = next(
            task
            for task in install_tasks
            if task["name"]
            == "Activate and hardware-verify selected Hailo candidate"
        )
        self.assertEqual(
            install["failed_when"],
            "rust_maintenance_hailo_install.rc not in [0, 10]",
        )

    def test_existing_provisioning_respects_source_managed_runtime(self):
        tasks = self.tasks("roles/pi_hardware/tasks/hailo.yml")
        driver = next(
            task
            for task in tasks
            if task["name"]
            == "Install minimal Hailo-8 driver and matching runtime"
        )
        self.assertEqual(
            driver["when"],
            "not pi_hardware_hailo_source_managed.stat.exists",
        )
        text = (ROOT / "roles/pi_hardware/tasks/hailo.yml").read_text()
        self.assertIn("/opt/rustberrypi-hailo/current/venv/bin/python", text)
        self.assertIn(
            "/opt/rustberrypi-hailo/current/runtime/bin/hailortcli", text
        )


if __name__ == "__main__":
    unittest.main()
