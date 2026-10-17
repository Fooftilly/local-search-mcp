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
