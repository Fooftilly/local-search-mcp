"""Network guard tests do not modify the host firewall."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

P = Path(__file__).resolve().parents[1] / 'scripts/crawler_egress_guard.py'
spec = importlib.util.spec_from_file_location('crawler_egress_guard', P)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def fixture(networks=None, cap=None, privileged=False, members=True, driver='bridge'):
    networkid='a'*64
    container={'Id':'c'*64,'State':{'Running':True},'HostConfig':{'Privileged':privileged,'CapAdd':cap or []},
               'NetworkSettings':{'Networks':networks if networks is not None else {'crawler-egress':{'NetworkID':networkid}}}}
    network={'Id':networkid, 'Driver':driver, 'Internal':False, 'EnableIPv6':False, 'Options':{},
             'Containers':{('c'*64):{}} if members else {}}
    return [container], [network]

class GuardTests(unittest.TestCase):
    def test_rules_only_act_on_selected_bridge_and_block_ipv6(self):
        rules=guard.rules('br-aaaaaaaaaaaa')
        for value in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','169.254.0.0/16',
                      '100.64.0.0/10','127.0.0.0/8','meta nfproto ipv6',
                      'hook forward priority -200','hook input priority -200',
                      'ct state established,related accept'):
            self.assertIn(value, rules)
        self.assertNotIn('flush ruleset',rules)
        with self.assertRaises(guard.GuardError):guard.rules('bad"; flush ruleset')

    def test_known_container_on_isolated_network(self):
        c,n=fixture()
        with patch.object(guard,'command',side_effect=[json.dumps(c),json.dumps(n)]):
            self.assertEqual(guard.network_for('crawl4ai'),('crawler-egress','br-aaaaaaaaaaaa'))

    def test_refuses_default_bridge_and_extra_network(self):
        for name, networks in [('bridge',{'bridge':{'NetworkID':'a'*64}}),
                               ('crawler-egress',{'crawler-egress':{'NetworkID':'a'*64},'other':{}})]:
            c,n=fixture(networks=networks)
            with self.subTest(name=name),patch.object(guard,'command',side_effect=[json.dumps(c),json.dumps(n)]):
                with self.assertRaises(guard.GuardError):guard.network_for('crawl4ai')

    def test_refuses_privileged_admin_capability_and_peers(self):
        for opts in ({'privileged':True},{'cap':['NET_ADMIN']},{'members':False}):
            c,n=fixture(**opts)
            with self.subTest(opts=opts),patch.object(guard,'command',side_effect=[json.dumps(c),json.dumps(n)]):
                with self.assertRaises(guard.GuardError):guard.network_for('crawl4ai')

    def test_apply_requires_root_before_nft_writes(self):
        c,n=fixture()
        with patch.object(guard,'command',side_effect=[json.dumps(c),json.dumps(n)]) as cmd, patch.object(guard.os,'geteuid',return_value=1000):
            self.assertEqual(guard.main(['--container','crawl4ai','--apply']),1)
            self.assertEqual(cmd.call_count,2)

    def test_nft_table_discovery(self):
        found={'nftables':[{'metainfo':{}},{'table':{'family':'inet','name':guard.TABLE}}]}
        with patch.object(guard,'command',return_value=json.dumps(found)):
            self.assertTrue(guard.table_exists())

if __name__=='__main__': unittest.main()

class ProbeTests(unittest.TestCase):
    def test_no_side_effects_in_preview(self):
        c,n=fixture()
        with patch.object(guard,'command',side_effect=[json.dumps(c),json.dumps(n)]) as cmd:
            self.assertEqual(guard.main(['--container','crawl4ai','--print']),0)
            self.assertEqual(cmd.call_count,2)