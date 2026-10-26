#!/usr/bin/env python3
"""Build a private, uninstalled Hailo-8 source candidate on the Pi."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

REPOSITORIES = {
    "hailort": "hailo-ai/hailort",
    "driver": "hailo-ai/hailort-drivers",
}
FIRMWARE_BASE = "https://hailo-hailort.s3.eu-west-2.amazonaws.com"
VERSION_PATTERN = re.compile(r"^v?(4)\.(\d+)\.(\d+)$")
MAX_FIRMWARE_BYTES = 256 * 1024 * 1024
ROOT_DEFAULT = Path("/var/lib/rustberrypi-hailo")


def version_key(value):
    match = VERSION_PATTERN.fullmatch(value)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def get_release_tags(repository):
    tags = set()
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{repository}/releases"
            f"?per_page=100&page={page}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "rustberrypi-hailo-candidate-builder",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                releases = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Could not read stable releases for {repository}: {error}"
            ) from error
        if not releases:
            break
        if not isinstance(releases, list):
            raise RuntimeError(
                f"Unexpected release response from GitHub for {repository}"
            )
        tags.update(
            release["tag_name"]
            for release in releases
            if not release.get("draft")
            and not release.get("prerelease")
            and version_key(release.get("tag_name") or "") is not None
        )
        page += 1
    return tags


def latest_common_release(hailort_tags, driver_tags):
    common = {
        tag
        for tag in hailort_tags & driver_tags
        if version_key(tag) is not None
    }
    if not common:
        raise RuntimeError(
            "No common stable Hailo-8 (4.x) release exists in both "
            "repositories"
        )
    return max(common, key=version_key).removeprefix("v")


def run(command, cwd=None, env=None):
    subprocess.run(command, cwd=cwd, env=env, check=True)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_firmware(version, destination):
    url = f"{FIRMWARE_BASE}/Hailo8/{version}/FW/" f"hailo8_fw.{version}.bin"
    request = urllib.request.Request(
        url, headers={"User-Agent": "rustberrypi-hailo-candidate-builder"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_FIRMWARE_BYTES:
                raise RuntimeError(
                    "Downloaded firmware exceeds the size limit"
                )
            with destination.open("wb") as output:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_FIRMWARE_BYTES:
                        raise RuntimeError(
                            "Downloaded firmware exceeds the size limit"
                        )
                    output.write(chunk)
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError(
            f"Could not download matching Hailo firmware from {url}: {error}"
        ) from error
    if destination.stat().st_size == 0:
        raise RuntimeError("Downloaded Hailo firmware is empty")
    return url


def clone_repository(repository, tag, destination):
    run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            tag,
            f"https://github.com/{repository}.git",
            str(destination),
        ]
    )


def build_candidate(root, version, firmware_eula_accepted):
    if not firmware_eula_accepted:
        raise RuntimeError(
            "Hailo firmware download is disabled until the operator has "
            "accepted the Hailo firmware EULA and set the private AWX host "
            "variable rust_maintenance_hailo_firmware_eula_accepted=true"
        )
    if root.resolve() != ROOT_DEFAULT:
        raise RuntimeError(f"Candidate storage is fixed at {ROOT_DEFAULT}")
    if os.geteuid() != 0:
        raise RuntimeError("Hailo candidate builds must run as root")
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "Candidate version must be a stable Hailo 4.x version"
        )
    if platform.machine() != "aarch64":
        raise RuntimeError("Hailo candidate builds require an aarch64 Pi")

    kernel = os.uname().release
    kernel_build = Path("/lib/modules") / kernel / "build"
    if not (kernel_build / "Makefile").is_file():
        raise RuntimeError(
            f"Exact running-kernel headers are missing: {kernel_build}"
        )

    candidates = root / "candidates"
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    candidates.mkdir(parents=True, exist_ok=True, mode=0o755)
    root.chmod(0o755)
    candidates.chmod(0o755)
    candidate = candidates / version
    if candidate.is_symlink():
        raise RuntimeError(
            f"Candidate path must not be a symlink: {candidate}"
        )
    if (candidate / "READY").is_file():
        print(f"Hailo {version} candidate already exists; no changes made.")
        return
    if candidate.exists():
        shutil.rmtree(candidate)
    candidate.mkdir(mode=0o755)
    sources = candidate / "source"
    sources.mkdir()
    runtime = candidate / "runtime"
    driver_module = candidate / "driver" / "hailo_pci.ko"
    firmware = candidate / "firmware" / "hailo8_fw.bin"
    firmware.parent.mkdir(mode=0o700)
    driver_module.parent.mkdir()

    clone_repository(
        REPOSITORIES["hailort"], f"v{version}", sources / "hailort"
    )
    clone_repository(REPOSITORIES["driver"], f"v{version}", sources / "driver")

    runtime_build = candidate / "build" / "hailort"
    run(
        [
            "cmake",
            "-S",
            str(sources / "hailort" / "hailort"),
            "-B",
            str(runtime_build),
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_INSTALL_PREFIX={runtime}",
            "-DHAILO_BUILD_GSTREAMER=OFF",
            "-DHAILO_BUILD_EXAMPLES=OFF",
            "-DHAILO_BUILD_UT=OFF",
        ]
    )
    run(["cmake", "--build", str(runtime_build), "--target", "install", "-j2"])

    venv = candidate / "venv"
    run(
        [
            sys.executable,
            "-m",
            "venv",
            "--system-site-packages",
            str(venv),
        ]
    )
    python_source = (
        sources
        / "hailort"
        / "hailort"
        / "libhailort"
        / "bindings"
        / "python"
        / "platform"
    )
    library = runtime / "lib" / f"libhailort.so.{version}"
    python_env = os.environ.copy()
    python_env.update(
        {
            "CMAKE_BUILD_TYPE": "Release",
            "LIBHAILORT_PATH": str(library),
            "HAILORT_INCLUDE_DIR": str(runtime / "include"),
            "PYTHON_EXECUTABLE": str(venv / "bin" / "python"),
        }
    )
    run(
        [
            str(venv / "bin" / "python"),
            "setup.py",
            "bdist_wheel",
        ],
        cwd=python_source,
        env=python_env,
    )
    wheels = sorted((python_source / "dist").glob("hailort-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(
            f"Expected exactly one HailoRT wheel, found {len(wheels)}"
        )
    run(
        [
            str(venv / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--no-deps",
            str(wheels[0]),
        ]
    )

    driver_source = sources / "driver"
    run(
        [
            "make",
            "-C",
            str(driver_source / "linux" / "pcie"),
            "all",
            f"KERNEL_DIR={kernel_build}",
        ]
    )
    built_module = (
        driver_source
        / "linux"
        / "pcie"
        / "build"
        / "release"
        / "aarch64"
        / "hailo_pci.ko"
    )
    if not built_module.is_file():
        raise RuntimeError(f"Driver build did not produce {built_module}")
    shutil.copy2(built_module, driver_module)

    firmware_url = download_firmware(version, firmware)
    firmware.chmod(0o600)
    checksums = {
        str(path.relative_to(candidate)): sha256(path)
        for path in (
            driver_module,
            firmware,
            runtime / "bin" / "hailortcli",
            library,
            wheels[0],
        )
    }
    manifest = {
        "schema_version": 1,
        "version": version,
        "hailort_tag": f"v{version}",
        "driver_tag": f"v{version}",
        "architecture": platform.machine(),
        "kernel_release": kernel,
        "firmware_url": firmware_url,
        "firmware_eula_accepted": True,
        "candidate_only": True,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "sha256": checksums,
        "source_commits": {
            name: subprocess.check_output(
                ["git", "-C", str(sources / name), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            for name in ("hailort", "driver")
        },
    }
    (candidate / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (candidate / "READY").write_text("candidate-built\n", encoding="ascii")
    print(f"Built private Hailo {version} candidate; it is not installed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware-eula-accepted", action="store_true")
    args = parser.parse_args()
    if not args.firmware_eula_accepted:
        parser.error(
            "firmware download requires the operator-approved "
            "--firmware-eula-accepted flag"
        )
    version = latest_common_release(
        get_release_tags(REPOSITORIES["hailort"]),
        get_release_tags(REPOSITORIES["driver"]),
    )
    active_file = ROOT_DEFAULT / "active-version"
    active = (
        active_file.read_text(encoding="ascii").strip()
        if active_file.exists()
        else ""
    )
    if not active:
        installed = subprocess.run(
            ["modinfo", "-F", "version", "hailo_pci"],
            capture_output=True,
            check=False,
            text=True,
        )
        if installed.returncode == 0:
            active = installed.stdout.strip()
    if active:
        active_version = version_key(active)
        if active_version is None:
            raise RuntimeError(f"Invalid installed Hailo release: {active}")
        if active_version >= version_key(version):
            print(
                f"Installed Hailo {active} is current; "
                "no candidate is needed."
            )
            return
    build_candidate(ROOT_DEFAULT, version, args.firmware_eula_accepted)


if __name__ == "__main__":
    main()
