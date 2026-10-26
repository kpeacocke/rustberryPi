#!/usr/bin/env python3
"""Activate or roll back a locally built, explicitly approved Hailo
candidate.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

VERSION_PATTERN = re.compile(r"^4\.\d+\.\d+$")
MODULE = "hailo_pci"
ROOT_DEFAULT = Path("/var/lib/rustberrypi-hailo")
FIRMWARE_PATH = Path("/lib/firmware/hailo/hailo8_fw.bin")
CURRENT_LINK = Path("/opt/rustberrypi-hailo/current")
LDCONF = Path("/etc/ld.so.conf.d/rustberrypi-hailo.conf")
HAT_DROPIN = Path(
    "/etc/systemd/system/rust-health-hat.service.d/10-hailo-runtime.conf"
)
FIRMWARE_BASE = "https://hailo-hailort.s3.eu-west-2.amazonaws.com"
APT_PACKAGES = ("hailort-pcie-driver", "hailort", "python3-hailort")


def run(command, check=True, env=None):
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        timeout=1800,
        env=env,
    )


def verify_candidate(root, version):
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "Candidate version must be a stable Hailo 4.x version"
        )
    candidate = root / "candidates" / version
    if candidate.is_symlink() or not (candidate / "READY").is_file():
        raise ValueError(
            f"No complete local Hailo candidate exists for {version}"
        )
    manifest = json.loads(
        (candidate / "manifest.json").read_text(encoding="utf-8")
    )
    if (
        manifest.get("version") != version
        or manifest.get("candidate_only") is not True
        or manifest.get("firmware_eula_accepted") is not True
    ):
        raise ValueError(
            "Candidate manifest is incomplete or not EULA-approved"
        )
    checksums = manifest.get("sha256", {})
    required_checksums = {
        "driver/hailo_pci.ko",
        "firmware/hailo8_fw.bin",
        f"runtime/lib/libhailort.so.{version}",
        "runtime/bin/hailortcli",
    }
    if not required_checksums.issubset(checksums):
        raise ValueError("Candidate manifest omits required file checksums")
    for relative, expected in checksums.items():
        path = (candidate / relative).resolve()
        if candidate.resolve() not in path.parents or not path.is_file():
            raise ValueError(f"Invalid candidate checksum path: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f"Candidate checksum mismatch: {relative}")
    required = [
        candidate / "driver" / "hailo_pci.ko",
        candidate / "firmware" / "hailo8_fw.bin",
        candidate / "runtime" / "bin" / "hailortcli",
        candidate / "venv" / "bin" / "python",
        candidate / "source" / "driver" / "linux" / "pcie" / "dkms.conf.in",
    ]
    if not all(path.is_file() for path in required):
        raise ValueError(
            "Candidate is missing a required runtime or source file"
        )
    expected_firmware_url = (
        f"{FIRMWARE_BASE}/Hailo8/{version}/FW/hailo8_fw.{version}.bin"
    )
    if manifest.get("firmware_url") != expected_firmware_url:
        raise ValueError(
            "Candidate firmware source is not the official Hailo URL"
        )
    source_commits = manifest.get("source_commits", {})
    for name in ("hailort", "driver"):
        commit = source_commits.get(name, "")
        source = candidate / "source" / name
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise ValueError(f"Candidate is missing the {name} source commit")
        current = run(
            ["git", "-C", str(source), "rev-parse", "HEAD"]
        ).stdout.strip()
        if current != commit:
            raise ValueError(f"Candidate {name} source commit changed")
        run(["git", "-C", str(source), "diff", "--quiet", "HEAD", "--"])
    return candidate, manifest


def version_key(version):
    if not VERSION_PATTERN.fullmatch(version):
        return None
    return tuple(int(part) for part in version.split("."))


def copy_dkms_source(driver, version, source_root=Path("/usr/src")):
    source = source_root / f"{MODULE}-{version}"
    if source.exists():
        raise RuntimeError(
            "DKMS source path already exists; inspect before activation: "
            f"{source}"
        )
    source.mkdir(parents=True)
    mappings = [
        (
            driver / "linux" / "pcie",
            source / "hailort" / "drivers" / "linux" / "pcie",
        ),
        (driver / "common", source / "hailort" / "drivers" / "common"),
        (
            driver / "linux" / "vdma",
            source / "hailort" / "drivers" / "linux" / "vdma",
        ),
        (
            driver / "linux" / "utils",
            source / "hailort" / "drivers" / "linux" / "utils",
        ),
        (driver / "common" / "include", source / "common" / "include"),
    ]
    for origin, destination in mappings:
        shutil.copytree(origin, destination, symlinks=True)
    template = (driver / "linux" / "pcie" / "dkms.conf.in").read_text(
        encoding="utf-8"
    )
    config = template.replace("@PCIE_DRIVER_VERSION@", version)
    (source / "dkms.conf").write_text(config, encoding="utf-8")
    return source


def current_target():
    if CURRENT_LINK.is_symlink():
        return os.readlink(CURRENT_LINK)
    if CURRENT_LINK.exists():
        raise RuntimeError(f"Expected a symlink at {CURRENT_LINK}")
    return None


def atomic_link(target):
    CURRENT_LINK.parent.mkdir(parents=True, exist_ok=True)
    temporary = CURRENT_LINK.with_name("current.new")
    temporary.unlink(missing_ok=True)
    if target is not None:
        temporary.symlink_to(target)
        os.replace(temporary, CURRENT_LINK)
    else:
        CURRENT_LINK.unlink(missing_ok=True)


def write_active_version(root, version):
    path = root / "active-version"
    if version is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(version + "\n", encoding="ascii")
        path.chmod(0o600)


def restore_package_holds(previous_holds):
    package_set = set(APT_PACKAGES)
    current_holds = set(run(["apt-mark", "showhold"]).stdout.splitlines())
    to_unhold = sorted((current_holds & package_set) - previous_holds)
    to_hold = sorted((previous_holds & package_set) - current_holds)
    if to_unhold:
        run(["apt-mark", "unhold", *to_unhold])
    if to_hold:
        run(["apt-mark", "hold", *to_hold])


def activate(root, version):
    if os.geteuid() != 0:
        raise RuntimeError("Hailo candidate activation must run as root")
    if root.resolve() != ROOT_DEFAULT:
        raise RuntimeError(f"Candidate storage is fixed at {ROOT_DEFAULT}")
    candidate, manifest = verify_candidate(root, version)
    kernel = os.uname().release
    if manifest.get("architecture") != "aarch64":
        raise ValueError("Candidate was not built for aarch64")
    if manifest.get("kernel_release") != kernel:
        raise ValueError(
            f'Candidate driver targets {manifest.get("kernel_release")}; '
            f"the running kernel is {kernel}"
        )
    if not (Path("/lib/modules") / kernel / "build" / "Makefile").is_file():
        raise RuntimeError(
            "Exact running-kernel headers are required for DKMS"
        )

    old_link = current_target()
    old_marker = (
        (root / "active-version").read_text(encoding="ascii").strip()
        if (root / "active-version").is_file()
        else None
    )
    if old_marker and version_key(old_marker) is None:
        raise RuntimeError(
            f"Invalid active Hailo release marker: {old_marker}"
        )
    if old_marker and version_key(version) < version_key(old_marker):
        raise RuntimeError(
            f"Refusing to downgrade active Hailo {old_marker} to {version}"
        )
    old_firmware = (
        FIRMWARE_PATH.read_bytes() if FIRMWARE_PATH.is_file() else None
    )
    old_ldconf = LDCONF.read_bytes() if LDCONF.is_file() else None
    old_hat_dropin = HAT_DROPIN.read_bytes() if HAT_DROPIN.is_file() else None
    module_loaded = Path("/sys/module/hailo_pci").exists()
    old_module_version = ""
    probe = run(["modinfo", "-F", "version", MODULE], check=False)
    if probe.returncode == 0:
        old_module_version = probe.stdout.strip()
    if old_marker == version and old_link == str(candidate.resolve()):
        print(f"Hailo {version} is already active and verified.")
        return 0
    if old_module_version and version_key(old_module_version) is None:
        raise RuntimeError(
            f"Cannot guarantee driver rollback: installed driver version "
            f"{old_module_version} is not a stable Hailo 4.x release"
        )
    if old_module_version and version_key(old_module_version) >= version_key(
        version
    ):
        raise RuntimeError(
            f"Candidate {version} must be newer than installed driver "
            f"{old_module_version}"
        )
    candidate_dkms_source = Path("/usr/src") / f"{MODULE}-{version}"
    if candidate_dkms_source.exists():
        raise RuntimeError(
            "Candidate DKMS source already exists; inspect before activation: "
            f"{candidate_dkms_source}"
        )
    if (
        old_module_version
        and old_module_version != version
        and not (
            Path("/usr/src") / f"{MODULE}-{old_module_version}" / "dkms.conf"
        ).is_file()
    ):
        raise RuntimeError(
            f"Cannot guarantee driver rollback: DKMS source for "
            f"{old_module_version} is missing"
        )
    old_holds = set(run(["apt-mark", "showhold"]).stdout.splitlines())
    old_holds.intersection_update(APT_PACKAGES)

    rollback_root = root / "rollback"
    rollback_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = rollback_root / datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ"
    )
    backup.mkdir(mode=0o700)
    if old_firmware is not None:
        (backup / "hailo8_fw.bin").write_bytes(old_firmware)
        (backup / "hailo8_fw.bin").chmod(0o600)
    state = {
        "previous_link": old_link,
        "previous_active_version": old_marker,
        "previous_module_version": old_module_version,
        "previous_module_loaded": module_loaded,
        "previous_held_packages": sorted(old_holds),
        "previous_firmware_present": old_firmware is not None,
        "previous_ldconf": old_ldconf.decode("utf-8") if old_ldconf else None,
        "previous_hat_dropin": (
            old_hat_dropin.decode("utf-8") if old_hat_dropin else None
        ),
        "candidate_version": version,
        "kernel_release": kernel,
    }
    (backup / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    (backup / "state.json").chmod(0o600)

    try:
        copy_dkms_source(candidate / "source" / "driver", version)
        candidate_status = run(
            ["dkms", "status", "-m", MODULE, "-v", version],
            check=False,
        )
        if candidate_status.returncode != 0:
            raise RuntimeError(
                f"Could not determine whether DKMS already has "
                f"{MODULE}/{version}"
            )
        if f"{MODULE}/{version}" in candidate_status.stdout:
            raise RuntimeError(
                f"DKMS already has {MODULE}/{version}; refusing ambiguous "
                "candidate activation"
            )
        if module_loaded:
            run(["modprobe", "-r", MODULE])
        FIRMWARE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_firmware = FIRMWARE_PATH.with_suffix(".bin.new")
        shutil.copy2(
            candidate / "firmware" / "hailo8_fw.bin", temporary_firmware
        )
        os.replace(temporary_firmware, FIRMWARE_PATH)

        run(["dkms", "add", "-m", MODULE, "-v", version])
        run(
            [
                "dkms",
                "build",
                "-m",
                MODULE,
                "-v",
                version,
                "-k",
                kernel,
                "--force",
            ]
        )
        run(
            [
                "dkms",
                "install",
                "-m",
                MODULE,
                "-v",
                version,
                "-k",
                kernel,
                "--force",
            ]
        )
        run(["depmod", "-a", kernel])

        target = str(candidate.resolve())
        atomic_link(target)
        LDCONF.write_text(
            str(CURRENT_LINK / "runtime" / "lib") + "\n", encoding="utf-8"
        )
        run(["ldconfig"])
        run(["modprobe", MODULE])

        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(CURRENT_LINK / "runtime" / "lib")
        run(
            [
                str(CURRENT_LINK / "runtime" / "bin" / "hailortcli"),
                "fw-control",
                "identify",
            ],
            env=env,
        )
        run(
            [
                str(CURRENT_LINK / "venv" / "bin" / "python"),
                "-c",
                "import numpy, hailo_platform",
            ],
            env=env,
        )
        HAT_DROPIN.parent.mkdir(parents=True, exist_ok=True)
        HAT_DROPIN.write_text(
            "[Service]\n"
            f"Environment=LD_LIBRARY_PATH={CURRENT_LINK}/runtime/lib\n"
            "ExecStart=\n"
            f"ExecStart={CURRENT_LINK}/venv/bin/python -m telemetry.hat\n",
            encoding="utf-8",
        )
        run(["systemctl", "daemon-reload"])
        run(
            [
                "apt-mark",
                "hold",
                *APT_PACKAGES,
            ]
        )
        write_active_version(root, version)
    except Exception as error:
        rollback_errors = []
        try:
            if Path("/sys/module/hailo_pci").exists():
                run(["modprobe", "-r", MODULE])
            if old_firmware is None:
                FIRMWARE_PATH.unlink(missing_ok=True)
            else:
                FIRMWARE_PATH.write_bytes(old_firmware)
            atomic_link(old_link)
            if old_ldconf is None:
                LDCONF.unlink(missing_ok=True)
            else:
                LDCONF.write_bytes(old_ldconf)
            if old_hat_dropin is None:
                HAT_DROPIN.unlink(missing_ok=True)
                if HAT_DROPIN.parent.is_dir() and not any(
                    HAT_DROPIN.parent.iterdir()
                ):
                    HAT_DROPIN.parent.rmdir()
            else:
                HAT_DROPIN.parent.mkdir(parents=True, exist_ok=True)
                HAT_DROPIN.write_bytes(old_hat_dropin)
            run(["systemctl", "daemon-reload"])
            run(["ldconfig"])
            candidate_status = run(
                ["dkms", "status", "-m", MODULE, "-v", version],
                check=False,
            )
            if candidate_status.returncode != 0:
                raise RuntimeError(
                    "Could not inspect candidate DKMS state during rollback"
                )
            if f"{MODULE}/{version}" in candidate_status.stdout:
                run(
                    [
                        "dkms",
                        "remove",
                        "-m",
                        MODULE,
                        "-v",
                        version,
                        "--all",
                    ]
                )
                if candidate_dkms_source.exists():
                    shutil.rmtree(candidate_dkms_source)
            if old_module_version:
                run(
                    [
                        "dkms",
                        "install",
                        "-m",
                        MODULE,
                        "-v",
                        old_module_version,
                        "-k",
                        kernel,
                        "--force",
                    ]
                )
            else:
                run(
                    [
                        "dkms",
                        "remove",
                        "-m",
                        MODULE,
                        "-v",
                        version,
                        "-k",
                        kernel,
                    ],
                    check=False,
                )
            run(["depmod", "-a", kernel])
            restore_package_holds(old_holds)
            if module_loaded:
                run(["modprobe", MODULE])
            write_active_version(root, old_marker)
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            message = (
                f"Candidate activation failed ({error}); "
                "rollback also failed: " + "; ".join(rollback_errors)
            )
            raise RuntimeError(message) from error
        print(
            f"Candidate {version} failed validation and was rolled back: "
            f"{error}",
            file=sys.stderr,
        )
        return 10

    print(f"Activated and verified Hailo {version}; rollback data: {backup}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--root", type=Path, default=ROOT_DEFAULT)
    args = parser.parse_args()
    try:
        return activate(args.root, args.version)
    except Exception as error:
        print(f"Hailo activation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
