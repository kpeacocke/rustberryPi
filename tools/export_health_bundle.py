#!/usr/bin/env python3
"""Export private numeric telemetry and portable Hailo build tools."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NUMBERS = (
    "schema",
    "at",
    "game_uptime_seconds",
    "player_count",
    "entities",
    "fps",
    "cpu_mean",
    "memory_percent",
    "swap_used_mib",
    "temperature",
    "read_kib_s",
    "write_kib_s",
    "a2s_latency_ms",
    "rcon_latency_ms",
)
FLAGS = (
    "rcon_ok",
    "a2s_ok",
    "throttled_now",
    "throttle_cpu_now",
    "watchdog_blocked",
)
SOURCES = (
    "tools/train_server_health.py",
    "telemetry/hat.py",
    "telemetry/health.py",
)


def numeric_row(row):
    if not isinstance(row, dict):
        raise ValueError("Feature row must be an object")
    result = {}
    for name in NUMBERS:
        value = row.get(name)
        if value is None:
            result[name] = None
        elif (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            result[name] = value
        else:
            raise ValueError("Invalid numeric feature: " + name)
    for name in FLAGS:
        value = row.get(name)
        if value is not None and not isinstance(value, bool):
            raise ValueError("Invalid feature flag: " + name)
        result[name] = value
    state = row.get("active_state")
    result["active_state"] = (
        state
        if state
        in ("active", "inactive", "failed", "activating", "deactivating")
        else None
    )
    return result


def export_bundle(features, output, architecture="hailo8"):
    paths = sorted(features.glob("features-*.jsonl"))
    if not paths:
        raise ValueError("No feature archives found")
    output.parent.mkdir(parents=True, exist_ok=True)
    hashes = {}
    rows = 0
    # Exclusive creation avoids overwriting a previous snapshot.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream, zipfile.ZipFile(
            stream, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:

            def write(name, content):
                archive.writestr(name, content)
                hashes[name] = hashlib.sha256(content).hexdigest()

            for name in SOURCES:
                write(name, (ROOT / name).read_bytes())
            write("telemetry/__init__.py", b"")
            for name in ("build.py", "Dockerfile", "README.txt"):
                write(name, (ROOT / "tools/health_bundle" / name).read_bytes())
            write(
                "bundle.json",
                json.dumps(
                    {"version": 1, "architecture": architecture}
                ).encode(),
            )
            for path in paths:
                if path.is_symlink() or not path.is_file():
                    raise ValueError(
                        "Feature archives must be regular non-symlink files"
                    )
                digest = hashlib.sha256()
                # Snapshot the size while the collector may still append.
                with path.open("rb") as source, archive.open(
                    "features/" + path.name, "w"
                ) as target:
                    remaining = path.stat().st_size
                    while remaining:
                        line = source.readline(min(remaining, 65537))
                        remaining -= len(line)
                        if not line:
                            raise ValueError(
                                "Feature archive changed during export: "
                                + path.name
                            )
                        if not line.endswith(b"\n") and remaining == 0:
                            # Exclude the collector's incomplete final row.
                            break
                        if len(line) > 65536:
                            raise ValueError(
                                "Oversized feature row: " + path.name
                            )
                        try:
                            clean = numeric_row(json.loads(line))
                        except (ValueError, OverflowError) as error:
                            raise ValueError(
                                "Invalid feature archive: " + path.name
                            ) from error
                        content = (
                            json.dumps(clean, allow_nan=False) + "\n"
                        ).encode()
                        target.write(content)
                        digest.update(content)
                        rows += 1
                hashes["features/" + path.name] = digest.hexdigest()
            if not rows:
                raise ValueError("No complete feature rows found")
            write(
                "snapshot.json",
                json.dumps(
                    {"rows": rows, "files": len(paths)}, indent=2
                ).encode(),
            )
            archive.writestr("SHA256.json", json.dumps(hashes, indent=2))
    except BaseException:
        output.unlink()
        raise
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--features",
        type=Path,
        default=Path("/srv/rust/data/.rustberrypi-telemetry/features"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--architecture", choices=("hailo8", "hailo8l"), default="hailo8"
    )
    args = parser.parse_args()
    try:
        count = export_bundle(args.features, args.output, args.architecture)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Export failed: {error}\n")
    print(
        f"Exported {count} numeric rows to {args.output}; "
        "keep this ZIP private."
    )


if __name__ == "__main__":
    main()
