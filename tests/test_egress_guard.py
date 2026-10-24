"""Network guard tests do not modify the host firewall."""
import argparse
import importlib.util
import errno
import json
import ipaddress
from pathlib import Path
import unittest
from unittest.mock import patch

P = Path(__file__).resolve().parents[1] / 'scripts/crawler_egress_guard.py'
spec = importlib.util.spec_from_file_location('crawler_egress_guard', P)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def fixture(networks=None, cap=None, privileged=False, members=True, driver='bridge', bindings=None):
    """Build minimal safe Docker inspect responses for mock-based tests."""
    networkid='a'*64
    container={'Id':'c'*64,'State':{'Running':True},'HostConfig':{'Privileged':privileged,'CapAdd':cap or [], 'PortBindings': bindings if bindings is not None else {'11235/tcp':[{'HostIp':'127.0.0.1','HostPort':'11235'}]}, 'Dns':['1.1.1.1']},
               'NetworkSettings':{'Networks':networks if networks is not None else {'crawler-egress':{'NetworkID':networkid}}}}
    network={'Id':networkid, 'Driver':driver, 'Internal':False, 'EnableIPv6':False, 'Options':{},
             'Containers':{('c'*64):{}} if members else {}}
    return [container], [network]

def nft_listing(bridge: str) -> str:
    """Comment-free, stateless nft -s list table shape, not an input rules file.

    Keep this independent of guard.rules(): a generated template includes
    comments and does not represent the kernel's serialized listing.
    """
    networks = ', '.join(guard.BLOCKED_IPV4)
    return f'''table inet {guard.TABLE} {{
    counter blocked_ipv4 {{
    }}
    chain block_forward {{
        type filter hook forward priority -200; policy accept;
        iifname "{bridge}" ip daddr {{ {networks} }} counter name blocked_ipv4 reject
        iifname "{bridge}" meta nfproto ipv6 counter reject
    }}
    chain block_host {{
        type filter hook input priority -200; policy accept;
        iifname "{bridge}" ct state established ct direction reply accept
        iifname "{bridge}" counter reject
    }}
}}
'''


