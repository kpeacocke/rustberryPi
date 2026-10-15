#!/usr/bin/env python3
"""Discover bound camera sensors and Hailo PCI devices without opening either."""
import json
from pathlib import Path
import re


def detect(root=Path('/sys')):
    hailo = []
    for device in sorted((root / 'bus/pci/devices').glob('*')):
        if (device / 'vendor').read_text().strip().lower() == '0x1e60':
            hailo.append({'address': device.name, 'device': (device / 'device').read_text().strip().lower()})
    cameras = []
    for device in sorted((root / 'bus/i2c/devices').glob('*')):
        name = device / 'name'
        if name.exists() and (device / 'driver').exists():
            sensor = name.read_text().strip()
            if re.fullmatch(r'(?:imx|ov|ar)\d+[a-z0-9_-]*', sensor):
                cameras.append({'address': device.name, 'sensor': sensor})
    return {'hailo': hailo, 'cameras': cameras}


if __name__ == '__main__':
    print(json.dumps(detect()))
