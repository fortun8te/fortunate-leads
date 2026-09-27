"""Generic page readers never issue direct or redirected Meta requests."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qual_api
import deepscout
import meta_network


class MetaResearchBoundaryTest(unittest.TestCase):
    def test_exact_meta_domain_boundary(self):
        for host in ('instagram.com', 'WWW.INSTAGRAM.COM.', 'graph.facebook.com', 'a.fbcdn.net', 'threads.com'):
            self.assertTrue(meta_network.is_meta_host(host), host)
        for host in ('notinstagram.com', 'instagram.com.example.org', 'example.org'):
            self.assertFalse(meta_network.is_meta_host(host), host)

    def test_website_reader_rejects_direct_and_redirected_meta_before_dns(self):
        for host in ('instagram.com', 'm.facebook.com', 'threads.net', 'a.cdninstagram.com'):
            with patch.object(qual_api, '_public_address') as dns, patch.object(qual_api.socket, 'create_connection') as network:
                with self.assertRaisesRegex(ValueError, 'saved profile evidence'):
                    qual_api.fetch('https://' + host + '/alice/')
                with self.assertRaisesRegex(ValueError, 'saved profile evidence'):
                    qual_api._check('https://' + host + '/alice/', resolve=False)
                dns.assert_not_called()
                network.assert_not_called()

    def test_scout_rejects_matching_instagram_source_without_network(self):
        person = {'handle': 'alice', 'website': 'https://brand.example/'}
        with patch.object(deepscout, '_public_address') as dns, patch.object(deepscout.socket, 'create_connection') as network:
            with self.assertRaisesRegex(ValueError, 'saved profile evidence'):
                deepscout._fetch_cited_page('https://instagram.com/alice/', person)
            dns.assert_not_called()
            network.assert_not_called()

    def test_scout_checks_meta_redirect_destination(self):
        from types import SimpleNamespace
        response = SimpleNamespace(status=302, getheader=lambda name: 'https://instagram.com/alice/')
        connection = SimpleNamespace(request=lambda *a, **k: None, getresponse=lambda: response, close=lambda: None)
        sock = SimpleNamespace(settimeout=lambda *_: None)
        person = {'handle': 'alice', 'website': 'http://brand.example/'}
        with patch.object(deepscout, '_public_address', return_value='93.184.216.34') as dns, \
                patch.object(deepscout.socket, 'create_connection', return_value=sock) as network, \
                patch.object(deepscout.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'saved profile evidence'):
                deepscout._fetch_cited_page('http://brand.example/', person)
            self.assertEqual(dns.call_count, 1)
            self.assertEqual(network.call_count, 1)
