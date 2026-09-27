import random
import unittest

from scale_helpers import Clock, resp
from scale.dedupe import SeenStore
from scale.egress import Egress, HomeGuard, Registry
from scale.lists import Account, AccountScheduler, ListQueue, mobile_list_url, web_list_url
from scale.metrics import Metrics

HOME = '198.51.100.7'


class Client:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch(self, user_id, direction, cursor):
        self.calls.append((user_id, direction, cursor))
        return self.pages.pop(0)


def page(users, nxt=None, more=None):
    body = {'users': [{'username': u, 'pk': i + 1} for i, u in enumerate(users)], 'status': 'ok'}
    if nxt:
        body['next_max_id'] = nxt
    if more is not None:
        body['has_more'] = more
    return resp(200, body)


class ListsTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.reg = Registry(HomeGuard([HOME]))
        for i in (1, 2):
            e = self.reg.add(Egress('p%d' % i, 'socks5', host='h', port=i, logged_in_ok=True))
            e.exit_ip, e.verified_at = '203.0.113.%d' % i, self.clock()
        self.q = ListQueue()
        self.store = SeenStore()

    def sched(self, accounts, clients):
        return AccountScheduler(accounts, self.reg, self.q, self.store, clients, metrics=Metrics(),
                                rng=random.Random(0), clock=self.clock)

    def test_urls(self):
        self.assertIn('search_surface=follow_list_page', web_list_url('1', 'followers', 'c'))
        self.assertIn('count=50', web_list_url('1', 'following'))
        u = mobile_list_url('1', 'followers', 'rt', 'c', count=100)
        self.assertTrue(u.startswith('https://i.instagram.com/api/v1/friendships/1/followers/?count=100'))

    def test_account_without_egress_refused(self):
        with self.assertRaises(ValueError):
            self.sched([Account('b1')], {})

    def test_main_account_off_by_default(self):
        s = self.sched([Account('main', is_main=True, enabled=True, egress_id='p1')], {})
        self.assertEqual(s.accounts, {})
        s2 = self.sched([Account('main2', is_main=True, enabled=True, allow_main=True, egress_id='p2')], {})
        self.assertEqual(s2.accounts['main2'].directions, ('following',))
        self.assertLessEqual(s2.pacers['main2'].daily_budget, 100)

    def test_pages_save_cursor_and_feed_dedupe(self):
        self.q.add('seed', '42', 'followers')
        c = Client([page(['a', 'b'], nxt='c1', more=True), page(['b', 'c'], more=False)])
        s = self.sched([Account('b1', egress_id='p1')], {'b1': c})
        self.assertEqual(s.step(s.accounts['b1']), 'page')
        self.clock.advance(3600)
        self.assertEqual(s.step(s.accounts['b1']), 'page')
        self.assertEqual(c.calls, [('42', 'followers', None), ('42', 'followers', 'c1')])
        row = dict(self.q.conn.execute('SELECT * FROM list_work').fetchone())
        self.assertEqual((row['state'], row['pages'], row['users']), ('done', 2, 4))
        self.assertEqual(self.store.stats()['total'], 3)          # b counted once

    def test_limit_on_one_account_does_not_stop_the_other(self):
        self.q.add('seed', '42', 'followers')
        c1 = Client([page(['a'], nxt='c1', more=True), resp(429, '')])
        c2 = Client([page(['z'], more=False)])
        s = self.sched([Account('b1', egress_id='p1'), Account('b2', egress_id='p2')], {'b1': c1, 'b2': c2})
        b1, b2 = s.accounts['b1'], s.accounts['b2']
        self.assertEqual(s.step(b1), 'page')
        self.clock.advance(3600)
        self.assertEqual(s.step(b1), 'cooldown')
        self.assertNotIn(b1, s.ready())
        self.assertIn(b2, s.ready())
        self.assertEqual(s.step(b2), 'page')                       # resumes the released list...
        self.assertEqual(c2.calls, [('42', 'followers', 'c1')])    # ...from the saved cursor

    def test_challenge_holds_account(self):
        self.q.add('seed', '42', 'following')
        c = Client([resp(400, '{"message":"checkpoint_required","checkpoint_url":"/challenge/x"}')])
        s = self.sched([Account('b1', egress_id='p1')], {'b1': c})
        self.assertEqual(s.step(s.accounts['b1']), 'hold')
        self.assertEqual(s.accounts['b1'].hold, 'challenge')
        self.assertEqual(s.ready(), [])

    def test_unverified_egress_not_ready_and_warmup_holds(self):
        e = self.reg.add(Egress('p3', 'socks5', host='h', port=3, logged_in_ok=True))
        s = self.sched([Account('b3', egress_id='p3')], {})
        self.assertEqual(s.ready(), [])
        e.exit_ip, e.verified_at = '203.0.113.3', self.clock()
        self.assertEqual([a.id for a in s.ready()], ['b3'])
        young = self.sched([Account('b4', egress_id='p1', created_at=self.clock() - 86400)], {})
        self.assertEqual(young.accounts['b4'].hold, 'warming_up')


if __name__ == '__main__':
    unittest.main()
