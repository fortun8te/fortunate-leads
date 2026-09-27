"""Egress units: the only way any scale worker reaches the network.

Kinds
  tor     a Tor SOCKS port; `IsolateSOCKSAuth` makes every SOCKS username its own circuit, so a
          circuit swap is just a new random username (no control port needed). Logged-out only.
  socks5  a SOCKS5 proxy (a phone on mobile data, a friend's home box over Tailscale, a VM).
  http    an HTTP CONNECT proxy (same uses as socks5).
  bind    bind outgoing connections to a specific local source address on this machine, e.g. one
          IPv6 address out of an Oracle free-tier VM's /64. Only valid on a machine that is not at home.
There is deliberately no "direct" kind: a request without an egress cannot be built.

HomeGuard
  The home connection must never carry scraping traffic. The guard holds the home addresses
  (IPv4 exact, IPv6 by /64 prefix, since one household gets a whole /64) and every non-Tor egress
  must pass `verify()` (observed exit address through that egress != home) before it gets work.
  With no home address configured the guard refuses every non-Tor egress: fail closed.
"""
import ipaddress
import json
import os
import secrets
import threading
import time

KINDS = ('tor', 'socks5', 'http', 'bind')
IP_ECHO = 'https://api64.ipify.org/?format=json'   # not an Instagram host


class HomeIPError(RuntimeError):
    pass


class EgressPolicyError(RuntimeError):
    pass


class Egress:
    def __init__(self, id, kind, host=None, port=None, username=None, password=None, source_addr=None,
                 label=None, logged_in_ok=False, group=None, dedicated_to=None):
        if kind not in KINDS:
            raise EgressPolicyError('unknown egress kind %r (no direct egress exists)' % (kind,))
        if kind == 'bind' and not source_addr:
            raise EgressPolicyError('bind egress needs source_addr')
        if kind != 'bind' and not (host and port):
            raise EgressPolicyError('%s egress needs host and port' % kind)
        if kind == 'tor' and logged_in_ok:
            # Logging in through Tor exits is a fast way to lose an account (shared, abused exits).
            raise EgressPolicyError('Tor egress can never carry a logged-in session')
        self.id, self.kind, self.host, self.port = str(id), kind, host, int(port) if port else None
        self.username, self.password, self.source_addr = username, password, source_addr
        self.label, self.logged_in_ok, self.group = label or str(id), bool(logged_in_ok), group
        self.dedicated_to = dedicated_to
        self.exit_ip = None
        self.verified_at = None
        self.circuit = 0
        if kind == 'tor' and not username:
            self.rotate()

    def rotate(self):
        """New Tor circuit (IsolateSOCKSAuth). Other kinds cannot rotate: returns False."""
        if self.kind != 'tor':
            return False
        self.username = 'fl-%s-%s' % (self.id, secrets.token_hex(6))
        self.password = 'x'
        self.circuit += 1
        self.exit_ip = None
        return True

    def describe(self):
        return {'id': self.id, 'kind': self.kind, 'label': self.label, 'group': self.group,
                'logged_in_ok': self.logged_in_ok, 'dedicated_to': self.dedicated_to,
                'exit_ip': self.exit_ip, 'verified_at': self.verified_at, 'circuit': self.circuit}

    @classmethod
    def from_dict(cls, d):
        keys = ('id', 'kind', 'host', 'port', 'username', 'password', 'source_addr', 'label',
                'logged_in_ok', 'group', 'dedicated_to')
        return cls(**{k: d[k] for k in keys if k in d})


def _norm(addr):
    return ipaddress.ip_address(str(addr).strip().split('%')[0])


class HomeGuard:
    def __init__(self, home_addresses=None):
        raw = home_addresses if home_addresses is not None else \
            [a for a in os.environ.get('FL_HOME_IPS', '').split(',') if a.strip()]
        self.v4, self.v6 = set(), set()
        for a in raw:
            a = a.strip()
            if not a:
                continue
            if '/' in a:
                net = ipaddress.ip_network(a, strict=False)
                (self.v6 if net.version == 6 else self.v4).add(net)
                continue
            ip = _norm(a)
            if ip.version == 6:
                self.v6.add(ipaddress.ip_network('%s/64' % ip, strict=False))
            else:
                self.v4.add(ipaddress.ip_network('%s/32' % ip))

    @property
    def configured(self):
        return bool(self.v4 or self.v6)

    def is_home(self, addr):
        try:
            ip = _norm(addr)
        except ValueError:
            return True   # unparseable: treat as unsafe
        nets = self.v6 if ip.version == 6 else self.v4
        return any(ip in n for n in nets)

    def check_exit(self, egress, exit_ip):
        if not exit_ip:
            raise HomeIPError('%s: exit address unknown; refusing (fail closed)' % egress.id)
        if self.is_home(exit_ip):
            raise HomeIPError('%s exits through the HOME address %s; refusing' % (egress.id, exit_ip))
        return True

    def check_bind(self, egress):
        if egress.kind == 'bind' and self.is_home(egress.source_addr):
            raise HomeIPError('%s binds to a home address; refusing' % egress.id)

    def verify(self, egress, transport, now=None):
        """Observe the exit address through `egress` (IP echo, not Instagram) and check it."""
        if egress.kind != 'tor' and not self.configured:
            raise HomeIPError('home address not configured (FL_HOME_IPS); non-Tor egress refused')
        self.check_bind(egress)
        r = transport.send(egress, 'GET', IP_ECHO, {'Accept': 'application/json'}, timeout=20)
        ip = None
        try:
            ip = json.loads(r.text).get('ip')
        except (ValueError, AttributeError):
            pass
        self.check_exit(egress, ip)
        egress.exit_ip, egress.verified_at = ip, (now or time.time())
        return ip


class Registry:
    """Egress inventory with a strict 1:1 binding of logged-in accounts to dedicated egresses."""

    def __init__(self, guard=None):
        self.guard = guard or HomeGuard()
        self.items = {}
        self.bound = {}          # account id -> egress id
        self.lock = threading.Lock()

    def add(self, egress):
        with self.lock:
            if egress.id in self.items:
                raise EgressPolicyError('duplicate egress id %s' % egress.id)
            self.guard.check_bind(egress)
            self.items[egress.id] = egress
        return egress

    def bind_account(self, account_id, egress_id):
        with self.lock:
            e = self.items.get(egress_id)
            if e is None:
                raise EgressPolicyError('unknown egress %s' % egress_id)
            if not e.logged_in_ok or e.kind == 'tor':
                raise EgressPolicyError('%s is not allowed to carry a logged-in session' % egress_id)
            owner = next((a for a, eid in self.bound.items() if eid == egress_id and a != account_id), None)
            if owner or (e.dedicated_to and e.dedicated_to != account_id):
                # Two accounts on one IP get linked by Instagram; one flag then hits both.
                raise EgressPolicyError('%s is already dedicated to %s' % (egress_id, owner or e.dedicated_to))
            self.bound[account_id] = egress_id
            e.dedicated_to = account_id
            return e

    def for_account(self, account_id):
        eid = self.bound.get(account_id)
        return self.items.get(eid) if eid else None

    def pool(self, kinds=('tor', 'socks5', 'http', 'bind')):
        """Egresses free for logged-out enrichment: never one dedicated to an account."""
        return [e for e in self.items.values() if e.kind in kinds and not e.dedicated_to]

    def ready_for_work(self, egress):
        """Tor exits are never the home address; everything else must have a verified exit."""
        if egress.kind == 'tor':
            return True
        return bool(egress.exit_ip and egress.verified_at and not self.guard.is_home(egress.exit_ip))
