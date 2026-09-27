"""Stdlib HTTPS over an Egress (SOCKS5 with remote DNS, HTTP CONNECT, or a bound source address).

No function here can open a connection without an Egress; there is no direct path.
Redirects are never followed: a 3xx comes back with `url` set to its Location so the
classifier can see login / challenge redirects.
"""
import gzip
import http.client
import ipaddress
import socket
import ssl
import struct
import time
import zlib
from urllib.parse import urljoin, urlsplit


class Response:
    def __init__(self, status, headers, text, url, elapsed=0.0):
        self.status, self.headers, self.text, self.url, self.elapsed = status, headers, text, url, elapsed

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)


class TransportError(OSError):
    pass


def _recv(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise TransportError('proxy closed the connection')
        buf += chunk
    return buf


def socks5_connect(proxy_host, proxy_port, host, port, username=None, password=None, timeout=20):
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        methods = b'\x00\x02' if username else b'\x00'
        s.sendall(b'\x05' + bytes([len(methods)]) + methods)
        ver, method = _recv(s, 2)
        if ver != 5 or method == 0xFF:
            raise TransportError('SOCKS5 proxy refused auth methods')
        if method == 2:
            u, p = (username or '').encode(), (password or '').encode()
            s.sendall(b'\x01' + bytes([len(u)]) + u + bytes([len(p)]) + p)
            if _recv(s, 2)[1] != 0:
                raise TransportError('SOCKS5 auth failed')
        h = host.encode('idna')
        s.sendall(b'\x05\x01\x00\x03' + bytes([len(h)]) + h + struct.pack('>H', port))  # remote DNS
        rep = _recv(s, 4)
        if rep[1] != 0:
            raise TransportError('SOCKS5 connect failed (code %d)' % rep[1])
        atyp = rep[3]
        _recv(s, 4 if atyp == 1 else 16 if atyp == 4 else _recv(s, 1)[0])
        _recv(s, 2)
        return s
    except Exception:
        s.close()
        raise


def http_connect(proxy_host, proxy_port, host, port, timeout=20):
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    s.sendall(('CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n' % (host, port, host, port)).encode())
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = s.recv(4096)
        if not chunk:
            s.close()
            raise TransportError('proxy closed during CONNECT')
        head += chunk
    if b' 200' not in head.split(b'\r\n', 1)[0]:
        s.close()
        raise TransportError('CONNECT refused: %r' % head[:80])
    return s


def bound_connect(source_addr, host, port, timeout=20):
    src = ipaddress.ip_address(source_addr)
    fam = socket.AF_INET6 if src.version == 6 else socket.AF_INET
    infos = socket.getaddrinfo(host, port, fam, socket.SOCK_STREAM)
    if not infos:
        raise TransportError('no %s address for %s' % ('IPv6' if fam == socket.AF_INET6 else 'IPv4', host))
    last = None
    for af, st, proto, _, addr in infos:
        s = socket.socket(af, st, proto)
        s.settimeout(timeout)
        try:
            s.bind((str(src), 0))
            s.connect(addr)
            return s
        except OSError as e:
            last = e
            s.close()
    raise TransportError('bound connect failed: %s' % last)


def open_socket(egress, host, port, timeout=20):
    if egress is None:
        raise TransportError('no egress: direct connections are not allowed')
    if egress.kind in ('tor', 'socks5'):
        return socks5_connect(egress.host, egress.port, host, port, egress.username, egress.password, timeout)
    if egress.kind == 'http':
        return http_connect(egress.host, egress.port, host, port, timeout)
    if egress.kind == 'bind':
        return bound_connect(egress.source_addr, host, port, timeout)
    raise TransportError('unsupported egress kind %s' % egress.kind)


class _Conn(http.client.HTTPSConnection):
    def __init__(self, host, raw, context, timeout):
        super().__init__(host, 443, timeout=timeout, context=context)
        self._raw = raw

    def connect(self):
        self.sock = self._context.wrap_socket(self._raw, server_hostname=self.host)


def _decode(data, enc):
    enc = (enc or '').lower()
    if enc == 'gzip':
        data = gzip.decompress(data)
    elif enc == 'deflate':
        data = zlib.decompress(data)
    return data.decode('utf-8', 'replace')


class Transport:
    def __init__(self, context=None):
        self.context = context or ssl.create_default_context()

    def send(self, egress, method, url, headers=None, body=None, timeout=20):
        u = urlsplit(url)
        if u.scheme != 'https':
            raise TransportError('https only')
        path = (u.path or '/') + ('?' + u.query if u.query else '')
        t0 = time.monotonic()
        raw = open_socket(egress, u.hostname, u.port or 443, timeout)
        conn = _Conn(u.hostname, raw, self.context, timeout)
        try:
            h = {'Accept-Encoding': 'gzip, deflate', 'Connection': 'close'}
            h.update(headers or {})
            conn.request(method, path, body=body, headers=h)
            r = conn.getresponse()
            data = r.read()
            hdrs = {k.lower(): v for k, v in r.getheaders()}
            text = _decode(data, hdrs.get('content-encoding'))
            loc = hdrs.get('location')
            final = urljoin(url, loc) if 300 <= r.status < 400 and loc else url
            return Response(r.status, hdrs, text, final, time.monotonic() - t0)
        finally:
            conn.close()
