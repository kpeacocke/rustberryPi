"""Test numeric snapshots, standalone sources and safe build outputs."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from test_server_health import sample
from tools.export_health_bundle import export_bundle

ROOT = Path(__file__).resolve().parents[1]


class BundleTests(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("numpy") and importlib.util.find_spec("onnx"),
        "Optional model dependencies required",
    )
    def test_exported_source_trains_real_snapshot_without_repository_access(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            features = root / "features"
            features.mkdir()
            with (features / "features-2026-10-01.jsonl").open("w") as stream:
                for index in range(1400):
                    stream.write(
                        json.dumps(
                            sample(1000 + index * 5, entities=5000 + index)
                        )
                        + "\n"
                    )
            export_bundle(features, root / "kit.zip")
            with zipfile.ZipFile(root / "kit.zip") as archive:
                archive.extractall(root / "kit")
            kit = root / "kit"
            result = subprocess.run(
                [
                    __import__("sys").executable,
                    str(kit / "tools/train_server_health.py"),
                    "train",
                    "--features",
                    str(kit / "features"),
                    "--output",
                    str(root / "model"),
                    "--architecture",
                    "hailo8",
                ],
                capture_output=True,
                text=True,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((root / "model/manifest.json").read_text())
            self.assertEqual(manifest["input_range"], [[2, 2], [5000, 6399]])
            self.assertIsNone(manifest["hef_sha256"])

    def test_numeric_snapshot_is_standalone_private_and_checksum_verified(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            features = root / "features"
            features.mkdir()
            path = features / "features-2026-10-01.jsonl"
            path.write_text(
                json.dumps(
                    sample(1000, player_name="PRIVATE", password="SECRET")
                )
                + "\n"
                + '{"partial":'
            )
            output = root / "kit.zip"
            self.assertEqual(export_bundle(features, output), 1)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                export_bundle(features, output)
            with zipfile.ZipFile(output) as archive:
                content = archive.read("features/" + path.name)
                self.assertNotIn(b"PRIVATE", content)
                self.assertNotIn(b"SECRET", content)
                self.assertEqual(json.loads(content)["fps"], 60)
                archive.extractall(root / "extracted")
            extracted = root / "extracted"
            spec = importlib.util.spec_from_file_location(
                "bundle_build", extracted / "build.py"
            )
            build = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(build)
            build.verify_snapshot()
            result = subprocess.run(
                [
                    __import__("sys").executable,
                    str(extracted / "tools/train_server_health.py"),
                    "--help",
                ],
                capture_output=True,
                text=True,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            (extracted / "features" / path.name).write_text("tampered")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                build.verify_snapshot()

    def test_invalid_archive_removes_partial_zip_and_symlinks_are_rejected(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "features-2026-10-01.jsonl"
            output = root / "kit.zip"
            for content in (
                "broken\n",
                json.dumps(sample(1000, fps=float("nan"))) + "\n",
            ):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    export_bundle(root, output)
                self.assertFalse(output.exists())
            path.unlink()
            path.symlink_to(ROOT / "README.md")
            with self.assertRaises(ValueError):
                export_bundle(root, output)
            self.assertFalse(output.exists())

    def test_build_checks_and_packages_candidate_without_deployment(self):
        spec = importlib.util.spec_from_file_location(
            "bundle_build", ROOT / "tools/health_bundle/build.py"
        )
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "bundle.json").write_text('{"architecture":"hailo8"}')
            commands = []

            def run(command, log=None):
                commands.append(command)
                if "compile" in command:
                    output = next((root / "output").iterdir())
                    model = output / "model"
                    model.mkdir()
                    (model / "server-health.hef").write_bytes(b"fixture")
                    (model / "manifest.json").write_text(
                        json.dumps(
                            {
                                "hef_sha256": build.hashlib.sha256(
                                    b"fixture"
                                ).hexdigest(),
                                "input_range": [[0, 0], [100, 200]],
                                "validation_rmse": {},
                                "median_baseline_rmse": {},
                            }
                        )
                    )

            original_read = Path.read_text

            def read(path, *args, **kwargs):
                return (
                    "avx avx2"
                    if str(path) == "/proc/cpuinfo"
                    else original_read(path, *args, **kwargs)
                )

            with patch.object(build, "ROOT", root), patch.object(
                build, "verify_snapshot"
            ), patch.object(build, "run", side_effect=run), patch.object(
                build.platform, "system", return_value="Linux"
            ), patch.object(
                build.platform, "machine", return_value="x86_64"
            ), patch.object(
                build.shutil, "which", return_value="/usr/bin/docker"
            ), patch.object(
                Path, "read_text", read
            ), patch(
                "sys.argv", ["build.py"]
            ):
                build.main()
            candidate = next((root / "output").glob("*/candidate.zip"))
            with zipfile.ZipFile(candidate) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"server-health.hef", "manifest.json"},
                )
            self.assertTrue(any("train" in command for command in commands))
            self.assertEqual(
                commands[-1][-4:],
                [
                    "chown",
                    "-R",
                    f"{build.os.getuid()}:{build.os.getgid()}",
                    "/work",
                ],
            )
            self.assertFalse(
                any(
                    "ssh" in command or "--privileged" in command
                    for command in commands
                )
            )
