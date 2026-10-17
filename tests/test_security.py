import unittest
from crawler_security import verify_health
from bootstrap import validate_domains, SetupError

class SafetyTests(unittest.TestCase):
    def test_minimum_crawler_version(self):
        self.assertIsNone(verify_health({'status':'ok','version':'0.9.4'}))
        self.assertIsNone(verify_health({'status':'ok','version':'0.10.0'}))
        self.assertIsNotNone(verify_health({'status':'ok','version':'0.9.3'}))
        self.assertIsNotNone(verify_health({'status':'ok'}))
        self.assertIsNotNone(verify_health({'status':'unhealthy','version':'0.9.4'}))

    def test_bootstrap_uses_same_public_suffix_rules(self):
        with self.assertRaises(SetupError):
            validate_domains('example.com,co.uk')
        with self.assertRaises(SetupError):
            validate_domains('github.io')
        self.assertEqual(validate_domains('example.com,example.com,docs.python.org'), 'example.com,docs.python.org')

if __name__=='__main__': unittest.main()
