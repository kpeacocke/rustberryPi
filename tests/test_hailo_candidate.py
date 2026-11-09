"""Hailo release selection, candidate integrity and DKMS layout checks."""

import asyncio
from contextlib import nullcontext
import hashlib
import importlib.util
import json
import io
import stat
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from jinja2 import Environment, StrictUndefined

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
    def test_exact_override_must_be_a_common_stable_release(self):
        with patch.object(candidate, "get_release_tags", side_effect=[
            {"v4.23.0", "v4.24.0"}, {"v4.23.0", "v4.24.0"},
        ]):
            self.assertEqual(candidate.select_release("4.23.0"), "4.23.0")
        for version in ("4.25.0", "v4.24.0", "5.0.0", "4.24.0-rc1"):
            with self.subTest(version=version), patch.object(
                candidate, "get_release_tags", return_value={"v4.24.0"}
            ), self.assertRaises(ValueError):
                candidate.select_release(version)

    def test_all_release_preflight_checks_installed_without_building(self):
        with patch("sys.argv", ["builder", "--firmware-eula-accepted",
                                "--select-release", "--version", "4.24.0"]), \
                patch.object(candidate, "get_release_tags", return_value={"v4.24.0"}), \
                patch.object(candidate.subprocess, "run",
                             return_value=SimpleNamespace(stdout="4.23.0")) as probe, \
                patch.object(candidate, "build_candidate") as build, \
                patch.object(candidate, "candidate_lock", return_value=nullcontext()) as lock, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            candidate.main()
        self.assertEqual(output.getvalue().strip(), "4.24.0")
        self.assertTrue(probe.call_args.kwargs["check"])
        build.assert_not_called()
        lock.assert_not_called()

    def test_post_os_build_uses_pinned_release_not_new_upstream_release(self):
        with patch("sys.argv", ["builder", "--firmware-eula-accepted",
                                "--maintenance", "--version", "4.24.0"]), \
                patch.object(candidate, "select_release") as select, \
                patch.object(candidate, "candidate_lock", return_value=nullcontext()), \
                patch.object(candidate, "require_build_window"), \
                patch.object(Path, "exists", return_value=False), \
                patch.object(candidate.subprocess, "run",
                             return_value=SimpleNamespace(returncode=0, stdout="4.23.0")), \
                patch.object(candidate, "build_candidate") as build:
            candidate.main()
        select.assert_not_called()
        build.assert_called_once_with(candidate.ROOT_DEFAULT, "4.24.0", True)

    def test_already_current_builder_is_an_explicit_noop(self):
        with patch("sys.argv", ["builder", "--firmware-eula-accepted",
                                "--maintenance", "--version", "4.24.0"]), \
                patch.object(Path, "exists", return_value=False), \
                patch.object(candidate, "candidate_lock", return_value=nullcontext()), \
                patch.object(candidate, "require_build_window"), \
                patch.object(candidate.subprocess, "run",
                             return_value=SimpleNamespace(returncode=0, stdout="4.24.0")), \
                patch.object(candidate, "build_candidate") as build, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            candidate.main()
        build.assert_not_called()
        self.assertIn("is current", output.getvalue())

    def test_eda_build_cannot_enter_maintenance_owned_window(self):
        for window, maintenance in ((True, False), (False, True)):
            with self.subTest(window=window, maintenance=maintenance), \
                    patch.object(Path, "exists", return_value=window), \
                    self.assertRaisesRegex(RuntimeError, "blocked during maintenance"):
                candidate.require_build_window(maintenance)
        with patch.object(Path, "exists", return_value=True):
            candidate.require_build_window(True)

    def test_builder_and_installer_serialize_on_the_same_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locks = []
            for module in (candidate, installer):
                with patch.object(module.fcntl, "flock") as flock:
                    with module.candidate_lock(root):
                        locks.append((root / ".candidate.lock").stat().st_ino)
                    self.assertTrue(
                        flock.call_args.args[1] & module.fcntl.LOCK_EX
                    )
            self.assertEqual(locks[0], locks[1])
            with installer.candidate_lock(root):
                contender = candidate.subprocess.run([
                    candidate.sys.executable, "-c",
                    "import fcntl,sys; "
                    "f=open(sys.argv[1]); "
                    "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)",
                    str(root / ".candidate.lock"),
                ], capture_output=True, text=True, check=False)
            self.assertNotEqual(contender.returncode, 0)
            self.assertIn("BlockingIOError", contender.stderr)

    def test_lock_permissions_are_private_and_existing_files_are_hardened(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".candidate.lock"
            for module in (candidate, installer):
                path.unlink(missing_ok=True)
                with module.candidate_lock(root):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                path.chmod(0o644)
                with module.candidate_lock(root):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                path.unlink()
                target = root / "unchanged"
                target.touch()
                target.chmod(0o644)
                path.symlink_to(target)
                with self.assertRaises(OSError):
                    with module.candidate_lock(root):
                        self.fail("Symlink lock must not be opened")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

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
    def test_current_maintenance_checks_actual_kernel_and_hardware(self):
        kernel = candidate.os.uname().release
        def command(argv, **kwargs):
            field = argv[2] if argv[0] == "modinfo" else ""
            return SimpleNamespace(stdout={"version": "4.24.0",
                                           "vermagic": kernel + " SMP"} .get(field, ""))
        with patch.object(installer, "run", side_effect=command) as run, \
                patch.object(Path, "is_file", return_value=False), \
                patch.object(installer, "_write_private_json"), \
                patch.object(installer, "_clear_failure"):
            installer.verify_installed(Path("unused"), "4.24.0")
        self.assertIn(["hailortcli", "fw-control", "identify"],
                      [call.args[0] for call in run.call_args_list])
        with patch.object(installer, "run", side_effect=[
            SimpleNamespace(stdout="4.24.0"), SimpleNamespace(stdout="older SMP"),
        ]), self.assertRaisesRegex(ValueError, "running kernel"):
            installer.maintenance_status(Path("unused"), "4.24.0")

    def test_selected_invalid_artifact_is_not_treated_as_current(self):
        with patch.object(installer, "run",
                          return_value=SimpleNamespace(stdout="4.23.0", returncode=0)), \
                patch.object(installer, "verify_artifact_integrity",
                             side_effect=ValueError("checksum mismatch")), \
                self.assertRaisesRegex(ValueError, "checksum mismatch"):
            installer.maintenance_status(Path("unused"), "4.24.0")
        with patch.object(installer, "run",
                          return_value=SimpleNamespace(stdout="4.24.0", returncode=0)), \
                patch.object(Path, "exists", return_value=True), \
                patch.object(installer, "verify_artifact_integrity",
                             side_effect=ValueError("checksum mismatch")), \
                self.assertRaisesRegex(ValueError, "checksum mismatch"):
            installer.maintenance_status(Path("unused"), "4.24.0")

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
                installer, "verify_artifact_integrity", side_effect=verify
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
                "verify_artifact_integrity",
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

    def test_current_source_runtime_survives_healthy_post_os_kernel_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = self.make_candidate(root)
            manifest_path = path / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["kernel_release"] = "historical-build-kernel"
            manifest_path.write_text(json.dumps(manifest))
            original_manifest = manifest_path.read_bytes()
            (root / "active-version").write_text("4.24.0\n")
            (root / "active-version").chmod(0o600)
            link = root / "current"
            link.symlink_to(path)
            kernel = installer.os.uname().release

            def command(argv, **kwargs):
                if argv[0] == "modinfo":
                    output = "4.24.0" if argv[2] == "version" else kernel + " SMP"
                elif argv[0] == "git":
                    output = "a" * 40
                else:
                    output = ""
                return SimpleNamespace(stdout=output, returncode=0)

            with patch.object(installer, "CURRENT_LINK", link), \
                    patch.object(installer, "VERIFIED_MARKER", root / "verified.json"), \
                    patch.object(installer, "FAILURE_MARKER", root / "failure.json"), \
                    patch.object(installer, "run", side_effect=command) as run:
                self.assertFalse(installer.maintenance_status(root, "4.24.0")["candidate_ready"])
                installer.verify_installed(root, "4.24.0")
                with self.assertRaisesRegex(ValueError, "running kernel"):
                    installer.verify_candidate(root, "4.24.0")
                with self.assertRaisesRegex(ValueError, "running kernel"):
                    installer.select_candidate(root, "4.24.0")
                self.assertIn([str(link / "runtime/bin/hailortcli"),
                               "fw-control", "identify"],
                              [call.args[0] for call in run.call_args_list])
            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            (path / "runtime/lib/libhailort.so.4.24.0").write_bytes(b"corrupt")
            with patch.object(installer, "CURRENT_LINK", link), \
                    patch.object(installer, "run", side_effect=command), \
                    self.assertRaisesRegex(ValueError, "checksum mismatch"):
                installer.verify_installed(root, "4.24.0")

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
                "common",
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
                (installed / "hailort/drivers/common/source.c").is_file()
            )
            self.assertFalse((installed / "common/include").exists())
            self.assertIn(
                "PACKAGE_VERSION=4.24.0",
                (installed / "dkms.conf").read_text(),
            )

    def test_dkms_source_copy_failure_removes_only_its_new_source_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "driver"
            for relative in ("linux/pcie", "common", "linux/vdma", "linux/utils"):
                (driver / relative).mkdir(parents=True)
            (driver / "linux/pcie/dkms.conf.in").write_text(
                "PACKAGE_VERSION=@PCIE_DRIVER_VERSION@\n"
            )
            source_root = root / "usr-src"
            existing = source_root / "hailo_pci-4.23.0"
            existing.mkdir(parents=True)
            (existing / "dkms.conf").write_text("old source")
            with patch.object(
                installer.shutil, "copytree", side_effect=OSError("copy failed")
            ), self.assertRaisesRegex(OSError, "copy failed"):
                installer.copy_dkms_source(driver, "4.24.0", source_root)
            self.assertFalse((source_root / "hailo_pci-4.24.0").exists())
            self.assertEqual((existing / "dkms.conf").read_text(), "old source")

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


