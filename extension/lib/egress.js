// Per-account egress for the logged-in Chrome profiles (optional; wire-up in docs/scaling-plan.md).
//
// Today every Chrome profile on the Mac reaches Instagram from the HOME IP, so every account is
// linked to that IP (and to the main account using it), and one limit is treated as a workspace-wide
// wait. With this, each profile sends only Meta hosts through its own SOCKS5 egress (a phone on
// mobile data or a friend's box, reachable over Tailscale) and everything else stays normal.
//
// Fail closed: the PAC has no DIRECT fallback for Meta hosts. If the egress is down, Instagram
// requests fail (the extension backs off as for any network error) instead of silently leaking
// through the home connection.
(function (root) {
  const META = ['instagram.com', 'cdninstagram.com', 'fbcdn.net', 'facebook.com', 'fbsbx.com', 'facebook.net',
    'instagr.am', 'threads.net', 'threads.com'];
  const HOST_RX = /^[A-Za-z0-9.-]{1,253}$/;

  function validEgress(e) {
    return !!(e && typeof e === 'object' && e.kind === 'socks5' && HOST_RX.test(String(e.host || '')) &&
      Number.isInteger(e.port) && e.port > 0 && e.port < 65536);
  }

  // Returns a PAC script string. Only Meta hosts go through the egress; nothing Meta ever goes DIRECT.
  function pacFor(e) {
    if (!validEgress(e)) throw new Error('egress must be {kind:"socks5", host, port}');
    const proxy = 'SOCKS5 ' + e.host + ':' + e.port;
    return 'function FindProxyForURL(url, host) {\n' +
      '  var meta = ' + JSON.stringify(META) + ';\n' +
      '  host = host.toLowerCase();\n' +
      '  for (var i = 0; i < meta.length; i++) {\n' +
      '    if (host === meta[i] || dnsDomainIs(host, "." + meta[i])) return "' + proxy + '";\n' +
      '  }\n' +
      '  return "DIRECT";\n' +
      '}\n';
  }

  // chrome.proxy settings object for this profile (scope regular).
  function proxySettings(e) {
    return { value: { mode: 'pac_script', pacScript: { data: pacFor(e), mandatory: true } }, scope: 'regular' };
  }

  // The exit check the extension runs before its first Instagram request: the observed exit address
  // (fetched through the same egress from a non-Meta echo, e.g. api64.ipify.org routed via the proxy)
  // must not be the home address. homes: ["203.0.113.7", "2001:db8:1:2::/64", ...]
  function isHome(ip, homes) {
    ip = String(ip || '').trim().toLowerCase();
    if (!ip) return true;                     // unknown = unsafe
    return (homes || []).some((h) => {
      h = String(h).trim().toLowerCase();
      if (!h) return false;
      if (ip.includes(':')) {
        const pfx = (a) => expand6(a).slice(0, 4).join(':');
        return h.includes(':') && pfx(ip) === pfx(h.split('/')[0]);   // same /64 = same household
      }
      return ip === h.split('/')[0];
    });
  }
  function expand6(a) {
    const [l, r] = a.split('::');
    const L = l ? l.split(':') : [], R = r !== undefined && r ? r.split(':') : [];
    const mid = r !== undefined ? Array(8 - L.length - R.length).fill('0') : [];
    return L.concat(mid, R).map((x) => (x || '0').replace(/^0+(?=.)/, ''));
  }

  const api = { META, validEgress, pacFor, proxySettings, isHome };
  root.FLEgress = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
