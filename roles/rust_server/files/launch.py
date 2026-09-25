#!/usr/bin/env python3
"""Supply managed RCON settings before Rust initializes its networking."""
import os
from pathlib import Path
import re
import sys


def startup_args(argv, data=Path('/srv/rust/data')):
    identity = argv[argv.index('+server.identity') + 1]
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', identity):
        raise ValueError('Invalid identity')
    config = data / identity / 'cfg/server.cfg'
    if not config.exists():
        return argv
    text = config.read_text()
    begin = '// BEGIN rustberryPi local telemetry'
    end = '// END rustberryPi local telemetry'
    if begin not in text:
        return argv
    block = text.split(begin, 1)[1].split(end, 1)
    if len(block) != 2:
        raise ValueError('Incomplete managed RCON configuration')
    passwords = re.findall(r'^rcon\.password "([a-f0-9]{64})"\s*$', block[0], re.MULTILINE)
    if len(passwords) != 1:
        raise ValueError('Invalid managed RCON credential')
    return argv + ['+rcon.ip', '127.0.0.1', '+rcon.port', '28016',
                   '+rcon.web', '1', '+rcon.password', passwords[0]]


if __name__ == '__main__':
    try:
        args = startup_args(sys.argv[1:])
    except Exception:
        # Never print arguments, config contents or credential-bearing exceptions.
        sys.exit('Cannot read managed Rust startup configuration')
    os.execv(args[0], args)
