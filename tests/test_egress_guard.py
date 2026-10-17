"""Network guard tests do not modify the host firewall."""
import argparse
import importlib.util
import errno
import json
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
    container={'Id':'c'*64,'State':{'Running':True},'HostConfig':{'Privileged':privileged,'CapAdd':cap or [], 'PortBindings': bindings if bindings is not None else {'11235/tcp':[{'HostIp':'127.0.0.1','HostPort':'11235'}]}},
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
                      'ct state established,related accept'):
            self.assertIn(value, rules)
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