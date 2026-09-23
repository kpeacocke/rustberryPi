#!/usr/bin/env python3
"""Require an A2S_INFO response, including the optional challenge exchange."""
import socket
import sys
import time


def query(host='127.0.0.1', port=28017):
    request = b'\xff\xff\xff\xffTSource Engine Query\x00'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.connect((host, port))
        sock.send(request)
        response = sock.recv(65535)
        if response[:5] == b'\xff\xff\xff\xffA' and len(response) == 9:
            sock.send(request + response[5:])
            response = sock.recv(65535)
        return response[:5] == b'\xff\xff\xff\xffI' and len(response) > 10


if __name__ == '__main__':
    deadline = time.monotonic() + int(sys.argv[1] if len(sys.argv) > 1 else 1800)
    while time.monotonic() < deadline:
        try:
            if query():
                print('Rust responds to A2S_INFO on UDP 28017')
                sys.exit(0)
        except OSError:
            pass
        time.sleep(5)
    sys.exit('Rust readiness timed out; inspect journalctl -u rust.service')
