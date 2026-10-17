#!/usr/bin/env python3
"""Operator-controlled host nftables containment for one Docker Crawl4AI bridge.

This tool never edits the firewall unless --apply or --remove is explicitly used.
It protects host-routed traffic, not loopback within the crawler container.
"""
from __future__ import annotations

import argparse
import errno
import ipaddress
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


# Categories are deliberately sampled outside the active Docker bridge subnet.
# The host firewall, not upstream route availability, must reject each test.
PROBE_CANDIDATES = (
    ('RFC1918 10/8', ('10.254.0.9', '10.127.254.9', '10.1.254.9')),
    ('RFC1918 172.16/12', ('172.31.254.9', '172.16.254.9', '172.20.254.9')),
    ('RFC1918 192.168/16', ('192.168.254.9', '192.168.1.9', '192.168.127.9')),
    ('metadata/link-local', ('169.254.169.254', '169.254.1.9')),
    ('carrier NAT', ('100.64.0.9', '100.127.0.9')),
    ('documentation', ('203.0.113.9', '198.51.100.9')),
)


class GuardError(RuntimeError):
    """Reject unsafe Docker topologies and inconclusive firewall diagnostics."""


def validated_container(value: str) -> str:
    """Accept Docker names/IDs, never flags or command/control characters."""
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise argparse.ArgumentTypeError('Container must be a Docker name or ID, not an option or path')
    if value in ('.', '..'):
        raise argparse.ArgumentTypeError('Invalid container name')
    return value


def command(argv: list[str], *, stdin: str | None = None) -> str:
    """Run a restricted Docker/nft subprocess without shell interpretation."""
    if not argv or argv[0] not in ('docker', 'nft'):
        raise GuardError('Only Docker and nft commands may be executed')
    p = subprocess.run(argv, input=stdin, text=True, capture_output=True, check=False, timeout=12, shell=False)
    if p.returncode:
        # Avoid dumping Docker environment variables or other sensitive output.
        raise GuardError(f'{argv[0]} {argv[1]} failed (exit {p.returncode}): {p.stderr.strip()[:250]}')
    return p.stdout


