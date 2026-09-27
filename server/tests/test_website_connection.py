"""Website connections use vetted addresses, not a second hostname lookup."""
import os
import socket
import ssl
import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import MagicMock, patch

import qual_api


class Response(BytesIO):
    def __init__(self, body=b'<title>Store</title>', status=200, location=None, content_type=None):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        if location:
            self.headers['Location'] = location
        if content_type:
            self.headers['Content-Type'] = content_type

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


def address(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]


class WebsiteConnection(unittest.TestCase):
    def transport(self, responses):
        conn = MagicMock()
        conn.getresponse.side_effect = responses
        return conn

    def test_dns_rebinding_cannot_change_actual_connection(self):
        conn, raw, context = self.transport([Response()]), MagicMock(), MagicMock()
        context.wrap_socket.return_value = raw
        with patch.object(qual_api.socket, 'getaddrinfo', side_effect=[address('93.184.216.34'), address('127.0.0.1')]) as dns, \
             patch.object(qual_api.socket, 'create_connection', return_value=raw) as connect, \
             patch.object(qual_api.http.client, 'HTTPConnection', return_value=conn), \
             patch.object(qual_api.llm, '_ssl', return_value=context), \
             patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:1234'}):
            self.assertEqual(qual_api.fetch('https://shop.example/')[0], 'https://shop.example/')
        self.assertEqual(dns.call_count, 1)
        self.assertEqual(connect.call_args.args[0], ('93.184.216.34', 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname='shop.example')
        self.assertEqual(conn.request.call_args.kwargs['headers']['Host'], 'shop.example')

    def test_public_https_redirect_validates_and_pins_each_hop(self):
        conn, raw, context = self.transport([Response(status=302, location='https://store.example/products'), Response()]), MagicMock(), MagicMock()
        context.wrap_socket.return_value = raw
        with patch.object(qual_api.socket, 'getaddrinfo', side_effect=[address('93.184.216.34'), address('1.1.1.1')]) as dns, \
             patch.object(qual_api.socket, 'create_connection', return_value=raw) as connect, \
             patch.object(qual_api.http.client, 'HTTPConnection', return_value=conn), \
             patch.object(qual_api.llm, '_ssl', return_value=context):
            final, body = qual_api.fetch('https://shop.example/')
        self.assertEqual(final, 'https://store.example/products')
        self.assertIn('Store', body)
        self.assertEqual([c.args[0] for c in connect.call_args_list], [('93.184.216.34', 443), ('1.1.1.1', 443)])
        self.assertEqual([c.kwargs['server_hostname'] for c in context.wrap_socket.call_args_list], ['shop.example', 'store.example'])
        self.assertEqual(dns.call_count, 2)

    def test_redirect_to_private_or_meta_has_no_second_connection(self):
        for target in ('https://private.example/', 'https://instagram.com/account/'):
            conn, raw, context = self.transport([Response(status=302, location=target)]), MagicMock(), MagicMock()
            context.wrap_socket.return_value = raw
            with patch.object(qual_api.socket, 'getaddrinfo', side_effect=[address('93.184.216.34'), address('127.0.0.1')]), \
                 patch.object(qual_api.socket, 'create_connection', return_value=raw) as connect, \
                 patch.object(qual_api.http.client, 'HTTPConnection', return_value=conn), \
                 patch.object(qual_api.llm, '_ssl', return_value=context):
                with self.assertRaises(ValueError):
                    qual_api.fetch('https://shop.example/')
            self.assertEqual(connect.call_count, 1)

    def test_verified_tls_context_and_failed_certificate_never_fall_back(self):
        real = qual_api.llm._ssl()
        self.assertTrue(real.check_hostname)
        self.assertEqual(real.verify_mode, ssl.CERT_REQUIRED)
        raw, context = MagicMock(), MagicMock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError('wrong certificate')
        with patch.object(qual_api, '_public_address', return_value='93.184.216.34'), \
             patch.object(qual_api.socket, 'create_connection', return_value=raw) as connect, \
             patch.object(qual_api.llm, '_ssl', return_value=context):
            with self.assertRaises(ValueError):
                qual_api.fetch('https://shop.example/')
        self.assertEqual(connect.call_count, 1)
        raw.close.assert_called_once()

    def test_meta_charset_and_page_cap_preserved(self):
        body = '<meta charset="windows-1252"><title>Café</title>'.encode('windows-1252')
        for content, too_large in [(body, False), (b'x' * (qual_api.FETCH_CAP + 1), True)]:
            conn = self.transport([Response(content)])
            with patch.object(qual_api, '_public_address', return_value='93.184.216.34'), \
                 patch.object(qual_api.socket, 'create_connection', return_value=MagicMock()), \
                 patch.object(qual_api.http.client, 'HTTPConnection', return_value=conn):
                if too_large:
                    with self.assertRaises(ValueError):
                        qual_api.fetch('http://shop.example/')
                else:
                    self.assertIn('Café', qual_api.fetch('http://shop.example/')[1])
