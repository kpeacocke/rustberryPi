"""Emit a stable Hailo-8 release event when the upstream version changes."""

import asyncio
import importlib.util
import logging
from pathlib import Path


def release_version():
    path = Path(__file__).resolve().parents[4] / "tools/hailo_candidate.py"
    spec = importlib.util.spec_from_file_location(
        "hailo_release_builder", path
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return builder.latest_common_release(
        builder.get_release_tags(builder.REPOSITORIES["hailort"]),
        builder.get_release_tags(builder.REPOSITORIES["driver"]),
    )


async def main(queue, args):
    interval = int(args.get("poll_interval", 3600))
    if interval < 300:
        raise ValueError("Hailo release polling must be at least 300 seconds")
    previous = None
    while True:
        try:
            version = await asyncio.to_thread(release_version)
        except (OSError, RuntimeError, ValueError):
            logging.exception("Hailo release check failed; no build triggered")
        else:
            if version != previous:
                await queue.put({"hailo_release": {"version": version}})
                previous = version
        await asyncio.sleep(interval)
