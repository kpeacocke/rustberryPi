#!/usr/bin/env python3
"""Blank the dashboard displays when nobody is in the room.

Runs as the desktop user inside the graphical session. The Pi camera is used only
as a presence sensor: frames are compared in memory and discarded immediately. No
image is written to disk, logged, or served anywhere.

The failure direction is deliberate. Anything unknown -- no camera, no supported
blanking command, an unexpected error -- leaves the screens on. A black screen that
cannot be woken would need physical recovery, so it is never an acceptable outcome.
"""
import os
import signal
import subprocess
import sys
import time

IDLE_SECONDS = float(os.environ.get('RUST_SCREEN_IDLE_SECONDS', 600))
SAMPLE_SECONDS = float(os.environ.get('RUST_SCREEN_SAMPLE_SECONDS', 0.5))
# Per-pixel luma change counted as movement, on a 0-255 scale. Below this is sensor noise.
PIXEL_THRESHOLD = int(os.environ.get('RUST_SCREEN_PIXEL_THRESHOLD', 12))
# Fraction of the subsampled frame that must change before movement is declared.
AREA_FRACTION = float(os.environ.get('RUST_SCREEN_AREA_FRACTION', 0.004))
# Frames discarded while automatic exposure and white balance settle.
WARMUP_FRAMES = int(os.environ.get('RUST_SCREEN_WARMUP_FRAMES', 10))

# DPMS-style power control only. Disabling an output instead of powering it down makes
# wlroots reflow clients, which would scatter the three positioned dashboard windows.
BLANK_LADDER = (
    (['wlopm', '--off', '*'], ['wlopm', '--on', '*']),
    (['xset', 'dpms', 'force', 'off'], ['xset', 'dpms', 'force', 'on']),
)


def motion_fraction(previous, current, pixel_threshold=PIXEL_THRESHOLD):
    """Fraction of luma samples that changed by more than pixel_threshold.

    Both arguments are equal-length luma planes. Pure function: no camera, no state.
    """
    if not previous or len(previous) != len(current):
        return 0.0
    changed = sum(1 for before, after in zip(previous, current) if abs(before - after) > pixel_threshold)
    return changed / len(previous)


def should_be_on(motion, seconds_since_motion, idle_seconds=IDLE_SECONDS):
    """Whether the displays should be powered at this moment.

    motion is True, False, or None when presence cannot be determined at all.
    """
    if motion is None:
        return True  # Unknown presence never blanks.
    if motion:
        return True
    return seconds_since_motion < idle_seconds


class Camera:
    """Pi camera reduced to a single boolean. Frames never leave this object."""

    def __init__(self):
        self.picam = None
        self.previous = None
        self.warmup = WARMUP_FRAMES
        self.error = None
        self._next_retry = 0.0

    def _open(self):
        from picamera2 import Picamera2  # Imported late so the module loads without hardware.
        picam = Picamera2()
        # Smallest useful stream. Luma only; colour is never requested or examined.
        picam.configure(picam.create_video_configuration(lores={'size': (320, 240), 'format': 'YUV420'},
                                                         buffer_count=2))
        picam.start()
        return picam

    def _luma(self):
        frame = self.picam.capture_array('lores')
        # Y plane is the first 240 rows; stride by 4 to compare roughly 80x60 samples.
        return frame[:240:4, :320:4].tobytes()

    def motion(self):
        """True, False, or None when the camera is unavailable."""
        now = time.monotonic()
        if self.picam is None:
            if now < self._next_retry:
                return None
            try:
                self.picam = self._open()
                self.previous = None
                self.warmup = WARMUP_FRAMES
                self.error = None
            except Exception as exc:  # Missing module, no camera, or already in use.
                self.error = f'{type(exc).__name__}: {exc}'
                self._next_retry = now + 30
                return None
        try:
            current = self._luma()
        except Exception as exc:
            self.error = f'{type(exc).__name__}: {exc}'
            self.close()
            self._next_retry = now + 30
            return None
        previous, self.previous = self.previous, current
        if self.warmup > 0:
            self.warmup -= 1
            return True  # Treat the settling period as presence rather than as stillness.
        return motion_fraction(previous, current) > AREA_FRACTION

    def close(self):
        picam, self.picam = self.picam, None
        self.previous = None
        if picam is not None:
            try:
                picam.stop()
                picam.close()
            except Exception:
                pass


class Screens:
    """Display power, using whichever command this session actually supports."""

    def __init__(self, runner=subprocess.call, ladder=BLANK_LADDER):
        self.runner = runner
        self.ladder = ladder
        self.commands = None
        self.on = True
        self.error = None

    def _probe(self):
        """Pick the first rung whose wake command succeeds, so probing never blanks."""
        for blank, wake in self.ladder:
            try:
                if self.runner(wake) == 0:
                    return (blank, wake)
            except OSError:
                continue  # Command not installed.
        return ()

    def set_on(self, wanted):
        if self.commands is None:
            self.commands = self._probe()
            if not self.commands:
                self.error = 'no supported display power command; blanking disabled'
        if not self.commands or wanted == self.on:
            return
        blank, wake = self.commands
        try:
            if self.runner(wake if wanted else blank) == 0:
                self.on = wanted
            else:
                self.error = 'display power command failed'
        except OSError as exc:
            self.error = f'{type(exc).__name__}: {exc}'


def run(camera, screens, sleep=time.sleep, clock=time.monotonic, iterations=None):
    """Main loop. iterations bounds the loop for tests; None runs until stopped."""
    last_motion = clock()
    count = 0
    while iterations is None or count < iterations:
        count += 1
        motion = camera.motion()
        now = clock()
        if motion:
            last_motion = now
        screens.set_on(should_be_on(motion, now - last_motion, IDLE_SECONDS))
        sleep(SAMPLE_SECONDS)
    return screens.on


def main():
    camera, screens = Camera(), Screens()

    def restore(*_):
        # Never exit leaving a dark screen behind, whatever the reason for exiting.
        screens.set_on(True)
        camera.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, restore)
    signal.signal(signal.SIGINT, restore)
    try:
        run(camera, screens)
    except Exception as exc:
        print(f'presence: stopping after {type(exc).__name__}: {exc}', file=sys.stderr)
    finally:
        screens.set_on(True)
        camera.close()


if __name__ == '__main__':
    main()