class HailoSafetyControlTests(unittest.TestCase):
    def test_disable_preserves_local_hat_unit_before_masking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "etc" / "rust-health-hat.service"
            vendor = root / "lib" / "rust-health-hat.service"
            local.parent.mkdir()
            local.write_text("[Service]\nExecStart=/usr/bin/true\n")
            commands = []
            with patch.object(installer, "HAT_LOCAL_UNIT", local), \
                    patch.object(installer, "HAT_VENDOR_UNIT", vendor), \
                    patch.object(installer, "run", side_effect=lambda command, **kwargs:
                                 commands.append(command) or
                                 subprocess.CompletedProcess(command, 0, "", "")):
                errors = []
                self.assertTrue(installer._stop_and_mask_hat(errors))
                self.assertEqual(errors, [])
                self.assertFalse(local.exists())
                self.assertIn("ExecStart=/usr/bin/true", vendor.read_text())
                self.assertIn(["systemctl", "mask", "rust-health-hat.service"], commands)
                self.assertTrue(installer._stop_and_mask_hat(errors))

    def control_paths(self, root):
        return {
            "DISABLED_MARKER": root / "state/disabled",
            "VERIFIED_MARKER": root / "state/verified.json",
            "FAILURE_MARKER": root / "state/last-failure.json",
            "MODPROBE_POLICY": root / "etc/modprobe.d/hailo-disabled.conf",
            "MODULES_LOAD": root / "etc/modules-load.d/hailo.conf",
        }

    def test_disable_persists_policy_and_reports_nonforced_unload_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.control_paths(root)
            paths["MODULES_LOAD"].parent.mkdir(parents=True)
            paths["MODULES_LOAD"].write_text(installer.MODULES_LOAD_POLICY)
            commands = []

            def command(argv, **kwargs):
                commands.append(argv)
                if argv == ["modprobe", "-r", installer.MODULE]:
                    raise subprocess.CalledProcessError(1, argv)
                return SimpleNamespace(returncode=0, stdout="")

            with patch.multiple(installer, **paths), \
                    patch.object(installer, "_stop_and_mask_hat", return_value=True), \
                    patch.object(installer, "module_loaded", return_value=True), \
                    patch.object(installer, "hailo_hardware_present", return_value=False), \
                    patch.object(installer, "run", side_effect=command):
                errors = []
                result = installer._disable_policy(errors, unload=True)

            self.assertTrue(result["disabled"])
            self.assertTrue(result["module_loaded"])
            self.assertIn("no forced unload", "; ".join(errors))
            self.assertEqual(paths["MODPROBE_POLICY"].read_text(),
                             installer.DISABLED_POLICY)
            self.assertEqual(paths["DISABLED_MARKER"].read_text(), "disabled\n")
            self.assertFalse(paths["MODULES_LOAD"].exists())
            self.assertIn(
                ["update-initramfs", "-u", "-k", "all"], commands
            )
            unload = next(argv for argv in commands if argv[:2] == ["modprobe", "-r"])
            self.assertEqual(unload, ["modprobe", "-r", "hailo_pci"])

    def test_disable_requires_observed_unload_even_when_modprobe_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.control_paths(root)
            with patch.multiple(installer, **paths), \
                    patch.object(installer.os, "geteuid",
                                 side_effect=[0] + [installer.os.getuid()] * 100), \
                    patch.object(installer, "_stop_and_mask_hat", return_value=True), \
                    patch.object(installer, "module_loaded", return_value=True), \
                    patch.object(installer, "hailo_hardware_present", return_value=False), \
                    patch.object(installer, "run", return_value=SimpleNamespace(
                        returncode=0, stdout="")) as run:
                code, status = installer.disable_hailo()
            self.assertEqual(code, 1)
            self.assertTrue(status["disabled"])
            self.assertTrue(status["module_loaded"])
            self.assertIn("unload postcondition", status["last_failure"])
            self.assertEqual(paths["MODPROBE_POLICY"].read_text(), installer.DISABLED_POLICY)
            self.assertTrue(paths["DISABLED_MARKER"].exists())
            self.assertIn(["modprobe", "-r", installer.MODULE],
                          [call.args[0] for call in run.call_args_list])

    def test_control_markers_reject_symlinks_and_lock_wait_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "disabled"
            target = root / "target"
            target.write_text("unchanged")
            marker.symlink_to(target)
            with patch.object(installer, "DISABLED_MARKER", marker):
                self.assertTrue(installer.safety_disabled())
            self.assertEqual(target.read_text(), "unchanged")
            with patch.object(installer.fcntl, "flock",
                              side_effect=BlockingIOError), \
                    patch.object(installer.time, "monotonic",
                                 side_effect=[0.0, 1.0]), \
                    patch.object(installer.time, "sleep"), \
                    self.assertRaisesRegex(TimeoutError, "artifact lock"):
                with installer.candidate_lock(root, timeout=0.5):
                    self.fail("A contended artifact lock must time out")

    def test_disable_is_idempotent_and_does_not_claim_loaded_module_unloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.control_paths(root)
            with patch.multiple(installer, **paths), \
                    patch.object(installer, "_stop_and_mask_hat", return_value=True), \
                    patch.object(installer, "module_loaded", return_value=False), \
                    patch.object(installer, "hailo_hardware_present", return_value=False), \
                    patch.object(installer, "run", return_value=SimpleNamespace(
                        returncode=0, stdout="")) as command:
                first = installer._disable_policy([], unload=True)
                second = installer._disable_policy([], unload=True)
            self.assertEqual(first["state"], "disabled")
            self.assertEqual(second["state"], "disabled")
            self.assertFalse(second["module_loaded"])
            self.assertEqual(
                sum(call.args[0] == ["update-initramfs", "-u", "-k", "all"]
                    for call in command.call_args_list),
                2,
            )
            self.assertNotIn(
                ["modprobe", "-r", installer.MODULE],
                [call.args[0] for call in command.call_args_list],
            )

    def test_control_status_distinguishes_disabled_pending_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.control_paths(root)
            kernel = installer.os.uname().release
            service = {"active": "inactive", "unit_file_state": "masked"}
            with patch.multiple(installer, **paths), \
                    patch.object(installer, "_service_state",
                                 return_value=service), \
                    patch.object(installer, "module_loaded", return_value=True), \
                    patch.object(installer, "hailo_hardware_present",
                                 return_value=False), \
                    patch.object(installer, "run") as command:
                paths["DISABLED_MARKER"].parent.mkdir(parents=True)
                paths["DISABLED_MARKER"].write_text("disabled\n")
                paths["DISABLED_MARKER"].chmod(0o600)
                paths["MODPROBE_POLICY"].parent.mkdir(parents=True)
                paths["MODPROBE_POLICY"].write_text(installer.DISABLED_POLICY)
                disabled = installer.control_status(root)
                self.assertEqual(disabled["state"], "disabled")
                self.assertTrue(disabled["module_loaded"])
                self.assertFalse(disabled["hardware_present"])
                self.assertIn("remains loaded", disabled["warning"])
                command.assert_called_once_with(
                    ["modinfo", "-F", "version", installer.MODULE],
                    check=False, timeout=30,
                )

                paths["DISABLED_MARKER"].unlink()
                paths["MODPROBE_POLICY"].unlink()
                with patch.object(installer, "module_loaded", return_value=False), \
                        patch.object(installer, "run", side_effect=[
                            SimpleNamespace(returncode=0, stdout="4.23.0\n"),
                            SimpleNamespace(returncode=0, stdout=kernel + " SMP\n"),
                        ]) as command:
                    pending = installer.control_status(root)
                self.assertEqual(pending["state"], "installed-but-unverified")
                self.assertFalse(pending["hardware_present"])
                self.assertFalse(any(
                    call.args[0][0] in ("modprobe", "hailortcli")
                    for call in command.call_args_list
                ))

                paths["VERIFIED_MARKER"].parent.mkdir(parents=True, exist_ok=True)
                paths["VERIFIED_MARKER"].write_text(json.dumps({
                    "driver_version": "4.23.0",
                    "kernel_release": kernel,
                    "verified_at": 123.0,
                }))
                paths["VERIFIED_MARKER"].chmod(0o600)
                with patch.object(installer, "module_loaded", return_value=True), \
                        patch.object(installer, "hailo_hardware_present",
                                     return_value=True), \
                        patch.object(installer, "run", side_effect=[
                            SimpleNamespace(returncode=0, stdout="4.23.0\n"),
                            SimpleNamespace(returncode=0, stdout=kernel + " SMP\n"),
                            SimpleNamespace(returncode=0, stdout="4.23.0\n"),
                            SimpleNamespace(returncode=0, stdout=kernel + " SMP\n"),
                        ]) as command:
                    verified = installer.control_status(root)
                self.assertEqual(verified["state"], "verified")
                self.assertEqual(verified["verified_at"], 123.0)
                self.assertFalse(any(
                    call.args[0][0] in ("modprobe", "hailortcli")
                    for call in command.call_args_list
                ))

    def test_apt_managed_driver_can_be_enabled_without_source_runtime_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.control_paths(root)
            kernel = installer.os.uname().release

            def command(argv, **kwargs):
                if argv == ["modprobe", "--ignore-install", installer.MODULE]:
                    self.assertEqual(paths["MODPROBE_POLICY"].read_text(),
                                     installer.DISABLED_POLICY)
                    self.assertTrue(paths["DISABLED_MARKER"].exists())
                if argv[:3] == ["modinfo", "-F", "version"]:
                    output = "4.23.0\n"
                elif argv[:3] == ["modinfo", "-F", "vermagic"]:
                    output = kernel + " SMP\n"
                else:
                    output = ""
                return SimpleNamespace(returncode=0, stdout=output)

            with patch.multiple(installer, **paths), \
                    patch.object(installer.os, "geteuid",
                                 side_effect=[0] + [installer.os.getuid()] * 100), \
                    patch.object(installer, "_stop_and_mask_hat", return_value=True), \
                    patch.object(installer, "hailo_hardware_present", return_value=True), \
                    patch.object(installer, "module_loaded", return_value=False), \
                    patch.object(installer, "run", side_effect=command) as run:
                code, result = installer.enable_and_verify(root)

            self.assertEqual(code, 0, result)
            self.assertEqual(result["state"], "verified")
            self.assertFalse(result["disabled"])
            self.assertFalse(result["inference_active"])
            self.assertTrue(result["inference_masked"])
            self.assertEqual(
                paths["MODULES_LOAD"].read_text(),
                installer.MODULES_LOAD_POLICY,
            )
            self.assertFalse(paths["MODPROBE_POLICY"].exists())
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(["modprobe", "--ignore-install", installer.MODULE], commands)
            self.assertIn(
                ["hailortcli", "fw-control", "identify"], commands
            )
            self.assertIn(
                ["update-initramfs", "-u", "-k", "all"], commands
            )

    def test_enable_failure_restores_disabled_policy_without_rust_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.control_paths(root)
            with patch.multiple(installer, **paths), \
                    patch.object(installer.os, "geteuid", return_value=0), \
                    patch.object(installer, "_stop_and_mask_hat", return_value=True), \
                    patch.object(installer, "hailo_hardware_present", return_value=False), \
                    patch.object(installer, "module_loaded", return_value=False), \
                    patch.object(installer, "run", return_value=SimpleNamespace(
                        returncode=0, stdout="")) as run:
                code, result = installer.enable_and_verify(root)
            self.assertEqual(code, 1)
            self.assertTrue(result["disabled"])
            self.assertIn("No supported Hailo-8", result["last_failure"])
            self.assertEqual(paths["MODPROBE_POLICY"].read_text(),
                             installer.DISABLED_POLICY)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertNotIn(["systemctl", "restart", "rust.service"], commands)
            self.assertNotIn(["systemctl", "start", "rust.service"], commands)

    def test_enable_identify_failure_keeps_block_at_load_and_restores_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.control_paths(root)
            def command(argv, **kwargs):
                if argv == ["modprobe", "--ignore-install", installer.MODULE]:
                    self.assertEqual(paths["MODPROBE_POLICY"].read_text(),
                                     installer.DISABLED_POLICY)
                    self.assertTrue(paths["DISABLED_MARKER"].exists())
                if "identify" in argv:
                    raise subprocess.CalledProcessError(1, argv)
                return SimpleNamespace(returncode=0, stdout="")
            with patch.multiple(installer, **paths), \
                    patch.object(installer.os, "geteuid",
                                 side_effect=[0] + [installer.os.getuid()] * 100), \
                    patch.object(installer, "_stop_and_mask_hat", return_value=True), \
                    patch.object(installer, "hailo_hardware_present", return_value=True), \
                    patch.object(installer, "module_loaded", return_value=False), \
                    patch.object(installer, "validate_installed",
                                 return_value=({"installed": "4.23.0",
                                                "kernel_release": "test"}, "hailortcli", None)), \
                    patch.object(installer, "run", side_effect=command) as run:
                def validated_command(argv, **kwargs):
                    if argv[:3] == ["modinfo", "-F", "version"]:
                        return SimpleNamespace(returncode=0, stdout="4.23.0")
                    return command(argv, **kwargs)
                run.side_effect = validated_command
                code, result = installer.enable_and_verify(root)
            self.assertEqual(code, 1)
            self.assertTrue(result["disabled"])
            self.assertIn("hailortcli", result["last_failure"])
            self.assertEqual(paths["MODPROBE_POLICY"].read_text(), installer.DISABLED_POLICY)
            self.assertFalse(paths["VERIFIED_MARKER"].exists())

    def test_non_hat_without_driver_is_not_installed_but_broken_package_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.control_paths(root)
            with patch.multiple(installer, **paths), \
                    patch.object(installer, "_service_state",
                                 return_value={"active": "inactive", "unit_file_state": "disabled"}), \
                    patch.object(installer, "hailo_hardware_present", return_value=False), \
                    patch.object(installer, "module_loaded", return_value=False), \
                    patch.object(installer, "current_target", return_value=None), \
                    patch.object(installer, "DKMS_SOURCE_ROOT", root / "src"), \
                    patch.object(installer, "run", side_effect=[
                        SimpleNamespace(returncode=1, stdout=""),
                        SimpleNamespace(returncode=1, stdout=""),
                        SimpleNamespace(returncode=1, stdout=""),
                        SimpleNamespace(returncode=0, stdout="installed"),
                    ]):
                self.assertEqual(installer.control_status(root)["state"], "not-installed")
                self.assertEqual(installer.control_status(root)["state"], "failure")

    def test_present_hat_without_installed_software_can_be_provisioned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(installer, **self.control_paths(root)), \
                    patch.object(installer, "_service_state",
                                 return_value={"active": "inactive", "unit_file_state": "disabled"}), \
                    patch.object(installer, "hailo_hardware_present", return_value=True), \
                    patch.object(installer, "module_loaded", return_value=False), \
                    patch.object(installer, "current_target", return_value=None), \
                    patch.object(installer, "DKMS_SOURCE_ROOT", root / "src"), \
                    patch.object(installer, "run", side_effect=[
                        SimpleNamespace(returncode=1, stdout=""),
                        SimpleNamespace(returncode=1, stdout=""),
                    ]) as run:
                status = installer.control_status(root)
            self.assertEqual(status["state"], "not-installed")
            self.assertTrue(status["hardware_present"])
            self.assertFalse(status["disabled"])
            self.assertEqual([call.args[0][0] for call in run.call_args_list],
                             ["modinfo", "dpkg-query"])

    def test_disabled_status_reports_pending_versions_or_absence_without_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.control_paths(root)
            paths["DISABLED_MARKER"].parent.mkdir(parents=True)
            paths["DISABLED_MARKER"].write_text("disabled\n")
            paths["DISABLED_MARKER"].chmod(0o600)
            paths["MODPROBE_POLICY"].parent.mkdir(parents=True)
            paths["MODPROBE_POLICY"].write_text(installer.DISABLED_POLICY)
            with patch.multiple(installer, **paths), \
                    patch.object(installer, "_service_state",
                                 return_value={"active": "inactive", "unit_file_state": "masked"}), \
                    patch.object(installer, "module_loaded", return_value=False), \
                    patch.object(installer, "hailo_hardware_present", return_value=True), \
                    patch.object(installer, "run", side_effect=[
                        SimpleNamespace(returncode=0, stdout="4.23.0"),
                        SimpleNamespace(returncode=0, stdout="4.24.0"),
                        SimpleNamespace(returncode=1, stdout=""),
                        SimpleNamespace(returncode=1, stdout=""),
                    ]) as run:
                apt = installer.control_status(root)
                installer.write_active_version(root, "4.24.0")
                source = installer.control_status(root)
                source_without_module = installer.control_status(root)
                installer.write_active_version(root, None)
                absent = installer.control_status(root)
            for status, version in ((apt, "4.23.0"), (source, "4.24.0")):
                self.assertEqual(status["state"], "disabled")
                self.assertTrue(status["disabled"])
                self.assertEqual(status["driver_version"], version)
                self.assertEqual(status["installed_version"], version)
                self.assertTrue(status["verification_pending"])
                self.assertEqual(status["hardware_verification"], "pending")
            self.assertEqual(source["source_managed_version"], "4.24.0")
            self.assertEqual(source_without_module["state"], "disabled")
            self.assertIsNone(source_without_module["driver_version"])
            self.assertEqual(source_without_module["installed_version"], "4.24.0")
            self.assertTrue(source_without_module["verification_pending"])
            self.assertIsNone(apt["source_managed_version"])
            self.assertEqual(absent["state"], "disabled")
            self.assertTrue(absent["disabled"])
            self.assertIsNone(absent["driver_version"])
            self.assertIsNone(absent["installed_version"])
            self.assertFalse(absent["verification_pending"])
            self.assertEqual(absent["hardware_verification"], "not-verified")
            self.assertEqual([call.args[0][0] for call in run.call_args_list],
                             ["modinfo"] * 4)

    def test_control_status_cli_accepts_no_version(self):
        with patch.object(installer.sys, "argv", ["hailo_install.py", "--control", "status"]), \
                patch.object(installer, "candidate_lock"), \
                patch.object(installer, "perform_operation", return_value=0) as operation:
            self.assertEqual(installer.main(), 0)
            self.assertEqual(operation.call_args.args[0].version, "")

    def test_service_inspection_failure_is_not_a_quiescent_state(self):
        with patch.object(installer, "run", return_value=SimpleNamespace(
                returncode=1, stdout="")):
            with self.assertRaisesRegex(RuntimeError, "Could not inspect"):
                installer._service_state()

    def test_disabled_install_rejects_unknown_service_state_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.control_paths(root)
            paths["DISABLED_MARKER"].parent.mkdir(parents=True)
            paths["DISABLED_MARKER"].write_text("disabled\n")
            paths["DISABLED_MARKER"].chmod(0o600)
            paths["MODPROBE_POLICY"].parent.mkdir(parents=True)
            paths["MODPROBE_POLICY"].write_text(installer.DISABLED_POLICY)
            with patch.multiple(installer, **paths), \
                    patch.object(installer.os, "geteuid",
                                 side_effect=[0] + [installer.os.getuid()] * 100), \
                    patch.object(installer, "safety_disabled", return_value=True), \
                    patch.object(installer, "module_loaded", return_value=False), \
                    patch.object(installer, "_service_state",
                                 return_value={"active": "unknown", "unit_file_state": "masked"}), \
                    patch.object(installer, "activate") as activate:
                with self.assertRaisesRegex(RuntimeError, "stopped and masked"):
                    installer.install_while_disabled(root, "4.24.0")
                activate.assert_not_called()

    def test_enable_rejects_still_loaded_module_before_loading_or_identification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(installer, **self.control_paths(root)), \
                    patch.object(installer.os, "geteuid", return_value=0), \
                    patch.object(installer, "_stop_and_mask_hat", return_value=True), \
                    patch.object(installer, "module_loaded", return_value=True), \
                    patch.object(installer, "hailo_hardware_present", return_value=True), \
                    patch.object(installer, "validate_installed") as validate, \
                    patch.object(installer, "run", return_value=SimpleNamespace(
                        returncode=0, stdout="")) as run:
                code, result = installer.enable_and_verify(root)
                self.assertEqual(code, 1)
                self.assertIn("still loaded", result["last_failure"])
                validate.assert_not_called()
                self.assertNotIn(["modprobe", "--ignore-install", installer.MODULE],
                                 [call.args[0] for call in run.call_args_list])

    def test_disabled_runtime_validation_never_loads_or_identifies_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate_path = Path(directory)
            with patch.object(installer, "run", return_value=SimpleNamespace(
                    returncode=0, stdout="")) as command:
                installer.validate_runtime(
                    candidate_path, {}, verify_hardware=False
                )
            self.assertEqual(
                [call.args[0] for call in command.call_args_list],
                [[str(candidate_path / "venv/bin/python"), "-c",
                  "import numpy, hailo_platform"]],
            )

    def test_ordinary_install_cannot_clear_a_persistent_safety_latch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(installer, "ROOT_DEFAULT", root), \
                    patch.object(installer.os, "geteuid", return_value=0), \
                    patch.object(installer, "safety_disabled", return_value=True), \
                    patch.object(installer, "verify_candidate") as verify:
                with self.assertRaisesRegex(RuntimeError, "safety latch is active"):
                    installer.activate(root, "4.24.0")
            verify.assert_not_called()

    def test_disabled_install_rollback_never_unblocks_or_identifies_hardware(self):
        for registered in (True, False):
            with self.subTest(registered=registered):
                self._assert_disabled_install_rollback(registered)

    def _assert_disabled_install_rollback(self, registered):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate_path = root / "candidates/4.24.0"
            firmware = candidate_path / "firmware/hailo8_fw.bin"
            firmware.parent.mkdir(parents=True)
            firmware.write_bytes(b"new firmware")
            old_firmware = root / "installed/hailo8_fw.bin"
            old_firmware.parent.mkdir()
            old_firmware.write_bytes(b"old firmware")
            dkms_source = root / "usr/src/hailo_pci-4.23.0"
            dkms_source.mkdir(parents=True)
            (dkms_source / "dkms.conf").write_text("old source")
            disabled_marker = root / "state/disabled"
            disabled_marker.parent.mkdir()
            disabled_marker.write_text("disabled\n")
            policy = root / "etc/modprobe.d/disabled.conf"
            policy.parent.mkdir(parents=True)
            policy.write_text(installer.DISABLED_POLICY)
            link = root / "opt/rustberrypi-hailo/current"
            link.parent.mkdir(parents=True)
            ldconf = root / "etc/ld.so.conf.d/hailo.conf"
            hat_dropin = root / "etc/systemd/system/rust-health-hat.service.d/runtime.conf"
            commands = []
            dkms_status_calls = 0

            def command(argv, **kwargs):
                nonlocal dkms_status_calls
                commands.append(argv)
                if argv[:3] == ["modinfo", "-F", "version"]:
                    output = "4.23.0\n"
                elif argv[:2] == ["apt-mark", "showhold"]:
                    output = ""
                elif argv[:2] == ["dkms", "status"]:
                    dkms_status_calls += 1
                    output = (
                        "hailo_pci/4.24.0"
                        if dkms_status_calls > 1 and registered
                        else ""
                    )
                else:
                    output = ""
                return SimpleNamespace(returncode=0, stdout=output)

            is_file = Path.is_file

            def is_file_with_headers(path):
                return path.name == "Makefile" or is_file(path)

            with patch.multiple(
                installer,
                ROOT_DEFAULT=root,
                DISABLED_MARKER=disabled_marker,
                VERIFIED_MARKER=root / "state/verified.json",
                FAILURE_MARKER=root / "state/failure.json",
                MODPROBE_POLICY=policy,
                MODULES_LOAD=root / "etc/modules-load.d/hailo.conf",
                FIRMWARE_PATH=old_firmware,
                CURRENT_LINK=link,
                LDCONF=ldconf,
                HAT_DROPIN=hat_dropin,
                DKMS_SOURCE_ROOT=root / "usr/src",
            ), patch.object(installer.os, "geteuid", return_value=0), \
                    patch.object(installer, "verify_candidate", return_value=(
                        candidate_path,
                        {"architecture": "aarch64",
                         "kernel_release": installer.os.uname().release},
                    )), \
                    patch.object(installer, "current_target", return_value=None), \
                    patch.object(installer, "module_loaded", return_value=False), \
                    patch.object(installer, "copy_dkms_source", side_effect=lambda *_: (
                        root / "usr/src/hailo_pci-4.24.0"
                    ).mkdir()), \
                    patch.object(installer, "atomic_link"), \
                    patch.object(installer, "validate_runtime",
                                 side_effect=subprocess.CalledProcessError(
                                     1, ["python", "-c", "import hailo_platform"]
                                 )), \
                    patch.object(installer.Path, "is_file",
                                 is_file_with_headers), \
                    patch.object(installer, "run", side_effect=command), \
                    patch.object(installer.sys, "stdout", new_callable=io.StringIO), \
                    patch.object(installer.sys, "stderr", new_callable=io.StringIO):
                result = installer.activate(root, "4.24.0", disabled_install=True)

            self.assertEqual(result, 10)
            self.assertEqual(old_firmware.read_bytes(), b"old firmware")
            self.assertEqual(disabled_marker.read_text(), "disabled\n")
            self.assertEqual(policy.read_text(), installer.DISABLED_POLICY)
            self.assertFalse((root / "active-version").exists())
            self.assertFalse((root / "usr/src/hailo_pci-4.24.0").exists())
            self.assertFalse(any(
                command[:2] == ["modprobe", installer.MODULE]
                or command[:3] == ["hailortcli", "fw-control", "identify"]
                for command in commands
            ))
            self.assertFalse(any(
                command[:2] == ["modprobe", "-r"] for command in commands
            ))


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
            activation["when"], "rust_maintenance_target == 'hailo' or rust_maintenance_all_hailo | bool"
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
        rollback_failure = next(task for task in tasks if
                                task["name"].startswith("Report Hailo rollback as unsuccessful"))
        self.assertIn("ansible.builtin.fail", rollback_failure)
        self.assertGreater(tasks.index(rollback_failure), tasks.index(workflow))

    def test_all_hardware_gate_ignores_inference_and_checks_approval_upfront(self):
        tasks = self.tasks("roles/rust_maintenance/tasks/main.yml")
        gate = next(task["ansible.builtin.set_fact"]["rust_maintenance_all_hailo"]
                    for task in tasks if task["name"].startswith("Remember whether All"))
        approval = next(task for task in tasks
                        if task["name"] == "Require Hailo approval and valid version before downtime")
        env = Environment(undefined=StrictUndefined)
        env.filters["bool"] = bool
        env.filters["from_json"] = json.loads
        env.tests["match"] = lambda text, pattern: __import__("re").match(pattern, text) is not None
        for target in ("all", "hailo", "os", "os-rust", "fex", "rust"):
            for hardware in (False, True):
                for inference in (False, True):
                    for disabled in (False, True):
                        context = dict(
                            rust_maintenance_target=target,
                            rust_telemetry_hat_enabled=inference,
                            rust_maintenance_hailo_version="",
                            rust_maintenance_hailo_firmware_eula_accepted=False,
                            rust_maintenance_hailo_disabled=disabled)
                        if target == "all":
                            context["rust_maintenance_hardware"] = {
                                "stdout": json.dumps({"hailo": [{"address": "0001:01:00.0"}]
                                                      if hardware else []})}
                        with self.subTest(target=target, hardware=hardware,
                                          inference=inference, disabled=disabled):
                            included = target == "all" and hardware and not disabled
                            self.assertEqual(
                                env.from_string(gate).render(**context) == "True",
                                included)
                            context["rust_maintenance_all_hailo"] = included
                            applies = env.compile_expression(approval["when"])(**context)
                            self.assertEqual(applies, target == "hailo" or included)
                            if applies:
                                self.assertFalse(env.compile_expression(
                                    approval["ansible.builtin.assert"]["that"][0])(**context))
        preflight = next(i for i, task in enumerate(tasks)
                         if task["name"].startswith("Validate and pin common"))
        downtime = next(i for i, task in enumerate(tasks)
                        if task["name"] == "Acquire exclusive maintenance window")
        self.assertLess(preflight, downtime)
        self.assertLess(tasks.index(approval), preflight)
        disabled_skip = next(
            task for task in tasks
            if task["name"] == "Explicitly skip Hailo for All while safety-disabled"
        )
        self.assertIn("persistent safety latch",
                      disabled_skip["ansible.builtin.debug"]["msg"])
        self.assertIn("rust_telemetry_hat_enabled",
                      disabled_skip["ansible.builtin.debug"]["msg"])
        detector = next(task for task in tasks if
                        task["name"] == "Detect Hailo PCI hardware without provisioning or enabling inference")
        self.assertFalse(detector["changed_when"])
        self.assertEqual(detector["when"], "rust_maintenance_target == 'all'")
        workflow = next(task["block"] for task in tasks if "block" in task)
        names = [task["name"] for task in workflow]
        self.assertLess(names.index("Run OS job"), names.index("Activate approved Hailo source candidate"))
        self.assertLess(names.index("Wait for existing Hailo candidate work before announcing downtime"),
                        names.index("Announce countdown and request world save"))
        for name, targets in (
            ("Run OS job", ("os", "os-rust", "all")),
            ("Run pinned FEX job", ("fex", "all")),
            ("Run Rust job", ("rust", "os-rust", "all")),
        ):
            task = next(task for task in workflow if task["name"] == name)
            for target in ("all", "hailo", "os", "os-rust", "fex", "rust"):
                self.assertEqual(env.compile_expression(task["when"])(
                    rust_maintenance_target=target), target in targets)

    def test_all_refreshes_facts_and_reuses_actual_kernel_builder(self):
        os_tasks = self.tasks("roles/rust_maintenance/tasks/os.yml")
        refresh = next(task for task in os_tasks if "ansible.builtin.setup" in task)
        self.assertEqual(refresh["when"], "rust_maintenance_all_hailo | bool")
        self.assertGreater(os_tasks.index(refresh), next(
            i for i, task in enumerate(os_tasks) if "ansible.builtin.reboot" in task))
        hailo = self.tasks("roles/rust_maintenance/tasks/hailo.yml")
        self.assertEqual(hailo[0]["ansible.builtin.include_role"]["name"], "hailo_candidate")
        self.assertTrue(hailo[0]["vars"]["hailo_candidate_maintenance"])
        builder = self.tasks("roles/hailo_candidate/tasks/main.yml")
        kernel = next(task for task in builder if task["name"].startswith("Read exact running"))
        self.assertEqual(kernel["ansible.builtin.command"], "uname -r")
        status = next(task for task in builder if task["name"].startswith("Validate candidate readiness"))
        self.assertIn("--maintenance-status", status["ansible.builtin.command"]["argv"])
        publication = next(task for task in builder if task["name"].startswith("Publish validated"))
        self.assertEqual(publication["when"], "not hailo_candidate_maintenance | bool")

    def test_existing_provisioning_respects_source_managed_runtime(self):
        tasks = self.tasks("roles/pi_hardware/tasks/hailo-provision.yml")
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
        text = (ROOT / "roles/pi_hardware/tasks/hailo-provision.yml").read_text()
        self.assertIn("/opt/rustberrypi-hailo/current/venv/bin/python", text)
        self.assertIn(
            "/opt/rustberrypi-hailo/current/runtime/bin/hailortcli", text
        )

    def test_safety_latch_skips_all_hailo_provisioning(self):
        guard = self.tasks("roles/pi_hardware/tasks/hailo.yml")
        include = next(
            task for task in guard
            if task["name"] == "Provision Hailo when not safety-disabled"
        )
        self.assertEqual(
            include["ansible.builtin.include_tasks"], "hailo-provision.yml"
        )
        self.assertIn("pi_hardware_hailo_disabled.stat.exists", include["when"])
        self.assertIn("pi_hardware_hailo_policy.stat.exists", include["when"])
        telemetry_guard = self.tasks("roles/rust_telemetry/tasks/hat.yml")
        self.assertIn("masked", str(telemetry_guard))
        telemetry_include = next(
            task for task in telemetry_guard
            if task["name"] == "Configure HAT inference only while Hailo is enabled"
        )
        self.assertIn("rust_telemetry_hailo_disabled.stat.exists",
                      telemetry_include["when"])
        self.assertIn("rust_telemetry_hailo_policy.stat.exists",
                      telemetry_include["when"])

    def test_ordinary_hailo_maintenance_refuses_disabled_state(self):
        tasks = self.tasks("roles/rust_maintenance/tasks/main.yml")
        status = next(task for task in tasks
                      if task["name"] == "Read persistent Hailo safety state before maintenance")
        self.assertEqual(status["ansible.builtin.command"]["argv"][-2:],
                         ["--control", "status"])
        guard = next(task for task in tasks
                     if task["name"] == "Refuse ordinary Hailo activation while safety-disabled")
        self.assertEqual(guard["when"], "rust_maintenance_target == 'hailo'")
        self.assertIn("not rust_maintenance_hailo_disabled",
                      guard["ansible.builtin.assert"]["that"])

    def test_control_playbook_isolated_from_rust_lifecycle(self):
        play = self.tasks("playbooks/hailo-control.yml")[0]
        self.assertEqual(play["hosts"], "rust_servers")
        self.assertTrue(play["become"])
        self.assertEqual(play["serial"], 1)
        self.assertNotIn("roles", play)
        text = (ROOT / "playbooks/hailo-control.yml").read_text()
        self.assertNotIn("rust.service", text)
        survey = json.loads((ROOT / "awx/hailo-control-survey.json").read_text())
        action = next(
            item for item in survey["spec"]
            if item["variable"] == "rust_hailo_control_action"
        )
        self.assertEqual(
            action["choices"],
            ["status", "disable", "enable_verify", "install_while_disabled"],
        )
        self.assertEqual(action["default"], "status")


if __name__ == "__main__":
    unittest.main()
