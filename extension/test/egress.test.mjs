import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const E = require('../lib/egress.js');

function runPac(pac, url, host) {
  const ctx = { dnsDomainIs: (h, d) => h.endsWith(d) };
  vm.runInNewContext(pac, ctx);
  return ctx.FindProxyForURL(url, host);
}

test('meta hosts go through the egress, never DIRECT', () => {
  const pac = E.pacFor({ kind: 'socks5', host: '100.64.0.11', port: 1080 });
  for (const h of ['www.instagram.com', 'i.instagram.com', 'instagram.com', 'scontent-ams2-1.cdninstagram.com', 'static.xx.fbcdn.net'])
    assert.equal(runPac(pac, 'https://' + h + '/', h), 'SOCKS5 100.64.0.11:1080', h);
  assert.equal(runPac(pac, 'http://127.0.0.1:8777/api/ext/next', '127.0.0.1'), 'DIRECT');
  assert.equal(runPac(pac, 'https://notinstagram.com/', 'notinstagram.com'), 'DIRECT');
  assert.ok(!/SOCKS5[^"]*;\s*DIRECT/.test(pac), 'no DIRECT fallback after the proxy');
});

test('invalid egress refused', () => {
  assert.throws(() => E.pacFor({ kind: 'socks5', host: 'x"; return "DIRECT', port: 1 }));
  assert.throws(() => E.pacFor({ kind: 'http', host: 'h', port: 1 }));
  assert.throws(() => E.pacFor({ kind: 'socks5', host: 'h', port: 0 }));
  const s = E.proxySettings({ kind: 'socks5', host: 'h', port: 1 });
  assert.equal(s.value.pacScript.mandatory, true);
});

test('home detection: exact v4, same v6 /64, unknown is unsafe', () => {
  const homes = ['203.0.113.7', '2001:db8:aa:bb::1'];
  assert.equal(E.isHome('203.0.113.7', homes), true);
  assert.equal(E.isHome('203.0.113.8', homes), false);
  assert.equal(E.isHome('2001:db8:aa:bb:1234::9', homes), true);
  assert.equal(E.isHome('2001:db8:aa:bc::9', homes), false);
  assert.equal(E.isHome('', homes), true);
});