def validate_api_bindings(host: dict) -> None:
    """Refuse LAN-facing API publication and unexpected exposed host ports.

    The bridge and tunnel are loopback consumers; a public Docker port mapping
    would expose the crawler API and bypass these frontend protections.
    """
    expected = {'11235/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '11235'}]}
    if host.get('PublishAllPorts') or host.get('PortBindings') != expected:
        raise GuardError('Crawl4AI must publish only 127.0.0.1:11235 -> 11235/tcp; '
                         'refusing wildcard/LAN/extra port bindings')


def validate_public_dns(host: dict) -> None:
    """Require explicit public IPv4 DNS so the LAN-blocking firewall cannot silently break lookups."""
    configured = host.get('Dns')
    if not isinstance(configured, list) or not configured:
        raise GuardError('Crawl4AI must configure public IPv4 DNS explicitly (--dns 1.1.1.1 or Compose dns:); '
                         'Docker host-derived LAN DNS is blocked by this guard')
    for value in configured:
        try:
            addr = ipaddress.ip_address(value)
        except (ValueError, TypeError) as exc:
            raise GuardError('Crawl4AI DNS entries must be public IPv4 addresses') from exc
        if not isinstance(addr, ipaddress.IPv4Address) or not addr.is_global:
            raise GuardError('Crawl4AI DNS entries must be public IPv4 addresses; LAN, host and IPv6 DNS cannot work with this guard')


def probe_targets(subnet: ipaddress.IPv4Network) -> tuple[str, ...]:
    """Pick one off-link deny target per block category, or refuse an inconclusive probe."""
    selected = []
    for category, candidates in PROBE_CANDIDATES:
        target = next((t for t in candidates if ipaddress.ip_address(t) not in subnet), None)
        if target is None:
            raise GuardError(f'Cannot test {category}: every candidate overlaps Docker bridge subnet {subnet}')
        selected.append(target)
    return tuple(selected)


def network_for(container: str) -> tuple[str, str]:
    """Inspect the running crawler and require a dedicated locked-down bridge."""
    container = validated_container(container)
    raw = json.loads(command(['docker', 'inspect', '--type=container', container]))
    if len(raw) != 1:
        raise GuardError('Expected exactly one Crawl4AI container')
    c = raw[0]
    if not c.get('State', {}).get('Running'):
        raise GuardError('Crawl4AI container must be running')
    host = c.get('HostConfig', {})
    if host.get('Privileged') or any(cap.upper() in {'NET_ADMIN', 'SYS_ADMIN', 'SYS_MODULE', 'BPF'} for cap in (host.get('CapAdd') or [])):
        raise GuardError('Refusing privileged or elevated container: crawler could modify firewall or networking')
    validate_api_bindings(host)
    validate_public_dns(host)
    names = c.get('NetworkSettings', {}).get('Networks', {})
    if len(names) != 1:
        raise GuardError('Crawler must be attached to exactly one dedicated Docker bridge network')
    name, detail = next(iter(names.items()))
    # Docker supplies this name, but it is still used as a CLI argument.
    validated_container(name)
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
    if not re.fullmatch(r'[a-fA-F0-9]{64}', c.get('Id', '')) or not re.fullmatch(r'[a-fA-F0-9]{64}', net.get('Id', '')):
        raise GuardError('Malformed Docker container/network identity')
    return name, bridge


def rules(bridge: str) -> str:
    """Render an nftables table scoped to the inspected Docker bridge."""
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', bridge):
        raise GuardError('Invalid bridge name')
    cidrs = ', '.join(BLOCKED_IPV4)
    # iifname rather than interface index survives interface recreation only
    # while the name remains stable. Recheck after Docker/network recreation.
    return f'''table inet {TABLE} {{
    counter blocked_ipv4 {{
    }}
    chain block_forward {{
        type filter hook forward priority -200; policy accept;
        iifname "{bridge}" ip daddr {{ {cidrs} }} counter name blocked_ipv4 reject
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
    """Detect only the firewall table owned by this tool."""
    d = json.loads(command(['nft', '-j', 'list', 'tables']))
    return any(item.get('table', {}).get('family') == 'inet' and
               item.get('table', {}).get('name') == TABLE for item in d.get('nftables', []))


def counter_packets(name: str = 'blocked_ipv4') -> int:
    """Read the kernel's named reject counter, failing closed on malformed data."""
    if name != 'blocked_ipv4':
        raise GuardError('Unknown firewall counter')
    raw = json.loads(command(['nft', '-j', 'list', 'counter', 'inet', TABLE, name]))
    matches = [item['counter'] for item in raw.get('nftables', [])
               if isinstance(item, dict) and isinstance(item.get('counter'), dict)
               and item['counter'].get('name') == name
               and item['counter'].get('table') == TABLE]
    if len(matches) != 1 or type(matches[0].get('packets')) is not int:
        raise GuardError('Cannot verify installed IPv4 rejection counter')
    return matches[0]['packets']


def rejected_connection(code_text: str) -> None:
    """A firewall reject should be ECONNREFUSED, not an arbitrary outage."""
    if not code_text.isdigit():
        raise GuardError('Host-gateway probe returned an invalid response')
    result = int(code_text)
    if result != errno.ECONNREFUSED:
        raise GuardError(f'Host-gateway probe inconclusive: expected ECONNREFUSED, got errno {result}')


def probe(container: str, network: str) -> None:
    """Prove host-gateway TCP denial with an actually listening test endpoint.

    A public HTTPS TCP connection should work; otherwise the deployment is not
    verified even if the firewall rejects the host-gateway probe.
    """
    container = validated_container(container)
    network = validated_container(network)
    raw = json.loads(command(['docker', 'network', 'inspect', network]))
    configs = raw[0].get('IPAM', {}).get('Config', [])
    if len(configs) != 1 or not configs[0].get('Gateway') or not configs[0].get('Subnet'):
        raise GuardError('Expected one Docker bridge IPv4 gateway and subnet for the probe')
    gateway = configs[0]['Gateway']
    try:
        subnet = ipaddress.ip_network(configs[0]['Subnet'], strict=True)
        gateway_addr = ipaddress.ip_address(gateway)
    except ValueError as exc:
        raise GuardError('Invalid Docker bridge gateway/subnet for the probe') from exc
    if not isinstance(subnet, ipaddress.IPv4Network) or gateway_addr not in subnet:
        raise GuardError('Docker bridge IPv4 gateway/subnet mismatch; refusing inconclusive probe')
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
        rejected_connection(code_text)
    # Verify representative packets hit the host FORWARD deny rule, not merely
    # some unrelated upstream timeout or missing route.
    targets = probe_targets(subnet)
    for target in targets:
        before = counter_packets()
        result = subprocess.run(['docker', 'exec', container, 'python', '-c', code, target, '80'],
                                capture_output=True, text=True, timeout=10, check=False)
        if result.returncode or not result.stdout.strip().startswith('CONNECT_RESULT '):
            raise GuardError(f'Forward-denial test could not run for {target}')
        rejected_connection(result.stdout.strip().split('CONNECT_RESULT ')[-1].strip())
        if counter_packets() <= before:
            raise GuardError(f'No kernel firewall deny counter increase for {target}; test inconclusive')
    # Require both DNS resolution and a real hostname HTTPS TCP connection.
    # Connecting to an IP literal would report a false PASS when Docker's
    # embedded resolver forwards to a blocked LAN router or Pi-hole.
    public = ("import ipaddress,socket; "
              "a=socket.getaddrinfo('example.com',443,socket.AF_INET,socket.SOCK_STREAM); "
              "assert a and all(ipaddress.ip_address(x[4][0]).is_global for x in a); "
              "s=socket.create_connection(a[0][4],timeout=8); s.close()")
    p = subprocess.run(['docker', 'exec', container, 'python', '-c', public],
                       capture_output=True, text=True, timeout=13, check=False)
    if p.returncode != 0:
        raise GuardError('Public hostname DNS or HTTPS failed; check Docker --dns with public IPv4 resolvers '
                         '(LAN/host upstream DNS is blocked) and public network access')
    print('PASS: host-gateway rejected; six forwarded IPv4 destinations rejected by nft; '
          'public DNS and HTTPS-by-hostname succeeded')


def require_root() -> None:
    """Reserve firewall changes and probes for the host administrator."""
    if os.geteuid() != 0:
        raise GuardError('Firewall changes require root; review --print first and use sudo --apply/--remove')


def main(argv: list[str] | None = None) -> int:
    """Validate operator action, then preview, apply, inspect, probe or remove."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='crawl4ai', type=validated_container, help='Running Crawl4AI Docker container name or ID')
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
    except (GuardError, ValueError, KeyError, IndexError, OSError, subprocess.TimeoutExpired) as exc:
        print(f'Egress guard: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
