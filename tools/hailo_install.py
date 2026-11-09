#!/usr/bin/env python3
"""Activate or roll back a locally built, explicitly approved Hailo
candidate.
"""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
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
DISABLED_MARKER = ROOT_DEFAULT / "disabled"
VERIFIED_MARKER = ROOT_DEFAULT / "verified.json"
FAILURE_MARKER = ROOT_DEFAULT / "last-failure.json"
MODPROBE_POLICY = Path(
    "/etc/modprobe.d/rustberrypi-hailo-disabled.conf"
)
MODULES_LOAD = Path("/etc/modules-load.d/rustberrypi-hailo.conf")
HAT_LOCAL_UNIT = Path("/etc/systemd/system/rust-health-hat.service")
HAT_VENDOR_UNIT = Path("/usr/local/lib/systemd/system/rust-health-hat.service")
LIFECYCLE_LOCK = Path("/run/lock/pi5-rust.lock")
PCI_DEVICES = Path("/sys/bus/pci/devices")
DISABLED_POLICY = (
    "blacklist hailo_pci\n"
    "install hailo_pci /bin/false\n"
)
MODULES_LOAD_POLICY = "hailo_pci\n"
FIRMWARE_BASE = "https://hailo-hailort.s3.eu-west-2.amazonaws.com"
APT_PACKAGES = ("hailort-pcie-driver", "hailort", "python3-hailort")
DKMS_SOURCE_ROOT = Path("/usr/src")


