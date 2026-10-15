"""Hardware discovery and provisioning gates without touching host peripherals."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('pi_hardware_detect', ROOT / 'roles/pi_hardware/files/detect.py')
detect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(detect)


class HardwareDiscoveryTests(unittest.TestCase):
    def pci(self, root, address, vendor, device):
        folder = root / 'bus/pci/devices' / address
        folder.mkdir(parents=True)
        (folder / 'vendor').write_text(vendor + '\n')
        (folder / 'device').write_text(device + '\n')

    def sensor(self, root, name, bound=True):
        folder = root / 'bus/i2c/devices/10-001a'
        folder.mkdir(parents=True)
        (folder / 'name').write_text(name + '\n')
        if bound:
            driver = root / 'bus/i2c/drivers' / name
            driver.mkdir(parents=True)
            (folder / 'driver').symlink_to(driver)
        return folder

    def test_no_peripherals_is_a_clean_skip(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(detect.detect(Path(folder)), {'hailo': [], 'cameras': []})

    def test_user_reported_hailo_and_imx708_are_detected_without_device_node(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.pci(root, '0001:01:00.0', '0x1e60', '0x2864')
            self.pci(root, '0002:01:00.0', '0x1de4', '0x0001')
            self.sensor(root, 'imx708')
            self.assertEqual(detect.detect(root), {
                'hailo': [{'address': '0001:01:00.0', 'device': '0x2864'}],
                'cameras': [{'address': '10-001a', 'sensor': 'imx708'}]})

    def test_unbound_camera_does_not_claim_working_hardware(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.sensor(root, 'imx708', bound=False)
            self.assertEqual(detect.detect(root)['cameras'], [])

    def test_other_i2c_devices_are_not_cameras(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.sensor(root, 'goodix-ts')
            self.assertEqual(detect.detect(root)['cameras'], [])

    def test_detection_errors_are_not_hidden_as_absent_hardware(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            device = root / 'bus/pci/devices/0001:01:00.0'
            device.mkdir(parents=True)
            with self.assertRaises(OSError):
                detect.detect(root)


class HardwareProvisioningTests(unittest.TestCase):
    def tasks(self, path):
        return yaml.safe_load((ROOT / path).read_text())

    def test_both_deployment_paths_provision_hardware_before_telemetry(self):
        for path in ('playbooks/rust.yml', 'playbooks/telemetry.yml'):
            roles = self.tasks(path)[0]['roles']
            names = [role if isinstance(role, str) else role['role'] for role in roles]
            self.assertLess(names.index('pi_hardware'), names.index('rust_telemetry'))
        main = self.tasks('roles/pi_hardware/tasks/main.yml')
        includes = [task for task in main if 'ansible.builtin.include_tasks' in task]
        self.assertEqual([task['when'] for task in includes],
                         ['pi_hardware_detected.hailo | length > 0',
                          'pi_hardware_detected.cameras | length > 0'])

    def test_driver_preconditions_permissions_and_no_kernel_upgrade(self):
        tasks = self.tasks('roles/pi_hardware/tasks/hailo.yml')
        names = [task['name'] for task in tasks]
        self.assertLess(names.index('Install DKMS before vendor driver configuration'),
                        names.index('Install minimal Hailo-8 driver and matching runtime'))
        self.assertLess(names.index('Restrict accelerator access before vendor package loads the driver'),
                        names.index('Install minimal Hailo-8 driver and matching runtime'))
        driver = next(task for task in tasks if task['name'] == 'Install minimal Hailo-8 driver and matching runtime')
        self.assertEqual(driver['ansible.builtin.apt']['name'],
                         ['hailort-pcie-driver', 'hailort', 'python3-hailort'])
        self.assertTrue(driver['ansible.builtin.apt']['fail_on_autoremove'])
        text = (ROOT / 'roles/pi_hardware/tasks/hailo.yml').read_text()
        self.assertNotIn('ansible.builtin.reboot', text)
        self.assertNotIn('linux-image', text)
        self.assertNotIn('upgrade:', text)
        self.assertIn("linux-headers-{{ pi_hardware_kernel.stdout | trim }}", text)
        self.assertIn('MODE="0660"', text)
        self.assertNotIn('MODE="0666"', text)

    def test_camera_provisioning_enumerates_but_never_captures(self):
        tasks = self.tasks('roles/pi_hardware/tasks/camera.yml')
        text = (ROOT / 'roles/pi_hardware/tasks/camera.yml').read_text()
        self.assertIn('Picamera2.global_camera_info()', text)
        self.assertNotIn('capture_array', text)
        self.assertNotIn('Picamera2()', text)
        self.assertEqual(tasks[0]['ansible.builtin.apt']['name'],
                         ['python3-picamera2', 'rpicam-apps-lite', 'wlopm'])

    def test_presence_only_still_creates_autostart_directory(self):
        tasks = self.tasks('roles/rust_telemetry/tasks/desktop.yml')
        directories = next(task for task in tasks if task['name'] == 'Create desktop startup directories')
        self.assertIn('rust_telemetry_screen_blank | bool', directories['when'])
        startup = next(task for task in tasks if task['name'] == 'Install presence startup entry')
        self.assertEqual(startup['when'], 'rust_telemetry_screen_blank | bool')
