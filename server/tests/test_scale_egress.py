import json
import socket
import struct
import threading
import unittest

from scale_helpers import FakeTransport, resp
from scale import transport as T
from scale.egress import Egress, EgressPolicyError, HomeGuard, HomeIPError, Registry

HOME4, HOME6 = '198.51.100.7', '2001:db8:aa:bb::5'


def echo(ip):
    return FakeTransport(lambda e, url: resp(200, {'ip': ip}, url=url))


class EgressPolicyTest(unittest.TestCase):
    def test_no_direct_kind_and_tor_never_logged_in(self):
        with self.assertRaises(EgressPolicyError):
            Egress('d', 'direct')
        with self.assertRaises(EgressPolicyError):
            Egress('t', 'tor', host='127.0.0.1', port=9050, logged_in_ok=True)

    def test_tor_rotation_changes_circuit_identity(self):
        e = Egress('t', 'tor', host='127.0.0.1', port=9050)
        u1 = e.username
        self.assertTrue(e.rotate())
        self.assertNotEqual(u1, e.username)
        self.assertFalse(Egress('s', 'socks5', host='h', port=1).rotate())

    def test_guard_fails_closed_without_home_config(self):
        g = HomeGuard([])
        with self.assertRaises(HomeIPError):
            g.verify(Egress('s', 'socks5', host='h', port=1), echo('203.0.113.9'))

    def test_guard_refuses_home_exit_v4_and_same_v6_64(self):
        g = HomeGuard([HOME4, HOME6])
        with self.assertRaises(HomeIPError):
            g.verify(Egress('s', 'socks5', host='h', port=1), echo(HOME4))
        with self.assertRaises(HomeIPError):   # another address in the home /64 is still home
            g.verify(Egress('s2', 'socks5', host='h', port=1), echo('2001:db8:aa:bb:ffff::1'))
        e = Egress('s3', 'socks5', host='h', port=1)
        self.assertEqual(g.verify(e, echo('203.0.113.9')), '203.0.113.9')
        self.assertEqual(e.exit_ip, '203.0.113.9')

    def test_guard_refuses_unknown_exit_and_home_bind(self):
        g = HomeGuard([HOME4])
        bad = FakeTransport(lambda e, url: resp(200, 'not json', url=url))
        with self.assertRaises(HomeIPError):
            g.verify(Egress('s', 'socks5', host='h', port=1), bad)
        reg = Registry(HomeGuard([HOME6]))
        with self.assertRaises(HomeIPError):
            reg.add(Egress('b', 'bind', source_addr='2001:db8:aa:bb::99'))

    def test_env_home_addresses(self):
        import os
        os.environ['FL_HOME_IPS'] = HOME4
        try:
            self.assertTrue(HomeGuard().is_home(HOME4))
        finally:
            del os.environ['FL_HOME_IPS']

    def test_registry_one_account_per_egress(self):
        reg = Registry(HomeGuard([HOME4]))
        reg.add(Egress('p1', 'socks5', host='h', port=1, logged_in_ok=True))
        reg.add(Egress('p2', 'socks5', host='h', port=2))
        reg.add(Egress('t1', 'tor', host='127.0.0.1', port=9050))
        reg.bind_account('bot1', 'p1')
        with self.assertRaises(EgressPolicyError):
            reg.bind_account('bot2', 'p1')          # shared IP would link the accounts
        with self.assertRaises(EgressPolicyError):
            reg.bind_account('bot2', 'p2')          # not marked for logged-in use
        with self.assertRaises(EgressPolicyError):
            reg.bind_account('bot2', 't1')          # never Tor for a session
        self.assertEqual([e.id for e in reg.pool()], ['p2', 't1'])   # account IPs never lent out
        self.assertFalse(reg.ready_for_work(reg.items['p2']))        # unverified
        self.assertTrue(reg.ready_for_work(reg.items['t1']))


class FakeSocks(threading.Thread):
    """Minimal SOCKS5 server: records the handshake, answers the CONNECT, then echoes a fixed reply."""

    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(('127.0.0.1', 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.seen = {}

    def run(self):
        c, _ = self.srv.accept()
        ver, n = c.recv(2)
        methods = c.recv(n)
        self.seen['methods'] = methods
        c.sendall(b'\x05\x02')
        c.recv(1)
        ul = c.recv(1)[0]
        self.seen['user'] = c.recv(ul).decode()
        pl = c.recv(1)[0]
        c.recv(pl)
        c.sendall(b'\x01\x00')
        head = c.recv(4)
        ln = c.recv(1)[0]
        self.seen['host'] = c.recv(ln).decode()
        self.seen['port'] = struct.unpack('>H', c.recv(2))[0]
        self.seen['atyp'] = head[3]
        c.sendall(b'\x05\x00\x00\x01' + b'\x00' * 6)
        c.sendall(b'hello')
        c.close()
        self.srv.close()


class TransportTest(unittest.TestCase):
    def test_no_egress_no_connection(self):
        with self.assertRaises(T.TransportError):
            T.open_socket(None, 'i.instagram.com', 443)

    def test_socks5_auth_and_remote_dns(self):
        fs = FakeSocks()
        fs.start()
        s = T.socks5_connect('127.0.0.1', fs.port, 'i.instagram.com', 443, 'fl-circuit-1', 'x', timeout=5)
        self.assertEqual(s.recv(5), b'hello')
        s.close()
        fs.join(5)
        self.assertEqual(fs.seen['user'], 'fl-circuit-1')
        self.assertEqual(fs.seen['host'], 'i.instagram.com')   # name sent to the proxy, never resolved locally
        self.assertEqual(fs.seen['atyp'], 3)
        self.assertEqual(fs.seen['port'], 443)


if __name__ == '__main__':
    unittest.main()
