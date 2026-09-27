import random
import unittest

from scale_helpers import Clock, FakeTransport, ListSink, profile_json, resp
from scale.dedupe import SeenStore
from scale.egress import Egress, HomeGuard, Registry
from scale.enrich import EnrichmentPool, parse_profile, parse_profile_html, profile_request
from scale.metrics import Metrics
from scale.pacing import Pacer


HTML = ('<meta property="og:title" content="lululemon (&#064;lululemon) &#x2022; Instagram photos and videos" />'
        '<meta property="og:description" content="6M Followers, 164 Following, 3,760 Posts - See Instagram photos" />'
        '<script>{"page_id":"profilePage_2134762","biography":"Movement, mindfulness \\u0026 more.",'
        '"is_private":false}</script>')


def fast_pacer():
    return Pacer(rate=60, min_rate=1, max_rate=60, jitter=0, rng=random.Random(0))


def handle_of(url):
    return url.rsplit('username=', 1)[1]


class ParseTest(unittest.TestCase):
    def test_contract_fields(self):
        p = parse_profile(profile_json('glossier', bio='Skin first.'))
        self.assertEqual(p['handle'], 'glossier')
        self.assertEqual(p['bio'], 'Skin first.')
        self.assertEqual((p['followers'], p['following'], p['posts']), (1234, 56, 7))
        self.assertEqual(p['website'], 'https://brand.com')
        self.assertTrue(p['is_business'])
        self.assertIsNone(parse_profile({'data': {}}))
        self.assertIsNone(parse_profile_html('<html>Log in to Instagram</html>', 'x'))

    def test_request_shape(self):
        url, h = profile_request('nike')
        self.assertTrue(url.startswith('https://i.instagram.com/api/v1/users/web_profile_info/'))
        self.assertEqual(h['X-IG-App-ID'], '567067343352427')
        self.assertIn('Android', h['User-Agent'])


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = SeenStore()
        self.sink = ListSink()
        self.metrics = Metrics()

    def pool(self, egresses, fn, registry=None, **kw):
        self.tr = FakeTransport(fn)
        return EnrichmentPool(self.store, egresses, self.tr, self.sink, metrics=self.metrics, registry=registry,
                              rng=random.Random(0), pacer_factory=fast_pacer, clock=self.clock, **kw)

    def test_ok_writes_sink_and_marks_done(self):
        self.store.add_discovered([{'handle': 'a'}, {'handle': 'b'}], now=self.clock())
        tor = Egress('t0', 'tor', host='127.0.0.1', port=9050)
        p = self.pool([tor], lambda e, url: resp(200, profile_json(handle_of(url))))
        self.assertEqual(p.step(tor), 'ok')
        self.clock.advance(2)
        self.assertEqual(p.step(tor), 'ok')
        self.assertEqual(sorted(x['handle'] for x in self.sink.rows), ['a', 'b'])
        self.assertEqual(self.store.stats()['done'], 2)
        # seen again in another list: never fetched twice
        self.store.add_discovered([{'handle': 'a'}], source='z/followers', now=self.clock())
        self.clock.advance(2)
        self.assertEqual(p.step(tor), 'idle')
        self.assertEqual(len(self.tr.log), 2)
        self.assertEqual(self.metrics.snapshot()['pipeline']['enrich']['profiles']['total'], 2)

    def test_tor_pushback_swaps_circuit_and_requeues(self):
        self.store.add_discovered([{'handle': 'a'}], now=self.clock())
        tor = Egress('t0', 'tor', host='127.0.0.1', port=9050)
        answers = [resp(429, ''), resp(200, profile_json('a'))]
        p = self.pool([tor], lambda e, url: answers.pop(0))
        u1 = tor.username
        self.assertEqual(p.step(tor), 'swapped')
        self.assertNotEqual(tor.username, u1)
        self.clock.advance(10)
        p.buf = []
        self.assertEqual(p.step(tor), 'ok')
        self.assertEqual(self.tr.log[0][1], u1)
        self.assertNotEqual(self.tr.log[1][1], u1)   # retried on a different circuit

    def test_tor_saturation_cools_down(self):
        self.store.add_discovered([{'handle': 'h%d' % i} for i in range(20)], now=self.clock())
        tor = Egress('t0', 'tor', host='127.0.0.1', port=9050)
        p = self.pool([tor], lambda e, url: resp(401, '{"message":"Please wait a few minutes before you try again."}'),
                      swap_limit=3)
        outs = []
        for _ in range(3):
            outs.append(p.step(tor))
            self.clock.advance(10)
        self.assertEqual(outs, ['swapped', 'swapped', 'tor_saturated'])
        self.assertTrue(p.pacers['t0'].cooling(self.clock()))

    def test_fixed_egress_pushback_cools_with_retry_after(self):
        reg = Registry(HomeGuard(['198.51.100.7']))
        e = reg.add(Egress('v6', 'bind', source_addr='2603:c020::1'))
        e.exit_ip, e.verified_at = '2603:c020::1', self.clock()
        self.store.add_discovered([{'handle': 'a'}], now=self.clock())
        p = self.pool([e], lambda eg, url: resp(429, '', headers={'Retry-After': '7200'}), registry=reg)
        self.assertEqual(p.step(e), 'cooldown')
        self.assertEqual(p.pacers['v6'].cool_until, self.clock() + 7200)
        self.assertEqual(self.store.stats()['new'], 1)   # the profile goes back untouched

    def test_profile_errors_do_not_punish_egress(self):
        self.store.add_discovered([{'handle': 'gone'}, {'handle': 'weird'}], now=self.clock())
        tor = Egress('t0', 'tor', host='127.0.0.1', port=9050)
        answers = {'gone': resp(404, ''), 'weird': resp(500, 'oops')}
        p = self.pool([tor], lambda e, url: answers[handle_of(url)])
        outs = {p.step(tor)}
        self.clock.advance(2)
        outs.add(p.step(tor))
        self.assertEqual(outs, {'not_found', 'error'})
        self.assertFalse(p.pacers['t0'].cooling(self.clock()))
        self.assertEqual(self.store.stats().get('not_found'), 1)

    def test_null_user_is_not_found(self):
        self.store.add_discovered([{'handle': 'ghost'}], now=self.clock())
        tor = Egress('t0', 'tor', host='127.0.0.1', port=9050)
        p = self.pool([tor], lambda e, url: resp(200, {'data': {'user': None}, 'status': 'ok'}))
        self.assertEqual(p.step(tor), 'not_found')

    def test_empty_ok_goes_to_html_route(self):
        self.store.add_discovered([{'handle': 'siete'}], now=self.clock())
        tor = Egress('t0', 'tor', host='127.0.0.1', port=9050)
        p = self.pool([tor], lambda e, url: resp(200, {'status': 'ok'}))
        self.assertEqual(p.step(tor), 'to_html')
        self.assertIn('siete', p.html_route)

    def test_asset_bug_falls_back_to_html_route(self):
        self.store.add_discovered([{'handle': 'lululemon'}], now=self.clock())
        tor = Egress('t0', 'tor', host='127.0.0.1', port=9050)
        answers = [resp(400, '{"message":"Asset asset://laser.provider/ig_business_category_subvertical has been deleted"}'),
                   resp(302, '', url='https://www.instagram.com/accounts/login/?next=/lululemon/'),
                   resp(200, '<html>Log in</html>', url='https://www.instagram.com/lululemon/'),
                   resp(200, HTML, url='https://www.instagram.com/lululemon/')]
        p = self.pool([tor], lambda e, url: answers.pop(0))
        outs = []
        for _ in range(4):
            p.buf = []
            outs.append(p.step(tor))
            self.clock.advance(30)
        self.assertEqual(outs, ['to_html', 'swapped', 'html_wall', 'ok'])
        self.assertTrue(self.tr.log[1][2].startswith('https://www.instagram.com/lululemon/'))
        row = self.sink.rows[0]
        self.assertEqual((row['ig_id'], row['bio'], row['followers'], row['following']),
                         ('2134762', 'Movement, mindfulness & more.', 6000000, 164))
        self.assertEqual(row['name'], 'lululemon')

    def test_html_route_gives_up(self):
        self.store.add_discovered([{'handle': 'x'}], now=self.clock())
        tor = Egress('t0', 'tor', host='127.0.0.1', port=9050)
        p = self.pool([tor], lambda e, url: resp(302, '', url='https://www.instagram.com/accounts/login/'), swap_limit=100)
        p.html_route['x'] = p.html_max - 1
        self.assertEqual(p.step(tor), 'html_gave_up')
        self.assertNotIn('x', p.html_route)


    def test_network_error_releases(self):
        self.store.add_discovered([{'handle': 'a'}], now=self.clock())
        tor = Egress('t0', 'tor', host='127.0.0.1', port=9050)
        p = self.pool([tor], lambda e, url: OSError('boom'))
        self.assertEqual(p.step(tor), 'network')
        self.assertEqual(self.store.stats()['new'], 1)

    def test_account_egress_and_unverified_egress_never_used(self):
        reg = Registry(HomeGuard(['198.51.100.7']))
        mine = reg.add(Egress('phone', 'socks5', host='h', port=1, logged_in_ok=True))
        reg.bind_account('bot1', 'phone')
        loose = reg.add(Egress('box', 'socks5', host='h', port=2))
        self.store.add_discovered([{'handle': 'a'}], now=self.clock())
        p = self.pool([mine, loose], lambda e, url: resp(200, profile_json('a')), registry=reg)
        self.assertEqual([e.id for e in p.egresses], ['box'])
        self.assertEqual(p.step(loose), 'unverified')
        self.assertEqual(self.tr.log, [])


if __name__ == '__main__':
    unittest.main()