@contextmanager
def candidate_lock(root, timeout=21600):
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor = os.open(
        root / ".candidate.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "r") as lock:
        os.fchmod(lock.fileno(), 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Timed out waiting for the Hailo candidate artifact lock"
                    )
                time.sleep(0.25)
        yield


def run(command, check=True, env=None, timeout=1800):
    return subprocess.run(
        command,
        shell=False,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


@contextmanager
def lifecycle_lock():
    if os.geteuid() != 0:
        raise RuntimeError("Hailo control actions must run as root")
    descriptor = os.open(
        LIFECYCLE_LOCK,
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "r") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise RuntimeError("Rust lifecycle lock is not a root-owned regular file")
        os.fchmod(lock.fileno(), 0o600)
        deadline = time.monotonic() + 300
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for the Rust lifecycle lock")
                time.sleep(0.25)
        yield


def _owned_regular(path, *, private=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError(f"Refusing unsafe Hailo control file: {path}")
    if private and stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError(f"Private Hailo control file has unsafe permissions: {path}")
    return info


def _read_owned(path, *, private=False):
    info = _owned_regular(path, private=private)
    if info is None:
        return None
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        opened = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_ino != info.st_ino
            or opened.st_uid != os.geteuid()
        ):
            raise RuntimeError(f"Hailo control file changed while opening: {path}")
        if private and stat.S_IMODE(opened.st_mode) & 0o077:
            raise RuntimeError(f"Private Hailo control file has unsafe permissions: {path}")
        return source.read()


def _atomic_write(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read_owned(path, private=mode == 0o600)
    if current == content:
        return False
    temporary = path.with_name(path.name + ".new")
    if _owned_regular(temporary, private=mode == 0o600) is not None:
        temporary.unlink()
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            os.fchmod(target.fileno(), mode)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _remove_owned(path, expected=None, *, private=False):
    content = _read_owned(path, private=private)
    if content is None:
        return False
    if expected is not None and content != expected:
        raise RuntimeError(f"Refusing to remove unexpected Hailo control file: {path}")
    path.unlink()
    return True


def _read_private_json(path):
    content = _read_owned(path, private=True)
    if content is None:
        return None
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError(f"Invalid Hailo control state: {path}")
    return value


def _write_private_json(path, value):
    return _atomic_write(path, json.dumps(value, sort_keys=True) + "\n", 0o600)


def _write_failure(action, message):
    _write_private_json(
        FAILURE_MARKER,
        {"action": action, "error": message, "recorded_at": time.time()},
    )


def _clear_failure():
    _remove_owned(FAILURE_MARKER, private=True)


def _marker_present(path, expected):
    content = _read_owned(path, private=True)
    if content is None:
        return False
    if content != expected:
        raise RuntimeError(f"Invalid Hailo control marker: {path}")
    return True


def _read_active_version(root):
    content = _read_owned(root / "active-version", private=True)
    if content is None:
        return None
    version = content.strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise RuntimeError(f"Invalid active Hailo release marker: {version}")
    return version


def safety_disabled():
    try:
        marker = _marker_present(DISABLED_MARKER, "disabled\n")
    except (OSError, RuntimeError):
        return True
    try:
        policy = _read_owned(MODPROBE_POLICY)
    except (OSError, RuntimeError):
        return True
    return marker or policy is not None


def hailo_hardware_present():
    if not PCI_DEVICES.is_dir():
        return False
    for device in PCI_DEVICES.iterdir():
        vendor = (device / "vendor").read_text(encoding="ascii").strip().lower()
        product = (device / "device").read_text(encoding="ascii").strip().lower()
        if vendor == "0x1e60" and product == "0x2864":
            return True
    return False


def module_loaded():
    return (Path("/sys/module") / MODULE).exists()


def verify_artifact_integrity(root, version):
    """Verify immutable build artifacts, including an installed historical runtime."""
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


def verify_candidate(root, version):
    candidate, manifest = verify_artifact_integrity(root, version)
    if manifest.get("architecture") != "aarch64":
        raise ValueError("Candidate was not built for aarch64")
    if manifest.get("kernel_release") != os.uname().release:
        raise ValueError("Candidate driver does not match the running kernel")
    return candidate, manifest


def version_key(version):
    if not VERSION_PATTERN.fullmatch(version):
        return None
    return tuple(int(part) for part in version.split("."))


def candidate_status(root, version=""):
    """Inspect complete local candidates without changing the installed driver."""
    if version and not VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "Candidate version must be a stable Hailo 4.x version"
        )
    kernel = os.uname().release
    probe = run(["modinfo", "-F", "version", MODULE], check=False)
    if probe.returncode != 0 or version_key(probe.stdout.strip()) is None:
        raise RuntimeError("Cannot determine installed Hailo driver version")
    installed = version_key(probe.stdout.strip())
    versions = (
        [version]
        if version
        else sorted(
            (
                path.name
                for path in (root / "candidates").glob("*")
                if VERSION_PATTERN.fullmatch(path.name)
                and (path / "READY").is_file()
            ),
            key=version_key,
            reverse=True,
        )
    )
    for selected in versions:
        candidate, manifest = verify_artifact_integrity(root, selected)
        if (
            manifest.get("architecture") != "aarch64"
            or manifest.get("kernel_release") != kernel
            or version_key(selected) <= installed
        ):
            if version:
                raise ValueError(
                    "Selected candidate must be newer than the installed "
                    "driver and match aarch64 and the running kernel"
                )
            continue
        return {
            "installed": probe.stdout.strip(),
            "available": selected,
            "candidate_ready": True,
            "kernel_release": kernel,
            "built_at": manifest.get("built_at"),
        }
    return {
        "installed": probe.stdout.strip(),
        "available": None,
        "candidate_ready": False,
        "kernel_release": kernel,
        "built_at": None,
    }


def select_candidate(root, version=""):
    """Select a complete local candidate for this kernel before downtime."""
    status = candidate_status(root, version)
    if not status["candidate_ready"]:
        raise ValueError(
            "No ready, newer Hailo candidate matches the running kernel; "
            "run the candidate build first"
        )
    return status["available"]


def maintenance_status(root, version):
    """Validate only the approved release, distinguishing current from failure."""
    if version_key(version) is None:
        raise ValueError("Maintenance requires an exact stable Hailo 4.x version")
    installed = run(["modinfo", "-F", "version", MODULE]).stdout.strip()
    if version_key(installed) is None:
        raise RuntimeError("Cannot determine installed Hailo driver version")
    if version_key(version) > version_key(installed):
        return candidate_status(root, version)
    if (root / "candidates" / version).exists():
        _, manifest = verify_artifact_integrity(root, version)
        if manifest.get("architecture") != "aarch64":
            raise ValueError("Selected candidate was not built for aarch64")
    vermagic = run(["modinfo", "-F", "vermagic", MODULE]).stdout.split()
    if not vermagic or vermagic[0] != os.uname().release:
        raise ValueError("Installed Hailo module does not match the running kernel")
    return {
        "installed": installed,
        "available": None,
        "candidate_ready": False,
        "kernel_release": os.uname().release,
        "built_at": None,
    }


def verify_installed(root, version):
    if safety_disabled():
        raise RuntimeError(
            "Hailo is safety-disabled; use the explicit enable-and-verify "
            "control action first"
        )
    status, cli, env = validate_installed(root, version)
    run(["modprobe", MODULE], timeout=120)
    run([cli, "fw-control", "identify"], env=env, timeout=120)
    _write_private_json(
        VERIFIED_MARKER,
        {
            "driver_version": status["installed"],
            "kernel_release": status["kernel_release"],
            "verified_at": time.time(),
        },
    )
    _clear_failure()
    print(
        f"Hailo {status['installed']} is current; "
        "running-kernel hardware verification passed."
    )


def validate_installed(root, version):
    status = maintenance_status(root, version)
    if status["candidate_ready"]:
        raise ValueError("A newer selected candidate still requires activation")
    active = _read_active_version(root)
    source_managed = active is not None
    if source_managed:
        if active != status["installed"]:
            raise ValueError("Active Hailo runtime and installed driver differ")
        # OS DKMS may rebuild the installed module without rewriting this immutable manifest.
        candidate, manifest = verify_artifact_integrity(root, active)
        if manifest.get("architecture") != "aarch64":
            raise ValueError("Active runtime was not built for aarch64")
        if current_target() != str(candidate.resolve()):
            raise ValueError("Active Hailo runtime pointer differs from its manifest")
        cli = str(CURRENT_LINK / "runtime" / "bin" / "hailortcli")
        env = dict(os.environ, LD_LIBRARY_PATH=str(CURRENT_LINK / "runtime" / "lib"))
    else:
        cli, env = "hailortcli", None
    return status, cli, env


def _attempt(command, errors, timeout=120):
    try:
        run(command, timeout=timeout)
        return True
    except subprocess.CalledProcessError as error:
        errors.append(f"{command[0]} failed with exit code {error.returncode}")
    except (OSError, subprocess.TimeoutExpired) as error:
        errors.append(f"{command[0]} failed: {type(error).__name__}")
    return False


def _service_state():
    active = run(
        ["systemctl", "show", "rust-health-hat.service",
         "--property=ActiveState", "--value"],
        check=False,
        timeout=30,
    )
    unit_file = run(
        ["systemctl", "show", "rust-health-hat.service",
         "--property=UnitFileState", "--value"],
        check=False,
        timeout=30,
    )
    if active.returncode != 0 or unit_file.returncode != 0:
        raise RuntimeError("Could not inspect rust-health-hat.service state")
    return {
        "active": active.stdout.strip(),
        "unit_file_state": unit_file.stdout.strip(),
    }


def _stop_and_mask_hat(errors):
    stop_ok = _attempt(["systemctl", "stop", "rust-health-hat.service"], errors)
    try:
        # systemctl cannot mask a regular unit already occupying its mask path.
        if HAT_LOCAL_UNIT.is_symlink():
            if os.readlink(HAT_LOCAL_UNIT) != "/dev/null":
                raise RuntimeError("Unexpected HAT unit symlink; refusing replacement")
        else:
            content = _read_owned(HAT_LOCAL_UNIT)
            if content is not None:
                vendor = _read_owned(HAT_VENDOR_UNIT)
                if vendor is not None and vendor != content:
                    raise RuntimeError("Conflicting HAT vendor unit; refusing replacement")
                _atomic_write(HAT_VENDOR_UNIT, content, 0o644)
                _remove_owned(HAT_LOCAL_UNIT, content)
    except (OSError, RuntimeError) as error:
        errors.append(f"Could not prepare HAT service for persistent masking: {error}")
        return False
    mask_ok = _attempt(["systemctl", "mask", "rust-health-hat.service"], errors)
    reload_ok = _attempt(["systemctl", "daemon-reload"], errors)
    return stop_ok and mask_ok and reload_ok


def _disable_policy(errors, *, unload):
    try:
        _atomic_write(DISABLED_MARKER, "disabled\n", 0o600)
    except (OSError, RuntimeError) as error:
        errors.append(f"Could not persist the Hailo disabled marker: {error}")
    try:
        _atomic_write(MODPROBE_POLICY, DISABLED_POLICY, 0o644)
    except (OSError, RuntimeError) as error:
        errors.append(f"Could not persist the modprobe safety block: {error}")
    try:
        _remove_owned(
            MODULES_LOAD, MODULES_LOAD_POLICY
        )
    except (OSError, RuntimeError) as error:
        errors.append(f"Could not remove Hailo automatic loading: {error}")
    try:
        _remove_owned(VERIFIED_MARKER, private=True)
    except (OSError, RuntimeError) as error:
        errors.append(f"Could not clear previous Hailo verification state: {error}")
    service_safe = _stop_and_mask_hat(errors)

    if not _attempt(
        ["update-initramfs", "-u", "-k", "all"], errors, timeout=600
    ):
        errors.append(
            "The running system policy is retained, but initramfs "
            "integration could not be confirmed"
        )
    if unload and module_loaded():
        if not service_safe:
            errors.append(
                "hailo_pci remains loaded because the inference service "
                "could not be confirmed stopped and masked"
            )
        elif not _attempt(["modprobe", "-r", MODULE], errors):
            errors.append(
                "hailo_pci remains loaded; no forced unload was attempted"
            )

    loaded = module_loaded()
    if unload and loaded:
        errors.append("hailo_pci remains loaded; the unload postcondition was not met")
    return {
        "state": "disabled",
        "disabled": safety_disabled(),
        "module_loaded": loaded,
        "hardware_present": hailo_hardware_present(),
        "errors": errors,
    }


def control_status(root):
    disabled = True
    try:
        marker_present = _marker_present(DISABLED_MARKER, "disabled\n")
        policy = _read_owned(MODPROBE_POLICY)
        disabled = marker_present or policy is not None
        policy_active = policy == DISABLED_POLICY
        autoload = _read_owned(MODULES_LOAD)
        state = {
            "state": "disabled" if disabled else "installed-but-unverified",
            "disabled": disabled,
            "policy_active": policy_active,
            "module_loaded": module_loaded(),
            "hardware_present": hailo_hardware_present(),
            "kernel_release": os.uname().release,
            "driver_version": None,
            "last_failure": _read_private_json(FAILURE_MARKER),
        }
        service = _service_state()
        state["inference_active"] = service["active"] == "active"
        state["inference_masked"] = service["unit_file_state"] == "masked"
        if state["last_failure"]:
            state["state"] = "failure"
            state["error"] = state["last_failure"].get(
                "error", "The last Hailo control action failed"
            )
        if disabled:
            if (
                not policy_active
                or not marker_present
                or autoload is not None
                or state["inference_active"]
                or not state["inference_masked"]
            ):
                state["state"] = "failure"
                state["error"] = (
                    "The persistent disabled policy is incomplete; "
                    "review the safety control job before provisioning"
                )
            elif state["module_loaded"]:
                state["warning"] = (
                    "The disabled policy is active, but hailo_pci remains "
                    "loaded; it was not reported as unloaded"
                )
            source_version = _read_active_version(root)
            probe = run(
                ["modinfo", "-F", "version", MODULE], check=False, timeout=30
            )
            if probe.returncode == 0 and version_key(probe.stdout.strip()) is not None:
                state["driver_version"] = probe.stdout.strip()
            state["source_managed_version"] = source_version
            state["installed_version"] = source_version or state["driver_version"]
            state["verification_pending"] = state["installed_version"] is not None
            state["hardware_verification"] = (
                "pending" if state["verification_pending"] else "not-verified"
            )
            return state

        probe = run(
            ["modinfo", "-F", "version", MODULE],
            check=False,
            timeout=30,
        )
        if probe.returncode != 0 or version_key(probe.stdout.strip()) is None:
            if (
                probe.returncode != 0
                and not state["module_loaded"]
                and _read_active_version(root) is None
                and current_target() is None
                and not any(DKMS_SOURCE_ROOT.glob(f"{MODULE}-*"))
                and not any(Path("/lib/modules").glob(f"**/{MODULE}.ko*"))
            ):
                packages = run(
                    ["dpkg-query", "-W", "-f=${db:Status-Status}",
                     "hailort-pcie-driver"], check=False, timeout=30
                )
                if packages.returncode in (0, 1) and packages.stdout.strip() in (
                    "", "not-installed", "config-files"
                ):
                    state["state"] = "not-installed"
                    return state
            state["state"] = "failure"
            state["error"] = "Cannot determine an installed Hailo driver version"
            return state
        state["driver_version"] = probe.stdout.strip()
        vermagic = run(
            ["modinfo", "-F", "vermagic", MODULE],
            check=False,
            timeout=30,
        )
        if vermagic.returncode != 0 or not vermagic.stdout.split():
            state["state"] = "failure"
            state["error"] = "Cannot determine the installed Hailo module kernel"
            return state
        if vermagic.stdout.split()[0] != os.uname().release:
            state["state"] = "failure"
            state["error"] = "Installed Hailo module does not match the running kernel"
            return state
        verified = _read_private_json(VERIFIED_MARKER)
        if (
            verified
            and verified.get("driver_version") == state["driver_version"]
            and verified.get("kernel_release") == state["kernel_release"]
            and state["module_loaded"]
            and state["hardware_present"]
        ):
            validate_installed(root, state["driver_version"])
            state["state"] = "verified"
            state["verified_at"] = verified.get("verified_at")
        return state
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        return {
            "state": "failure",
            "disabled": disabled,
            "error": f"{type(error).__name__}: {error}",
        }


def _record_control_failure(action, error, restore_errors=None):
    message = str(error)
    if restore_errors:
        message += "; safety restoration issues: " + "; ".join(restore_errors)
    _write_failure(action, message)
    return {
        "state": "disabled" if safety_disabled() else "failure",
        "disabled": safety_disabled(),
        "module_loaded": module_loaded(),
        "hardware_present": hailo_hardware_present(),
        "last_failure": message,
    }


def disable_hailo():
    if os.geteuid() != 0:
        raise RuntimeError("Hailo control actions must run as root")
    errors = []
    result = _disable_policy(errors, unload=True)
    if errors:
        _write_failure("disable", "; ".join(errors))
        result["last_failure"] = "; ".join(errors)
        result["state"] = "failure" if not result["disabled"] else "disabled"
        return 1, result
    _clear_failure()
    return 0, result


def enable_and_verify(root):
    if os.geteuid() != 0:
        raise RuntimeError("Hailo control actions must run as root")
    try:
        safety_errors = []
        _disable_policy(safety_errors, unload=False)
        if safety_errors:
            raise RuntimeError("Could not establish verification safety: " + "; ".join(safety_errors))
        if module_loaded():
            raise RuntimeError(
                "hailo_pci is still loaded; disable it successfully before "
                "enable-and-verify so the verified driver is freshly loaded"
            )
        if not hailo_hardware_present():
            raise RuntimeError("No supported Hailo-8 PCI device is present")
        probe = run(
            ["modinfo", "-F", "version", MODULE], check=False, timeout=30
        )
        version = probe.stdout.strip()
        if probe.returncode != 0 or version_key(version) is None:
            raise RuntimeError("Cannot determine an installed Hailo driver version")
        status, cli, env = validate_installed(root, version)
        run(["modprobe", "--ignore-install", MODULE], timeout=120)
        run([cli, "fw-control", "identify"], env=env, timeout=120)
        source_managed = _read_active_version(root) is not None
        python = (
            str(CURRENT_LINK / "venv" / "bin" / "python")
            if source_managed
            else "/usr/bin/python3"
        )
        run(
            [
                python,
                "-c",
                "import numpy, hailo_platform",
            ],
            env=env,
            timeout=120,
        )
        _remove_owned(MODPROBE_POLICY, DISABLED_POLICY)
        _atomic_write(MODULES_LOAD, MODULES_LOAD_POLICY, 0o644)
        run(["update-initramfs", "-u", "-k", "all"], timeout=600)
        verified_at = time.time()
        _write_private_json(
            VERIFIED_MARKER,
            {
                "driver_version": status["installed"],
                "kernel_release": status["kernel_release"],
                "verified_at": verified_at,
            },
        )
        _remove_owned(DISABLED_MARKER, "disabled\n", private=True)
        _clear_failure()
        return 0, {
            "state": "verified",
            "disabled": False,
            "driver_version": status["installed"],
            "kernel_release": status["kernel_release"],
            "hardware_present": True,
            "inference_active": False,
            "inference_masked": True,
            "verified_at": verified_at,
        }
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        restore_errors = []
        restored = _disable_policy(restore_errors, unload=True)
        result = _record_control_failure(
            "enable-and-verify", error, restore_errors
        )
        result["disabled"] = restored["disabled"]
        result["module_loaded"] = restored["module_loaded"]
        return 1, result


def install_while_disabled(root, version):
    try:
        if os.geteuid() != 0:
            raise RuntimeError("Hailo control actions must run as root")
        if not safety_disabled():
            raise RuntimeError(
                "Install-while-disabled requires the persistent Hailo safety latch"
            )
        if (
            not _marker_present(DISABLED_MARKER, "disabled\n")
            or _read_owned(MODPROBE_POLICY) != DISABLED_POLICY
            or _read_owned(MODULES_LOAD) is not None
        ):
            raise RuntimeError(
                "Disabled installation requires the complete persistent "
                "Hailo boot safety policy; run Disable successfully first"
            )
        if module_loaded():
            raise RuntimeError(
                "hailo_pci is still loaded; disable it successfully before installing"
            )
        service = _service_state()
        if (
            service["active"] != "inactive"
            or service["unit_file_state"] != "masked"
        ):
            raise RuntimeError(
                "rust-health-hat.service must remain stopped and masked during "
                "disabled installation"
            )
        result = activate(root, version, disabled_install=True)
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        _write_failure("install-while-disabled", str(error))
        raise
    if result != 0:
        _write_failure(
            "install-while-disabled",
            "Candidate activation failed and rollback was reported",
        )
        return result
    _clear_failure()
    return 0


def copy_dkms_source(driver, version, source_root=Path("/usr/src")):
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("DKMS source requires a stable Hailo 4.x version")
    source = source_root / f"{MODULE}-{version}"
    if source.exists():
        raise RuntimeError(
            "DKMS source path already exists; inspect before activation: "
            f"{source}"
        )
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
    ]
    # Standalone releases keep driver headers directly in common.
    if (driver / "common" / "include").is_dir():
        mappings.append(
            (driver / "common" / "include", source / "common" / "include")
        )
    for origin, _ in mappings:
        if not origin.is_dir():
            raise FileNotFoundError(f"Required DKMS source directory missing: {origin}")
    template = (driver / "linux" / "pcie" / "dkms.conf.in").read_text(
        encoding="utf-8"
    )
    config = template.replace("@PCIE_DRIVER_VERSION@", version)
    source.mkdir(parents=True)
    try:
        for origin, destination in mappings:
            shutil.copytree(origin, destination, symlinks=True)
        (source / "dkms.conf").write_text(config, encoding="utf-8")
    except OSError:
        shutil.rmtree(source)
        raise
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
        target_path = Path(target)
        candidate_root = (ROOT_DEFAULT / "candidates").resolve()
        if (
            not target_path.is_absolute()
            or target_path.resolve() != target_path
            or candidate_root not in target_path.parents
        ):
            raise ValueError("Hailo runtime target must be an immutable local candidate")
        temporary.symlink_to(target)
        os.replace(temporary, CURRENT_LINK)
    else:
        CURRENT_LINK.unlink(missing_ok=True)


def write_active_version(root, version):
    path = root / "active-version"
    if version is None:
        _remove_owned(path, private=True)
    else:
        if not VERSION_PATTERN.fullmatch(version):
            raise ValueError("Active Hailo version must be a stable 4.x release")
        _atomic_write(path, version + "\n", 0o600)


def restore_package_holds(previous_holds):
    package_set = set(APT_PACKAGES)
    current_holds = set(run(["apt-mark", "showhold"]).stdout.splitlines())
    to_unhold = sorted((current_holds & package_set) - previous_holds)
    to_hold = sorted((previous_holds & package_set) - current_holds)
    if to_unhold:
        run(["apt-mark", "unhold", *to_unhold])
    if to_hold:
        run(["apt-mark", "hold", *to_hold])


def activate(root, version, disabled_install=False):
    if os.geteuid() != 0:
        raise RuntimeError("Hailo candidate activation must run as root")
    if root.resolve() != ROOT_DEFAULT:
        raise RuntimeError(f"Candidate storage is fixed at {ROOT_DEFAULT}")
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "Candidate version must be a stable Hailo 4.x version"
        )
    if disabled_install:
        if not safety_disabled():
            raise RuntimeError(
                "Install-while-disabled requires the persistent Hailo safety latch"
            )
        if module_loaded():
            raise RuntimeError(
                "Refusing disabled installation while hailo_pci remains loaded"
            )
    elif safety_disabled():
        raise RuntimeError(
            "Hailo safety latch is active; use the explicit "
            "install-while-disabled or enable-and-verify control action"
        )
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
    old_marker = _read_active_version(root)
    if old_marker and version_key(version) < version_key(old_marker):
        raise RuntimeError(
            f"Refusing to downgrade active Hailo {old_marker} to {version}"
        )
    old_firmware = (
        FIRMWARE_PATH.read_bytes() if FIRMWARE_PATH.is_file() else None
    )
    old_ldconf = LDCONF.read_bytes() if LDCONF.is_file() else None
    old_hat_dropin = HAT_DROPIN.read_bytes() if HAT_DROPIN.is_file() else None
    old_verified = _read_owned(VERIFIED_MARKER, private=True)
    was_module_loaded = module_loaded()
    old_module_version = ""
    probe = run(["modinfo", "-F", "version", MODULE], check=False)
    if probe.returncode == 0:
        old_module_version = probe.stdout.strip()
    if (
        old_marker == version
        and old_link == str(candidate.resolve())
        and not disabled_install
    ):
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(CURRENT_LINK / "runtime" / "lib")
        validate_runtime(candidate, env, verify_hardware=True)
        _write_private_json(
            VERIFIED_MARKER,
            {
                "driver_version": version,
                "kernel_release": kernel,
                "verified_at": time.time(),
            },
        )
        _clear_failure()
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
    candidate_dkms_source = DKMS_SOURCE_ROOT / f"{MODULE}-{version}"
    if candidate_dkms_source.exists():
        raise RuntimeError(
            "Candidate DKMS source already exists; inspect before activation: "
            f"{candidate_dkms_source}"
        )
    if (
        old_module_version
        and old_module_version != version
        and not (
            DKMS_SOURCE_ROOT / f"{MODULE}-{old_module_version}" / "dkms.conf"
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
        "previous_module_loaded": was_module_loaded,
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
        if was_module_loaded:
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

        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(CURRENT_LINK / "runtime" / "lib")
        validate_runtime(candidate, env, verify_hardware=not disabled_install)
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
        if not disabled_install:
            _write_private_json(
                VERIFIED_MARKER,
                {
                    "driver_version": version,
                    "kernel_release": kernel,
                    "verified_at": time.time(),
                },
            )
            _clear_failure()
    except Exception as error:
        rollback_errors = []
        try:
            if module_loaded() and not disabled_install:
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
            if was_module_loaded and not disabled_install:
                run(["modprobe", MODULE])
            write_active_version(root, old_marker)
            if old_verified is None:
                _remove_owned(VERIFIED_MARKER, private=True)
            else:
                _atomic_write(VERIFIED_MARKER, old_verified, 0o600)
        except (
            OSError,
            RuntimeError,
            ValueError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as rollback_error:
            rollback_errors.append(str(rollback_error))
        if disabled_install and not safety_disabled():
            rollback_errors.append(
                "Persistent Hailo safety latch was lost during rollback"
            )
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

    if disabled_install:
        print(
            f"Installed Hailo {version} while safety-disabled; hardware "
            f"remains unverified. Rollback data: {backup}"
        )
    else:
        print(f"Activated and verified Hailo {version}; rollback data: {backup}")
    return 0


def validate_runtime(candidate, env, *, verify_hardware):
    if verify_hardware:
        run(["modprobe", MODULE], timeout=120)
        run(
            [
                str(CURRENT_LINK / "runtime" / "bin" / "hailortcli"),
                "fw-control",
                "identify",
            ],
            env=env,
            timeout=120,
        )
    run(
        [
            str(candidate / "venv" / "bin" / "python"),
            "-c",
            "import numpy, hailo_platform",
        ],
        env=env,
        timeout=120,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?", default="", type=stable_version_argument)
    inspection = parser.add_mutually_exclusive_group()
    inspection.add_argument(
        "--select",
        action="store_true",
        help="Validate and print the selected local candidate; never install",
    )
    inspection.add_argument(
        "--status",
        action="store_true",
        help="Print validated candidate readiness as JSON; never install",
    )
    inspection.add_argument("--maintenance-status", action="store_true")
    inspection.add_argument("--verify-installed", action="store_true")
    inspection.add_argument("--quiesce", action="store_true")
    inspection.add_argument(
        "--control",
        choices=("status", "disable", "enable-verify", "install-disabled"),
        help="Inspect or explicitly change Hailo safety state",
    )
    parser.add_argument("--root", type=Path, default=ROOT_DEFAULT)
    args = parser.parse_args()
    try:
        with candidate_lock(args.root):
            return perform_operation(args)
    except Exception as error:
        print(f"Hailo operation failed: {error}", file=sys.stderr)
        return 1


def perform_operation(args):
    if args.quiesce:
        return 0
    if args.control:
        if args.root.resolve() != ROOT_DEFAULT:
            raise RuntimeError(f"Candidate storage is fixed at {ROOT_DEFAULT}")
        with lifecycle_lock():
            if args.control == "status":
                print(json.dumps(control_status(args.root), sort_keys=True))
                return 0
            if args.control == "disable":
                code, result = disable_hailo()
                print(json.dumps(result, sort_keys=True))
                return code
            if args.control == "enable-verify":
                code, result = enable_and_verify(args.root)
                print(json.dumps(result, sort_keys=True))
                return code
            if args.control == "install-disabled":
                return install_while_disabled(args.root, args.version)
    if args.maintenance_status:
        print(json.dumps(maintenance_status(args.root, args.version)))
        return 0
    if args.verify_installed:
        verify_installed(args.root, args.version)
        return 0
    if args.status:
        print(json.dumps(dict(
            candidate_status(args.root, args.version),
            ok=True, checked_at=time.time(),
        )))
        return 0
    if args.select:
        print(select_candidate(args.root, args.version))
        return 0
    if not args.version:
        raise ValueError("Activation requires an exact candidate version")
    return activate(args.root, args.version)


def stable_version_argument(value):
    if value == "":
        return value
    if not VERSION_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "Version must be an exact stable Hailo 4.x release"
        )
    return value


if __name__ == "__main__":
    sys.exit(main())
