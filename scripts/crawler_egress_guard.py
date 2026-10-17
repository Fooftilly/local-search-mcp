#!/usr/bin/env python3
"""Operator-controlled host nftables containment for one Docker Crawl4AI bridge.

This tool never edits the firewall unless --apply or --remove is explicitly used.
It protects host-routed traffic, not loopback within the crawler container.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys

TABLE = 'local_search_mcp_crawler'
# Non-global IPv4 space, including carrier NAT, documentation/test networks,
# broadcast/multicast and future-use addresses. This is deliberately more
# restrictive than RFC1918 alone. All IPv6 forwarding is rejected.
BLOCKED_IPV4 = (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
    '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24',
    '192.0.2.0/24', '192.88.99.0/24', '192.168.0.0/16',
    '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24',
    '224.0.0.0/4', '240.0.0.0/4',
)


class GuardError(RuntimeError):
    pass


def command(argv: list[str], *, stdin: str | None = None) -> str:
    p = subprocess.run(argv, input=stdin, text=True, capture_output=True, check=False, timeout=12)
    if p.returncode:
        # Avoid dumping Docker environment variables or other sensitive output.
        raise GuardError(f'{argv[0]} {argv[1]} failed (exit {p.returncode}): {p.stderr.strip()[:250]}')
    return p.stdout


def network_for(container: str) -> tuple[str, str]:
    """Find a unique, private, single-container user-defined bridge."""
    raw = json.loads(command(['docker', 'inspect', '--type=container', container]))
    if len(raw) != 1:
        raise GuardError('Expected exactly one Crawl4AI container')
    c = raw[0]
    if not c.get('State', {}).get('Running'):
        raise GuardError('Crawl4AI container must be running')
    host = c.get('HostConfig', {})
    if host.get('Privileged') or any(cap.upper() == 'NET_ADMIN' for cap in (host.get('CapAdd') or [])):
        raise GuardError('Refusing privileged/NET_ADMIN container: crawler could modify firewall or networking')
    names = c.get('NetworkSettings', {}).get('Networks', {})
    if len(names) != 1:
        raise GuardError('Crawler must be attached to exactly one dedicated Docker bridge network')
    name, detail = next(iter(names.items()))
    raw_network = json.loads(command(['docker', 'network', 'inspect', name]))
    if len(raw_network) != 1:
        raise GuardError('Expected exactly one network')
    net = raw_network[0]
    if name == 'bridge' or net.get('Driver') != 'bridge' or net.get('Internal'):
        raise GuardError('Use a dedicated, non-internal, user-defined bridge (not Docker default bridge)')
    if net.get('EnableIPv6'):
        raise GuardError('Disable Docker-network IPv6 for this supported profile; IPv6 forwarding is blocked')
    members = net.get('Containers') or {}
    if len(members) != 1 or c['Id'] not in members:
        raise GuardError('Bridge must contain only this Crawl4AI container')
    opts = net.get('Options') or {}
    bridge = opts.get('com.docker.network.bridge.name') or ('br-' + net['Id'][:12])
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', bridge) or bridge in ('docker0', 'lo'):
        raise GuardError('Unexpected Docker bridge interface name')
    if detail.get('NetworkID') != net['Id']:
        raise GuardError('Container/network identity mismatch')
    return name, bridge


def rules(bridge: str) -> str:
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', bridge):
        raise GuardError('Invalid bridge name')
    cidrs = ', '.join(BLOCKED_IPV4)
    # iifname rather than interface index survives interface recreation only
    # while the name remains stable. Recheck after Docker/network recreation.
    return f'''table inet {TABLE} {{
    chain block_forward {{
        type filter hook forward priority -200; policy accept;
        iifname "{bridge}" ip daddr {{ {cidrs} }} counter reject
        iifname "{bridge}" meta nfproto ipv6 counter reject
    }}
    chain block_host {{
        type filter hook input priority -200; policy accept;
        iifname "{bridge}" ct state established,related accept
        iifname "{bridge}" counter reject
    }}
}}
'''


def table_exists() -> bool:
    d = json.loads(command(['nft', '-j', 'list', 'tables']))
    return any(item.get('table', {}).get('family') == 'inet' and
               item.get('table', {}).get('name') == TABLE for item in d.get('nftables', []))


def probe(container: str, network: str) -> None:
    """Prove host-gateway TCP denial with an actually listening test endpoint.

    A public HTTPS TCP connection should work; otherwise the deployment is not
    verified even if the firewall rejects the host-gateway probe.
    """
    raw = json.loads(command(['docker', 'network', 'inspect', network]))
    gateways = [x.get('Gateway') for x in raw[0].get('IPAM', {}).get('Config', []) if x.get('Gateway')]
    if len(gateways) != 1 or ':' in gateways[0]:
        raise GuardError('Expected exactly one Docker bridge IPv4 gateway for the probe')
    gateway = gateways[0]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((gateway, 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        # Verify test listener is genuinely reachable locally, to avoid false
        # positives from a missing endpoint or incorrect gateway.
        with socket.create_connection((gateway, port), timeout=2):
            pass
        code = ("import socket,sys; "
                "s=socket.socket(); s.settimeout(4); "
                "print('CONNECT_RESULT', s.connect_ex((sys.argv[1],int(sys.argv[2]))))")
        p = subprocess.run(['docker', 'exec', container, 'python', '-c', code, gateway, str(port)],
                           capture_output=True, text=True, timeout=10, check=False)
        if p.returncode != 0 or not p.stdout.strip().startswith('CONNECT_RESULT '):
            raise GuardError('Host-gateway probe could not execute correctly in Crawl4AI container')
        code_text = p.stdout.strip().split('CONNECT_RESULT ')[-1].strip()
        if not code_text.isdigit():
            raise GuardError('Host-gateway probe returned an invalid response')
        if int(code_text) == 0:
            raise GuardError('FAILED: crawler connected to host gateway despite expected isolation')
    # A separate positive probe avoids mistaking total network outage for
    # isolation, but cannot validate all CDN DNS, redirects or future routes.
    public = ("import socket,sys; "
              "s=socket.create_connection(('1.1.1.1',443),timeout=8); s.close()")
    p = subprocess.run(['docker', 'exec', container, 'python', '-c', public],
                       capture_output=True, text=True, timeout=13, check=False)
    if p.returncode != 0:
        raise GuardError('Public HTTPS connectivity failed; containment cannot be verified')
    print('PASS: live host-gateway TCP connection rejected; public HTTPS TCP connection succeeded')


def require_root() -> None:
    if os.geteuid() != 0:
        raise GuardError('Firewall changes require root; review --print first and use sudo --apply/--remove')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='crawl4ai', help='Running Crawl4AI Docker container name or ID')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--print', action='store_true', help='Preview nftables rules; makes no changes')
    modes.add_argument('--apply', action='store_true', help='Install isolated nftables table as root')
    modes.add_argument('--status', action='store_true', help='Display installed rules and check current bridge')
    modes.add_argument('--probe', action='store_true', help='Test live blocked host-gateway and permitted public TCP')
    modes.add_argument('--remove', action='store_true', help='Delete only this tool\'s nftables table')
    args = parser.parse_args(argv)
    try:
        if args.remove:
            require_root()
            if table_exists():
                command(['nft', 'delete', 'table', 'inet', TABLE])
            print('Removed local-search-mcp crawler firewall table')
            return 0
        name, bridge = network_for(args.container)
        generated = rules(bridge)
        if args.print:
            print(f'# Docker network: {name}; bridge: {bridge}\n{generated}')
            return 0
        if args.probe:
            require_root()
            if not table_exists():
                raise GuardError('Apply firewall before probing')
            live = command(['nft', 'list', 'table', 'inet', TABLE])
            if f'iifname "{bridge}"' not in live or 'hook forward' not in live or 'hook input' not in live:
                raise GuardError('Current bridge not covered by an installed guard')
            probe(args.container, name)
            return 0
        require_root()
        if args.apply:
            # nft -f processes the delete/create as one transaction. Never
            # modify Docker-managed tables, and never apply a partial ruleset.
            batch = (f'delete table inet {TABLE}\n' if table_exists() else '') + generated
            command(['nft', '-c', '-f', '-'], stdin=batch)
            command(['nft', '-f', '-'], stdin=batch)
            print(f'Installed {TABLE} for {name} on {bridge}; rules require reapplication after host reboot.')
            return 0
        if not table_exists():
            raise GuardError('Guard table absent; preview --print, then sudo --apply')
        live = command(['nft', 'list', 'table', 'inet', TABLE])
        # A human-readable diagnostic, not proof that all external access is safe.
        if f'iifname "{bridge}"' not in live or 'hook forward' not in live or 'hook input' not in live:
            raise GuardError('Installed guard does not match current Docker bridge or lacks a guard hook')
        print(f'Configured on {name} ({bridge}). Check actual rules and perform live negative probes:\n{live}')
        return 0
    except (GuardError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        print(f'Egress guard: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())