"""Hailo release selection, candidate integrity and DKMS layout checks."""

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


class HailoCandidateIntegrityTests(unittest.TestCase):
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
