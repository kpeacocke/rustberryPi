"""Coverage for camera-gated display blanking.

Everything here runs without a camera or a compositor. The camera and the display
power commands are injected, so the decision logic is exercised directly.
"""
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('presence', Path(__file__).resolve().parents[1] / 'dashboard/presence.py')
presence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(presence)


class FakeCamera:
    """Replays a scripted sequence of motion readings, then repeats the last one."""

    def __init__(self, readings):
        self.readings = list(readings)
        self.closed = False

    def motion(self):
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]

    def close(self):
        self.closed = True


class FakeRunner:
    """Stands in for subprocess.call, recording every command it is asked to run."""

    def __init__(self, succeeds=('wlopm',), missing=()):
        self.succeeds = succeeds
        self.missing = missing
        self.calls = []

    def __call__(self, command):
        if command[0] in self.missing:
            raise OSError(2, 'No such file or directory')
        self.calls.append(command)
        return 0 if command[0] in self.succeeds else 1


class MotionFractionTests(unittest.TestCase):
    def test_identical_frames_report_no_movement(self):
        frame = bytes([100] * 64)
        self.assertEqual(presence.motion_fraction(frame, frame, 12), 0.0)

    def test_changes_below_the_threshold_are_treated_as_sensor_noise(self):
        before, after = bytes([100] * 64), bytes([108] * 64)
        self.assertEqual(presence.motion_fraction(before, after, 12), 0.0)

    def test_a_changed_region_is_reported_as_a_fraction_of_the_frame(self):
        before = bytes([100] * 64)
        after = bytes([200] * 16 + [100] * 48)
        self.assertEqual(presence.motion_fraction(before, after, 12), 0.25)

    def test_a_missing_or_mismatched_previous_frame_never_reports_movement(self):
        frame = bytes([100] * 64)
        self.assertEqual(presence.motion_fraction(None, frame, 12), 0.0)
        self.assertEqual(presence.motion_fraction(b'', frame, 12), 0.0)
        self.assertEqual(presence.motion_fraction(bytes([100] * 32), frame, 12), 0.0)


class ShouldBeOnTests(unittest.TestCase):
    def test_movement_keeps_the_screens_on(self):
        self.assertTrue(presence.should_be_on(True, 9999, 600))

    def test_stillness_inside_the_idle_window_keeps_the_screens_on(self):
        self.assertTrue(presence.should_be_on(False, 599, 600))

    def test_stillness_past_the_idle_window_blanks_the_screens(self):
        self.assertFalse(presence.should_be_on(False, 600, 600))

    def test_unknown_presence_never_blanks_the_screens(self):
        # A camera that is missing, busy, or erroring must not be able to darken the Pi.
        self.assertTrue(presence.should_be_on(None, 9999, 600))


class ScreensTests(unittest.TestCase):
    def test_probing_uses_the_wake_command_so_it_can_never_blank_the_screens(self):
        runner = FakeRunner()
        presence.Screens(runner=runner).set_on(True)
        self.assertEqual(runner.calls, [['wlopm', '--on', '*']])

    def test_an_uninstalled_command_falls_through_to_the_next_rung(self):
        runner = FakeRunner(succeeds=('xset',), missing=('wlopm',))
        screens = presence.Screens(runner=runner)
        screens.set_on(False)
        self.assertEqual(screens.commands[0][0], 'xset')
        self.assertIn(['xset', 'dpms', 'force', 'off'], runner.calls)

    def test_blanking_is_abandoned_when_no_command_is_supported(self):
        screens = presence.Screens(runner=FakeRunner(succeeds=()))
        screens.set_on(False)
        self.assertTrue(screens.on)
        self.assertIn('blanking disabled', screens.error)

    def test_an_unchanged_state_does_not_rerun_the_command(self):
        runner = FakeRunner()
        screens = presence.Screens(runner=runner)
        screens.set_on(False)
        before = len(runner.calls)
        screens.set_on(False)
        self.assertEqual(len(runner.calls), before)

    def test_a_failing_command_leaves_the_recorded_state_untouched(self):
        # The rung is selected because wake succeeds, but the blank command then fails.
        runner = FakeRunner(succeeds=('wake',))
        screens = presence.Screens(runner=runner, ladder=((['blank'], ['wake']),))
        screens.set_on(False)
        self.assertTrue(screens.on)
        self.assertIn('failed', screens.error)


class RunLoopTests(unittest.TestCase):
    def setUp(self):
        self.clock = [0.0]

    def tick(self):
        return self.clock[0]

    def advance(self, _seconds):
        self.clock[0] += 60

    def test_the_screens_blank_once_the_idle_window_has_elapsed(self):
        runner = FakeRunner()
        screens = presence.Screens(runner=runner)
        presence.run(FakeCamera([False]), screens, sleep=self.advance, clock=self.tick, iterations=12)
        self.assertFalse(screens.on)
        self.assertIn(['wlopm', '--off', '*'], runner.calls)

    def test_movement_wakes_screens_that_have_already_blanked(self):
        runner = FakeRunner()
        screens = presence.Screens(runner=runner)
        camera = FakeCamera([False] * 12 + [True])
        presence.run(camera, screens, sleep=self.advance, clock=self.tick, iterations=14)
        self.assertTrue(screens.on)
        self.assertEqual(runner.calls[-1], ['wlopm', '--on', '*'])

    def test_a_camera_that_fails_leaves_the_screens_on_indefinitely(self):
        runner = FakeRunner()
        screens = presence.Screens(runner=runner)
        presence.run(FakeCamera([None]), screens, sleep=self.advance, clock=self.tick, iterations=30)
        self.assertTrue(screens.on)
        self.assertNotIn(['wlopm', '--off', '*'], runner.calls)


if __name__ == '__main__':
    unittest.main()
