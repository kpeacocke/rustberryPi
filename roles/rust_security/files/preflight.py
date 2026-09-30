#!/usr/bin/env python3
"""Validate management allowlists and retain access from the current SSH peer."""
import ipaddress
import json
import sys


def validate(networks, ssh_connection, ssh_port):
    if not networks:
        raise ValueError('At least one management network is required')
    allowed = [ipaddress.ip_network(value, strict=True) for value in networks]
    if any(not network.is_private or network.prefixlen == 0 for network in allowed):
        raise ValueError('SSH management networks must be private, explicit subnets')
    if not 1 <= int(ssh_port) <= 65535:
        raise ValueError('Invalid SSH port')
    if ssh_connection:
        peer, _, _, server_port = ssh_connection.split()
        source = ipaddress.ip_address(peer)
        if not any(source in network for network in allowed):
            raise ValueError('Current SSH peer is outside the management allowlist; add its trusted subnet before enabling UFW')
        if int(server_port) != int(ssh_port):
            raise ValueError('Current SSH session uses a different server port; correct rust_ssh_port first')


if __name__ == '__main__':
    validate(json.loads(sys.argv[1]), sys.argv[2], sys.argv[3])
