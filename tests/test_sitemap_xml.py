"""Offline safety and semantics regression tests for bounded XML sitemap parser."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import sitemap_xml as sm

HOST = "www.example.org"
BASE = "https://www.example.org/sitemap.xml"
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def xmlset(items, *, ns=NS):
    return (f'<urlset {ns}>' + ''.join(f'<url><loc>{url}</loc></url>' for url in items)
            + '</urlset>').encode()


def index(items):
    return ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + ''.join(f'<sitemap><loc>{url}</loc></sitemap>' for url in items)
            + '</sitemapindex>').encode()


class ParserTests(unittest.TestCase):
    def parse(self, data, **kwargs):
        return sm.parse_sitemap_xml(data, source_url=BASE, scope_host=HOST, **kwargs)

    def test_urls_valid_namespace_and_no_namespace(self):
        for namespace in (NS, ''):
            with self.subTest(namespace=namespace):
                r=self.parse(xmlset(['https://www.example.org/a','https://www.example.org/b'],ns=namespace))
                self.assertEqual(r['entry_type'], 'page_url')
                self.assertEqual([v['url'] for v in r['urls']], [f'https://www.example.org/{c}' for c in 'ab'])
                self.assertFalse(r['fetch_permissions_verified'])
                self.assertEqual(r['source_completeness'],'unknown')

    def test_index_is_not_page_urls(self):
        r=self.parse(index(['https://www.example.org/maps/en.xml.gz']))
        self.assertEqual(r['entry_type'], 'sitemap_reference')
        self.assertEqual(r['source_type'], 'sitemap_index')
        self.assertEqual(len(r['urls']), 1)
        self.assertEqual(r['urls'][0]['source_method'], 'sitemap_index')

    def test_rejects_dtd_and_internal_entities(self):
        for data in (
            b'<!DOCTYPE urlset [<!ENTITY x "oops">]><urlset></urlset>',
            b'<!DOCTYPE urlset SYSTEM "file:///etc/passwd"><urlset></urlset>',
            b'<!DOCTYPE urlset><urlset></urlset>',
        ):
            with self.subTest(xml=data),self.assertRaises(sm.SitemapXMLRejected):self.parse(data)

    def test_oversized_binary_compressed_encoding_and_malformed(self):
        inputs=[b'', b'x'*(sm.MAX_DOCUMENT_BYTES+1),b'\x1f\x8bGZIP',b'PK\x03\x04Zip',
                b'\xff\xfe<\x00',b'\xfe\xff\x00<',b'<urlset><url>',
                b'<?xml version="1.0" encoding="ISO-8859-1"?><urlset/>',
                b'<urlset>\xff</urlset>',b'<not-a-sitemap/>',b'<urlset><sitemap/></urlset>']
        for data in inputs:
            with self.subTest(sample=data[:30]),self.assertRaises(sm.SitemapXMLRejected):self.parse(data)

    def test_utf8_xml_declaration_only(self):
        valid=(b'<?xml version="1.0" encoding="UTF-8"?>'
               + xmlset(['https://www.example.org/valid']))
        self.assertEqual(self.parse(valid)['discovered_in_scope'],1)
        for prolog in (b'<?xml version="1.0" encoding="utf-16"?>',
                       b'<?xml version="1.0" encoding=UTF-8?>',
                       b'<?xml version="1.0" encoding="UTF-8"'):
            with self.subTest(prolog=prolog),self.assertRaises(sm.SitemapXMLRejected):
                self.parse(prolog+xmlset([]))

    def test_mixed_invalid_urls_scope_and_duplicates(self):
        urls=['https://www.example.org/docs/a','https://www.example.org/docs/a',
              'https://other.example.org/docs/z', 'http://www.example.org/docs/b',
              'https://www.example.org/docs/../secret',
              'https://www.example.org/docs%2f..%2fsecret',
              'https://www.example.org/docs/../secret',
              'https://www.example.org/docs/ok#fragment',
              'https://www.example.org/docs','https://www.example.org/docs2',
              'https://u:p@www.example.org/docs/forbidden',
              'https://www.example.org:8080/docs/nope',
              '/docs/relative']
        r=self.parse(xmlset(urls),path_prefix='/docs')
        self.assertEqual(r['discovered_in_scope'],3)
        self.assertEqual(r['duplicate_entries'],1)
        self.assertEqual(r['rejected_entries'],len(urls)-4)
        self.assertEqual(r['entry_type'],'page_url')

    def test_truncates_output_without_claiming_complete_site(self):
        r=self.parse(xmlset([f'https://www.example.org/p{i}' for i in range(8)]),max_urls=3)
        self.assertEqual(len(r['urls']),3)
        self.assertEqual(r['discovered_in_scope'],8)
        self.assertTrue(r['truncated'])
        self.assertTrue(r['all_supplied_xml_parsed'])
        self.assertEqual(r['source_completeness'],'unknown')

    def test_nested_xml_and_extensions_do_not_inject_locations(self):
        data=(f'<urlset {NS} xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
              '<url><loc>https://www.example.org/yes</loc>'
              '<image:image><image:loc>https://www.example.org/no</image:loc></image:image>'
              '</url></urlset>').encode()
        r=self.parse(data)
        self.assertEqual(r['discovered_in_scope'],1)
        self.assertEqual(r['urls'][0]['url'],'https://www.example.org/yes')

    def test_mixed_sitemap_namespaces_and_encoded_dangerous_paths(self):
        root=(f'<urlset {NS}><url xmlns=""><loc>https://www.example.org/a</loc></url></urlset>').encode()
        with self.assertRaises(sm.SitemapXMLRejected): self.parse(root)
        wrong=(f'<urlset {NS}><url><loc xmlns="">https://www.example.org/a</loc></url></urlset>').encode()
        r=self.parse(wrong)
        self.assertEqual(r['urls'],[])
        self.assertEqual(r['rejected_entries'],1)
        for path in ('/docs/%252e%252e', '/docs/%00secret', '/docs/%0aok', '/docs/%7fcontrol',
                     '/docs/%23hash', '/docs/%3fquery'):
            with self.subTest(path=path):
                r=self.parse(xmlset(['https://www.example.org'+path]),path_prefix='/docs')
                self.assertEqual(r['urls'],[])
                self.assertEqual(r['rejected_entries'],1)

    def test_malformed_source_scope_and_invalid_args(self):
        for params in ({'source_url':'http://other.example.org/sitemap.xml'},
                       {'scope_host':'EXAMPLE.org'},{'path_prefix':'/docs/../secret'},
                       {'path_prefix':'/docs%2f'},{'max_urls':True},{'max_urls':101}):
            with self.subTest(params=params),self.assertRaises(ValueError):
                inputs={'source_url': BASE, 'scope_host': HOST, **params}
                sm.parse_sitemap_xml(xmlset([]),**inputs)

    def test_reject_overdepth_and_too_many_entries(self):
        deep=(f'<urlset {NS}>'+'<url>'+'<a>'*15+'<loc>https://www.example.org/a</loc>'+'</a>'*15+'</url></urlset>').encode()
        with self.assertRaises(sm.SitemapXMLRejected): self.parse(deep)
        # Each duplicate still counts toward the input scan cap.
        many=xmlset(['https://www.example.org/a']*(sm.MAX_SITEMAP_ENTRIES+1))
        with self.assertRaises(sm.SitemapXMLRejected): self.parse(many)

    def test_invalid_loc_does_not_use_descendant_text(self):
        data=(f'<urlset {NS}><url><loc><x>https://www.example.org/a</x></loc></url></urlset>').encode()
        r=self.parse(data)
        self.assertEqual(r['urls'],[])
        self.assertEqual(r['rejected_entries'],1)

    def test_escaped_xml_ampersand_in_query_is_preserved(self):
        data=xmlset(['https://www.example.org/p?a=1&amp;b=2'])
        r=self.parse(data)
        self.assertEqual(r['urls'][0]['url'],'https://www.example.org/p?a=1&b=2')

    def test_invalid_query_percent_empty_fragment_and_default_ports(self):
        urls=['https://www.example.org/p?a=%GZ',
              'https://www.example.org/p?a=%',
              'https://www.example.org/p?a=%0A',
              'https://www.example.org/p#',
              'https://www.example.org/p#frag',
              'https://www.example.org/p',
              'https://www.example.org:443/p',
              'http://www.example.org/a',
              'http://www.example.org:80/a']
        r=self.parse(xmlset(urls))
        self.assertEqual(r['discovered_in_scope'],2)
        self.assertEqual(r['duplicate_entries'],2)
        self.assertEqual(r['rejected_entries'],5)
        self.assertEqual([x['url'] for x in r['urls']],
                         ['https://www.example.org/p','http://www.example.org/a'])

    def test_long_xml_declaration_cannot_bypass_utf8_check(self):
        # A large declaration past the fixed prefix is rejected, not parsed
        # using a conflicting claimed encoding.
        long_decl=(b'<?xml version="1.0" '+b' '*400+
                   b'encoding="ISO-8859-1"?>')
        with self.assertRaises(sm.SitemapXMLRejected):
            self.parse(long_decl+xmlset(['https://www.example.org/a']))

    def test_aggregate_budget_and_cross_document_semantics(self):
        first=xmlset([f'https://www.example.org/p{i}' for i in range(70)])
        second=xmlset([f'https://www.example.org/p{i}' for i in range(70,140)])
        r=sm.parse_sitemap_documents([(BASE,first),('https://www.example.org/map2.xml',second)],
                                    scope_host=HOST,max_urls=100)
        self.assertEqual(r['documents_parsed'],2)
        self.assertEqual(len(r['urls']),100)
        self.assertEqual(r['unique_retained_candidates_observed'],140)
        self.assertTrue(r['truncated'])
        self.assertEqual(r['raw_xml_bytes_parsed'],len(first)+len(second))
        self.assertEqual(r['source_completeness'],'unknown')
        self.assertFalse(r['fetch_permissions_verified'])
        mixed=sm.parse_sitemap_documents([(BASE,index(['https://www.example.org/map2.xml'])),
                                           ('https://www.example.org/map2.xml',xmlset(['https://www.example.org/p']))],
                                          scope_host=HOST)
        self.assertEqual([v['entry_type'] for v in mixed['urls']],
                         ['sitemap_reference','page_url'])

    def test_aggregate_fails_closed_on_extra_budget_and_invalid_second_document(self):
        good=xmlset(['https://www.example.org/a'])
        with self.assertRaises(ValueError):
            sm.parse_sitemap_documents([(BASE,good)]*3,scope_host=HOST)
        with patch.object(sm,'MAX_TOTAL_SITEMAP_BYTES',len(good)*2-1):
            with self.assertRaises(sm.SitemapXMLRejected):
                sm.parse_sitemap_documents([(BASE,good),(BASE,good)],scope_host=HOST)
        with self.assertRaises(sm.SitemapXMLRejected):
            sm.parse_sitemap_documents([(BASE,good),(BASE,b'<!DOCTYPE urlset><urlset/>')],
                                        scope_host=HOST)

    def test_no_auto_network_or_io_side_effect(self):
        # Fail if code introduces a filesystem, socket, HTTP or process hop.
        with (patch('builtins.open',side_effect=AssertionError('filesystem access')),
              patch('os.open',side_effect=AssertionError('file descriptor access')),
              patch('socket.socket',side_effect=AssertionError('network access')),
              patch('urllib.request.urlopen',side_effect=AssertionError('HTTP access')),
              patch('subprocess.Popen',side_effect=AssertionError('process execution'))):
            r=self.parse(xmlset(['https://www.example.org/a']))
        self.assertTrue(r['success'])
        self.assertFalse(r['fetch_permissions_verified'])


if __name__=='__main__': unittest.main()
