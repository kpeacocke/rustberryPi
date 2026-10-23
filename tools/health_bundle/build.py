#!/usr/bin/env python3
"""Check or build the isolated compiler environment, then train and compile."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent


def run(command, log=None):
    print("+ " + " ".join(map(str, command)), flush=True)
    if log is None:
        return subprocess.run(command, check=True)
    with log.open("a") as stream:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in process.stdout:
            print(line, end="", flush=True)
            stream.write(line)
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, command)


def verify_snapshot():
    for name, expected in json.loads(
        (ROOT / "SHA256.json").read_text()
    ).items():
        member = ROOT / name
        path = member.resolve()
        if not path.is_relative_to(ROOT) or member.is_symlink():
            raise ValueError("Invalid bundle member: " + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Bundle checksum mismatch: " + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="hailo-dfc:3.34.0-fixed")
    parser.add_argument(
        "--wheel",
        type=Path,
        help="Licensed DFC 3.34.0 wheel; builds a local image",
    )
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in (
        "x86_64",
        "AMD64",
    ):
        parser.error(
            "Run inside x86-64 WSL2 or Linux, "
            "not Windows Python, macOS or the Pi"
        )
    if not shutil.which("docker"):
        parser.error(
            "Docker CLI is missing; install/start Docker with WSL integration"
        )
    flags = Path("/proc/cpuinfo").read_text().split()
    if "avx" not in flags:
        parser.error("AVX is not exposed to this Linux environment")
    verify_snapshot()
    run(["docker", "info"], ROOT / "environment.log")
    if args.wheel:
        if (
            args.wheel.name
            != "hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl"
        ):
            parser.error("Expected the licensed DFC 3.34.0 x86-64 wheel")
        with tempfile.TemporaryDirectory(prefix="hailo-image-") as folder:
            context = Path(folder)
            shutil.copyfile(args.wheel, context / "compiler.whl")
            shutil.copyfile(ROOT / "Dockerfile", context / "Dockerfile")
            run(
                [
                    "docker",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "-t",
                    args.image,
                    folder,
                ],
                ROOT / "environment.log",
            )
    run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            args.image,
            "python",
            "-c",
            "import sys,numpy,onnx; "
            "from hailo_sdk_client import ClientRunner; "
            "assert sys.version_info[:2]==(3,10); "
            'print("Compiler environment ready")',
        ],
        ROOT / "environment.log",
    )
    if args.check_only:
        return
    output = (
        ROOT
        / "output"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    )
    output.mkdir(parents=True)
    model = output / "model"
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--platform",
        "linux/amd64",
        "--env",
        "HOME=/tmp",
        "--mount",
        f"type=bind,source={ROOT},target=/bundle,readonly",
        "--mount",
        f"type=bind,source={output},target=/work",
        "--workdir",
        "/work",
        args.image,
        "python",
        "/bundle/tools/train_server_health.py",
    ]
    architecture = json.loads((ROOT / "bundle.json").read_text())[
        "architecture"
    ]
    try:
        run(
            command
            + [
                "train",
                "--features",
                "/bundle/features",
                "--output",
                "/work/model",
                "--architecture",
                architecture,
            ],
            output / "build.log",
        )
        run(
            command + ["compile", "--model-dir", "/work/model"],
            output / "build.log",
        )
        manifest = json.loads((model / "manifest.json").read_text())
        if (
            hashlib.sha256(
                (model / "server-health.hef").read_bytes()
            ).hexdigest()
            != manifest["hef_sha256"]
        ):
            raise ValueError("Compiled HEF checksum mismatch")
        with zipfile.ZipFile(
            output / "candidate.zip", "x", zipfile.ZIP_DEFLATED
        ) as archive:
            for name in ("server-health.hef", "manifest.json"):
                archive.write(model / name, name)
        print(
            json.dumps(
                {
                    "input_range": manifest["input_range"],
                    "validation_rmse": manifest["validation_rmse"],
                    "median_baseline_rmse": manifest["median_baseline_rmse"],
                },
                indent=2,
            )
        )
        print(f'Candidate: {output / "candidate.zip"}')
        print(
            "NOT certified for deployment: compare holdout errors "
            "and test real Pi parity across the input range."
        )
    finally:
        run(
            [
                "docker",
                "run",
                "--rm",
                "--mount",
                f"type=bind,source={output},target=/work",
                args.image,
                "chown",
                "-R",
                f"{os.getuid()}:{os.getgid()}",
                "/work",
            ]
        )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(
            f"Build failed: {error}. "
            "See environment.log and output/*/build.log."
        ) from None