class GuardTests(unittest.TestCase):
    def test_rules_only_act_on_selected_bridge_and_block_ipv6(self):
        """Verify bridge scoping and deny rules for IPv4 and IPv6."""
        rules=guard.rules('br-aaaaaaaaaaaa')
        for value in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','169.254.0.0/16',
                      '100.64.0.0/10','127.0.0.0/8','meta nfproto ipv6',
                      'hook forward priority -200','hook input priority -200',
                      'ct state established ct direction reply accept'):
            self.assertIn(value, rules)
        self.assertNotIn('ct state established,related accept', rules)
        self.assertNotIn('flush ruleset',rules)
        with self.assertRaises(guard.GuardError):guard.rules('bad"; flush ruleset')

    def test_known_container_on_isolated_network(self):
        """Accept a single nonprivileged crawler on its own named bridge."""
        c,n=fixture()
        with patch.object(guard,'command',side_effect=[json.dumps(c),json.dumps(n)]):
            self.assertEqual(guard.network_for('crawl4ai'),('crawler-egress','br-aaaaaaaaaaaa'))

    def test_refuses_default_bridge_and_extra_network(self):
        """Reject default and multihomed Docker networking."""
        for name, networks in [('bridge',{'bridge':{'NetworkID':'a'*64}}),
                               ('crawler-egress',{'crawler-egress':{'NetworkID':'a'*64},'other':{}})]:
            c,n=fixture(networks=networks)
            with self.subTest(name=name),patch.object(guard,'command',side_effect=[json.dumps(c),json.dumps(n)]):
                with self.assertRaises(guard.GuardError):guard.network_for('crawl4ai')

    def test_refuses_privileged_admin_capability_and_peers(self):
        """Refuse elevated privileges and unexpected network peers."""
        for opts in ({'privileged':True},{'cap':['NET_ADMIN']},{'members':False}):
            c,n=fixture(**opts)
            with self.subTest(opts=opts),patch.object(guard,'command',side_effect=[json.dumps(c),json.dumps(n)]):
                with self.assertRaises(guard.GuardError):guard.network_for('crawl4ai')

    def test_stopped_crawler_preflight_reads_rules_without_exec(self):
        c, n = fixture(members=False)
        c[0]["State"] = {"Running": False, "Status": "exited"}
        c[0]["NetworkSettings"]["Networks"]["crawler-egress"]["NetworkID"] = ""
        with patch.object(guard, "command", side_effect=[
            json.dumps(c), json.dumps(n)]):
            self.assertEqual(guard.network_for("crawl4ai", allow_stopped=True),
                             ("crawler-egress", "br-aaaaaaaaaaaa"))
        with patch.object(guard, "command", side_effect=[
            json.dumps(c), json.dumps(n)]):
            with self.assertRaises(guard.GuardError):
                guard.network_for("crawl4ai")

    def test_preflight_fails_closed_without_any_docker_exec(self):
        expected = nft_listing("br-aaaaaaaaaaaa")
        with (patch.object(guard, "network_for",
                           return_value=("crawler-egress", "br-aaaaaaaaaaaa")) as net,
              patch.object(guard, "require_root"),
              patch.object(guard, "table_exists", return_value=True),
              patch.object(guard, "command", return_value=expected) as cmd):
            self.assertEqual(guard.main(["--preflight"]), 0)
        net.assert_called_once_with("crawl4ai", allow_stopped=True)
        self.assertEqual(cmd.call_args.args[0],
                         ["nft", "-s", "list", "table", "inet", guard.TABLE])
        for bad in ("", 'table inet ' + guard.TABLE + ' { chain forward {} }'):
            with (patch.object(guard, "network_for",
                               return_value=("crawler-egress", "br-aaaaaaaaaaaa")),
                  patch.object(guard, "require_root"),
                  patch.object(guard, "table_exists", return_value=True),
                  patch.object(guard, "command", return_value=bad)):
                self.assertEqual(guard.main(["--preflight"]), 1)

    def test_installed_rules_must_match_both_complete_guarded_chains(self):
        bridge = 'br-aaaaaaaaaaaa'
        expected = nft_listing(bridge)
        with (patch.object(guard, 'table_exists', return_value=True),
              patch.object(guard, 'command', return_value=expected) as cmd):
            guard.verify_installed(bridge)
        cmd.assert_called_once_with(['nft', '-s', 'list', 'table', 'inet', guard.TABLE])

        tampered = (
            # FORWARD uses stale bridge but INPUT mentions the correct bridge.
            expected.replace(
                f'iifname "{bridge}" ip daddr',
                'iifname "br-bbbbbbbbbbbb" ip daddr'),
            # A permissive rule before the denial would bypass the guard.
            expected.replace('chain block_forward {',
                             'chain block_forward {\n        accept'),
            expected.replace('192.168.0.0/16', '192.168.0.0/24'),
            expected.replace('hook forward priority -200',
                             'hook forward priority 0'),
            expected.replace(f'iifname "{bridge}" meta nfproto ipv6',
                             'meta nfproto ipv6'),
            expected.replace(f'iifname "{bridge}" counter reject',
                             'iifname "br-bbbbbbbbbbbb" counter reject'),
            # Dormant tables preserve rules but unregister their base chains.
            expected.replace(f'table inet {guard.TABLE} {{',
                             f'table inet {guard.TABLE} {{\n    flags dormant'),
        )
        for text in tampered:
            with (self.subTest(text=text[:80]),
                  patch.object(guard, 'table_exists', return_value=True),
                  patch.object(guard, 'command', return_value=text),
                  self.assertRaises(guard.GuardError)):
                guard.verify_installed(bridge)

    def test_apply_requires_root_before_nft_writes(self):
        """Never call nft to mutate rules without root privileges."""
        c,n=fixture()
        with patch.object(guard,'command',side_effect=[json.dumps(c),json.dumps(n)]) as cmd, patch.object(guard.os,'geteuid',return_value=1000):
            self.assertEqual(guard.main(['--container','crawl4ai','--apply']),1)
            self.assertEqual(cmd.call_count,2)

    def test_untrusted_container_input_is_rejected_before_subprocess(self):
        """Reject option-shaped and malformed Docker identifiers."""
        invalid = ('--privileged', '-x', '../crawl4ai', 'a/b', 'a\\n--help',
                   '$(whoami)', '', '.')
        for candidate in invalid:
            with self.subTest(container=candidate):
                with patch.object(guard, 'command') as cmd:
                    with self.assertRaises((guard.GuardError, ValueError, argparse.ArgumentTypeError)):
                        guard.network_for(candidate)
                    cmd.assert_not_called()
        self.assertEqual(guard.validated_container('crawl4ai-v1.0'), 'crawl4ai-v1.0')

    def test_probe_requires_specific_firewall_rejection_not_any_outage(self):
        """Reject false positives from TCP timeout or missing route."""
        guard.rejected_connection(str(errno.ECONNREFUSED))
        for candidate in ('0', str(errno.ETIMEDOUT), str(errno.EHOSTUNREACH), 'oops'):
            with self.subTest(value=candidate):
                with self.assertRaises(guard.GuardError):
                    guard.rejected_connection(candidate)

    def test_command_runner_never_accepts_other_executables(self):
        """Limit the subprocess runner to Docker and nft."""
        with patch.object(guard.subprocess, 'run') as proc:
            with self.assertRaises(guard.GuardError):
                guard.command(['sh', '-c', 'id'])
            proc.assert_not_called()

    def test_rejects_exposed_docker_ports_before_firewall_install(self):
        """Refuse a public Crawl4AI API even on an isolated bridge."""
        unsafe = [
            {'11235/tcp':[{'HostIp':'0.0.0.0','HostPort':'11235'}]},
            {'11235/tcp':[{'HostIp':'192.168.1.8','HostPort':'11235'}]},
            {'11235/tcp':[{'HostIp':'127.0.0.1','HostPort':'11235'}],
             '80/tcp':[{'HostIp':'127.0.0.1','HostPort':'8080'}]},
            {},
        ]
        for bindings in unsafe:
            c,n=fixture(bindings=bindings)
            with self.subTest(bindings=bindings),patch.object(guard,'command',return_value=json.dumps(c)) as cmd:
                with self.assertRaises(guard.GuardError):
                    guard.network_for('crawl4ai')
                self.assertEqual(cmd.call_count,1)

    def test_named_counter_validates_kernel_data(self):
        """Use only the expected inet nft counter for forward-probe evidence."""
        payload={'nftables':[{'metainfo':{}},
                {'counter':{'family':'inet','table':guard.TABLE,'name':'blocked_ipv4','packets':4,'bytes':160}}]}
        with patch.object(guard,'command',return_value=json.dumps(payload)):
            self.assertEqual(guard.counter_packets(),4)
        for invalid in ({'nftables':[]},
                        {'nftables':[{'counter':{'table':guard.TABLE,'name':'blocked_ipv4','packets':'4'}}]}):
            with self.subTest(invalid=invalid),patch.object(guard,'command',return_value=json.dumps(invalid)):
                with self.assertRaises(guard.GuardError):
                    guard.counter_packets()

    def test_requires_explicit_public_dns(self):
        """Reject absent, host, LAN and IPv6-only upstream resolvers."""
        for dns in ([], None, ['192.168.1.1'], ['127.0.0.53'], ['::1'], ['1.1.1.1', '10.0.0.1']):
            with self.subTest(dns=dns), self.assertRaises(guard.GuardError):
                guard.validate_public_dns({'Dns':dns})
        guard.validate_public_dns({'Dns':['1.1.1.1','9.9.9.9']})

    def test_probe_categories_stay_mapped_to_intended_firewall_ranges(self):
        """Catch denylist reordering that could silently invalidate category coverage."""
        expected = {
            'RFC1918 10/8': '10.0.0.0/8',
            'RFC1918 172.16/12': '172.16.0.0/12',
            'RFC1918 192.168/16': '192.168.0.0/16',
            'metadata/link-local': '169.254.0.0/16',
            'carrier NAT': '100.64.0.0/10',
            'documentation': '203.0.113.0/24',
        }
        self.assertEqual(len(guard.PROBE_RANGES), len(expected))
        for label, index in guard.PROBE_RANGES:
            with self.subTest(label=label):
                self.assertEqual(guard.BLOCKED_IPV4[index], expected[label])

    def test_probes_avoid_docker_on_link_addresses(self):
        """An overlapping bridge subnet must not masquerade as a firewall rejection."""
        for subnet in ('172.31.0.0/16', '192.168.240.0/20', '10.254.0.0/16'):
            with self.subTest(subnet=subnet):
                network = ipaddress.ip_network(subnet)
                targets = guard.probe_targets(network)
                self.assertEqual(len(targets), len(guard.PROBE_RANGES))
                self.assertTrue(all(ipaddress.ip_address(target) not in network for target in targets))
        with self.assertRaises(guard.GuardError):
            guard.probe_targets(ipaddress.ip_network('10.0.0.0/8'))

    def test_missing_binary_is_reported_without_traceback(self):
        """Missing Docker or nftables raises a controlled command-line failure."""
        with patch.object(guard, 'network_for', side_effect=FileNotFoundError('docker')):
            self.assertEqual(guard.main(['--print']), 1)

    def test_apply_batches_nft_syntax_validation_before_changes(self):
        """Applying a guard tests the full nft transaction and touches its table only."""
        for exists in (False, True):
            with self.subTest(exists=exists):
                with patch.object(guard, 'network_for', return_value=('crawler-egress','br-aaaaaaaaaaaa')), \
                     patch.object(guard, 'require_root'), \
                     patch.object(guard, 'table_exists', return_value=exists), \
                     patch.object(guard, 'command', return_value='') as cmd:
                    self.assertEqual(guard.main(['--apply']), 0)
                    self.assertEqual(cmd.call_count, 2)
                    checks = cmd.call_args_list
                    self.assertEqual(checks[0].args[0], ['nft', '-c', '-f', '-'])
                    self.assertEqual(checks[1].args[0], ['nft', '-f', '-'])
                    self.assertEqual(checks[0].kwargs['stdin'], checks[1].kwargs['stdin'])
                    payload = checks[0].kwargs['stdin']
                    self.assertIn('table inet local_search_mcp_crawler', payload)
                    self.assertNotIn('flush ruleset', payload)
                    self.assertEqual(payload.startswith('delete table inet'), exists)

    def test_bootstrap_fails_closed_when_stopped_firewall_cannot_be_verified(self):
        """A sleeping crawler permits nft installation, never an unproved pass."""
        ident = 'c' * 64
        for good in (True, False):
            with self.subTest(preflight=good):
                with (patch.object(guard, 'pin_container_id', return_value=ident) as pin,
                      patch.object(guard, 'network_for',
                                   return_value=('crawler-egress', 'br-aaaaaaaaaaaa')) as net,
                      patch.object(guard, 'require_root'),
                      patch.object(guard, 'table_exists', return_value=False),
                      patch.object(guard, 'command', return_value='') as cmd,
                      patch.object(guard, 'verify_installed',
                                   side_effect=None if good else guard.GuardError('no hook')) as verify,
                      patch.object(guard, 'container_running', return_value=False),
                      patch.object(guard, 'probe') as live):
                    status = guard.main(['--container', 'crawl4ai', '--startup'])
                pin.assert_called_once_with('crawl4ai')
                net.assert_called_once_with(ident, allow_stopped=True)
                self.assertEqual(status, 0 if good else 1)
                self.assertEqual(cmd.call_count, 2)
                self.assertEqual(cmd.call_args_list[0].args[0], ['nft', '-c', '-f', '-'])
                self.assertEqual(cmd.call_args_list[1].args[0], ['nft', '-f', '-'])
                verify.assert_called_once_with('br-aaaaaaaaaaaa')
                live.assert_not_called()

    def test_bootstrap_requires_live_probe_for_running_crawler(self):
        ident = 'c' * 64
        with (patch.object(guard, 'pin_container_id', return_value=ident),
              patch.object(guard, 'network_for',
                           return_value=('crawler-egress', 'br-aaaaaaaaaaaa')),
              patch.object(guard, 'require_root'),
              patch.object(guard, 'table_exists', return_value=False),
              patch.object(guard, 'command', return_value=''),
              patch.object(guard, 'verify_installed'),
              patch.object(guard, 'container_running', return_value=True) as running,
              patch.object(guard, 'probe', side_effect=guard.GuardError('unsafe')) as live):
            self.assertEqual(guard.main(['--startup']), 1)
        running.assert_called_once_with(ident)
        live.assert_called_once_with(ident, 'crawler-egress')

    def test_tunnel_gate_checks_existing_guard_before_state_and_live_probe(self):
        ident = 'c' * 64
        for running, healthy in ((False, True), (True, True), (True, False)):
            with self.subTest(running=running, healthy=healthy):
                events = []
                with (patch.object(guard, 'pin_container_id', return_value=ident) as pin,
                      patch.object(guard, 'network_for',
                                   return_value=('crawler-egress', 'br-aaaaaaaaaaaa')) as net,
                      patch.object(guard, 'require_root'),
                      patch.object(guard, 'verify_installed',
                                   side_effect=lambda bridge: events.append('firewall')) as verify,
                      patch.object(guard, 'container_running',
                                   side_effect=lambda target: events.append('state') or running) as state,
                      patch.object(guard, 'command',
                                   side_effect=AssertionError('No Docker exec or nft writes')),
                      patch.object(guard, 'probe',
                                   side_effect=lambda *args: (
                                       events.append('probe'),
                                       (_ for _ in ()).throw(guard.GuardError('live failed'))
                                       if not healthy else None)[1]) as live):
                    result = guard.main(['--tunnel-gate'])
                self.assertEqual(result, 0 if healthy else 1)
                pin.assert_called_once_with('crawl4ai')
                net.assert_called_once_with(ident, allow_stopped=True)
                verify.assert_called_once_with('br-aaaaaaaaaaaa')
                state.assert_called_once_with(ident)
                self.assertEqual(events, ['firewall', 'state'] + (['probe'] if running else []))
                if running:
                    live.assert_called_once_with(ident, 'crawler-egress')
                else:
                    live.assert_not_called()

    def test_tunnel_gate_fails_closed_for_missing_firewall_even_if_sleeping(self):
        with (patch.object(guard, 'pin_container_id', return_value='c' * 64),
              patch.object(guard, 'network_for',
                           return_value=('crawler-egress', 'br-aaaaaaaaaaaa')),
              patch.object(guard, 'require_root'),
              patch.object(guard, 'verify_installed',
                           side_effect=guard.GuardError('nft absent')),
              patch.object(guard, 'container_running') as state,
              patch.object(guard, 'probe') as live):
            self.assertEqual(guard.main(['--tunnel-gate']), 1)
        state.assert_not_called()
        live.assert_not_called()

    def test_pin_container_id_rejects_unverified_or_replaced_docker_identity(self):
        for result in ('c' * 64, 'c' * 12, '--privileged', '', 'C' * 64):
            with (self.subTest(result=result[:12]),
                  patch.object(guard, 'command', return_value=result)):
                if result == 'c' * 64:
                    self.assertEqual(guard.pin_container_id('crawl4ai'), result)
                else:
                    with self.assertRaises(guard.GuardError):
                        guard.pin_container_id('crawl4ai')
        with (patch.object(guard, 'pin_container_id',
                           side_effect=guard.GuardError('Docker unavailable')),
              patch.object(guard, 'network_for') as net):
            self.assertEqual(guard.main(['--tunnel-gate']), 1)
        net.assert_not_called()

    def test_live_probe_cannot_fall_back_to_stopped_preflight(self):
        with (patch.object(guard, 'network_for',
                           side_effect=guard.GuardError('Crawler must run')),
              patch.object(guard, 'require_root'),
              patch.object(guard, 'container_running') as state):
            self.assertEqual(guard.main(['--probe']), 1)
        state.assert_not_called()

    def test_forward_probe_requires_refusal_and_counter_increase(self):
        """A failed connection alone is not proof that the nft denylist blocked it."""
        from types import SimpleNamespace
        success = SimpleNamespace(returncode=0, stdout='CONNECT_RESULT ' + str(errno.ECONNREFUSED))
        with patch.object(guard, 'counter_packets', side_effect=[8, 9]), \
             patch.object(guard.subprocess, 'run', return_value=success) as run:
            guard.verify_forward_denials('crawl4ai', 'probe', ('blocked-probe',))
            self.assertEqual(run.call_count, 1)
        with patch.object(guard, 'counter_packets', side_effect=[8, 8]), \
             patch.object(guard.subprocess, 'run', return_value=success):
            with self.assertRaises(guard.GuardError):
                guard.verify_forward_denials('crawl4ai', 'probe', ('blocked-probe',))
        with patch.object(guard, 'counter_packets', return_value=8), \
             patch.object(guard.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='CONNECT_RESULT 0')):
            with self.assertRaises(guard.GuardError):
                guard.verify_forward_denials('crawl4ai', 'probe', ('blocked-probe',))

    def test_nft_table_discovery(self):
        """Recognize only the named inet firewall table."""
        found={'nftables':[{'metainfo':{}},{'table':{'family':'inet','name':guard.TABLE}}]}
        with patch.object(guard,'command',return_value=json.dumps(found)):
            self.assertTrue(guard.table_exists())

class ProbeTests(unittest.TestCase):
    def test_no_side_effects_in_preview(self):
        """Printing rules must not mutate the host firewall."""
        c,n=fixture()
        with patch.object(guard,'command',side_effect=[json.dumps(c),json.dumps(n)]) as cmd:
            self.assertEqual(guard.main(['--container','crawl4ai','--print']),0)
            self.assertEqual(cmd.call_count,2)


if __name__ == '__main__':
    unittest.main()
