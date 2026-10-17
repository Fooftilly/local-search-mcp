"""Mocks the upstream HTTP API to prove unsafe crawls never reach POST."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import httpx

class FakeFastMCP:
    def __init__(self, *_args, **_kwargs):pass
    def tool(self, **_kwargs):
        return lambda func: func

class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType('fastmcp')
        fake.FastMCP = FakeFastMCP
        path=Path(__file__).resolve().parents[1] / 'bridge.py'
        spec=importlib.util.spec_from_file_location('bridge_under_test',path)
        cls.module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'fastmcp':fake}),patch('crawler_security.local_container_warning',return_value=None):
            spec.loader.exec_module(cls.module)

    def test_no_crawl_when_backend_old(self):
        calls=[]
        def handler(request):
            calls.append(request.method)
            self.assertEqual(request.url.path,'/health')
            return httpx.Response(200,json={'status':'ok','version':'0.9.4'})
        orig_client=httpx.Client
        def client(**kwargs):return orig_client(transport=httpx.MockTransport(handler))
        with patch.object(self.module, '_check_public_target',return_value=None),patch.object(self.module.httpx,'Client',side_effect=client):
            result=self.module.web_fetch('https://example.com')
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(calls,['GET'])

    def test_safe_backend_crawls_and_parses_markdown(self):
        calls=[]
        def handler(request):
            calls.append(request.method)
            if request.method=='GET':return httpx.Response(200,json={'status':'ok','version':'0.9.5'})
            return httpx.Response(200,json={'results':[{'url':'https://example.com', 'success':True,'markdown':'Hello'}]})
        orig_client=httpx.Client
        def client(**kwargs):return orig_client(transport=httpx.MockTransport(handler))
        with patch.object(self.module, '_check_public_target',return_value=None),patch.object(self.module.httpx,'Client',side_effect=client):
            result=self.module.web_fetch('https://example.com')
        self.assertTrue(result['success'])
        self.assertEqual(result['markdown'],'Hello')
        self.assertEqual(calls,['GET','POST'])

    def test_unapproved_redirect_content_not_returned(self):
        def handler(request):
            if request.method=='GET':return httpx.Response(200,json={'status':'ok','version':'0.9.5'})
            # Crawl4AI keeps the requested URL in "url" and puts its final
            # navigation (including JS redirects) in "redirected_url".
            return httpx.Response(200,json={'results':[{'url':'https://example.com',
                'redirected_url':'https://elsewhere.net/secret','success':True,'markdown':'sensitive'}]})
        orig_client=httpx.Client
        def client(**kwargs):return orig_client(transport=httpx.MockTransport(handler))
        def check(url):return {'status':'approval_required'} if 'elsewhere.net' in url else None
        with patch.object(self.module,'_check_public_target',side_effect=check),patch.object(self.module.httpx,'Client',side_effect=client):
            result=self.module.web_fetch('https://example.com')
        self.assertEqual(result['status'],'blocked')
        self.assertNotIn('markdown',result)

    def test_approved_redirect_is_returned_as_final_url(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com", "redirected_url": "https://docs.python.org/page",
                "success": True, "markdown": "approved"
            }]})
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        checked = []
        def check(url):
            checked.append(url)
            return None
        with patch.object(self.module, "_check_public_target", side_effect=check), \
             patch.object(self.module.httpx, "Client", side_effect=client):
            result = self.module.web_fetch("https://example.com")
        self.assertEqual(result["url"], "https://docs.python.org/page")
        self.assertEqual(result["markdown"], "approved")
        self.assertEqual(checked, ["https://example.com", "https://docs.python.org/page"])

    def test_non_string_redirected_url_fails_closed(self):
        for value in ({"href": "https://example.com"}, ["https://example.com"], 42, ""):
            def handler(request):
                if request.method == "GET":
                    return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
                return httpx.Response(200, json={"results": [{
                    "url": "https://example.com", "redirected_url": value,
                    "success": True, "markdown": "do not expose"
                }]})
            original = httpx.Client
            def client(**kwargs):
                return original(transport=httpx.MockTransport(handler), trust_env=False)
            with self.subTest(redirected_url=value), \
                 patch.object(self.module, "_check_public_target", return_value=None), \
                 patch.object(self.module.httpx, "Client", side_effect=client):
                result = self.module.web_fetch("https://example.com")
                self.assertEqual(result["status"], "blocked")
                self.assertNotIn("markdown", result)

    def test_approved_nonredirected_page_remains_compatible(self):
        """Older Crawl4AI responses without redirected_url still work."""
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com", "success": True, "markdown": "normal"
            }]})
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(self.module, "_check_public_target", return_value=None), \
             patch.object(self.module.httpx, "Client", side_effect=client):
            result = self.module.web_fetch("https://example.com")
        self.assertTrue(result["success"])
        self.assertEqual(result["url"], "https://example.com")
        self.assertEqual(result["markdown"], "normal")

if __name__=='__main__':unittest.main()
