import unittest

import scale_helpers  # noqa: F401
from scale.dedupe import MAX_ATTEMPTS, SeenStore

T0 = 1_800_000_000.0


class SeenStoreTest(unittest.TestCase):
    def test_only_new_handles_returned(self):
        s = SeenStore()
        self.assertEqual(s.add_discovered([{'handle': 'A'}, {'handle': 'b'}], source='x/followers', now=T0), ['a', 'b'])
        self.assertEqual(s.add_discovered([{'handle': '@a'}, {'handle': 'c'}], source='y/followers', now=T0), ['c'])
        self.assertEqual(s.stats()['total'], 3)

    def test_more_lists_higher_priority(self):
        s = SeenStore()
        s.add_discovered([{'handle': 'solo'}, {'handle': 'multi'}], source='s1/followers', now=T0)
        s.add_discovered([{'handle': 'multi'}], source='s2/following', now=T0)
        s.add_discovered([{'handle': 'multi'}], source='s2/following', now=T0)   # same list again: no bump
        self.assertEqual([h for h, _ in s.lease(2, now=T0)], ['multi', 'solo'])
        row = s.conn.execute("SELECT sources FROM seen WHERE handle='multi'").fetchone()
        self.assertEqual(row['sources'], 2)

    def test_done_never_refetched_inside_ttl(self):
        s = SeenStore(ttl=100)
        s.add_discovered([{'handle': 'a'}], now=T0)
        self.assertEqual(len(s.lease(10, now=T0)), 1)
        s.done('a', now=T0)
        s.add_discovered([{'handle': 'a'}], source='other/followers', now=T0 + 1)
        self.assertEqual(s.lease(10, now=T0 + 50), [])
        self.assertEqual(len(s.lease(10, now=T0 + 101)), 1)       # stale: refresh allowed

    def test_lease_expiry_and_release(self):
        s = SeenStore()
        s.add_discovered([{'handle': 'a'}], now=T0)
        s.lease(1, now=T0, lease_s=60)
        self.assertEqual(s.lease(1, now=T0 + 30), [])
        self.assertEqual(len(s.lease(1, now=T0 + 61)), 1)
        s.release('a')
        self.assertEqual(len(s.lease(1, now=T0 + 62)), 1)

    def test_failures_back_off_then_park(self):
        s = SeenStore()
        s.add_discovered([{'handle': 'a'}], now=T0)
        t = T0
        for i in range(MAX_ATTEMPTS):
            got = s.lease(1, now=t)
            self.assertEqual(len(got), 1, i)
            s.fail('a', now=t, backoff_s=10)
            self.assertEqual(s.lease(1, now=t + 1), [])
            t += 10 * (i + 1) + 1
        self.assertEqual(s.stats().get('dead'), 1)

    def test_import_known(self):
        s = SeenStore()
        s.import_known(['Known'], now=T0)
        self.assertEqual(s.add_discovered([{'handle': 'known'}], now=T0), [])
        self.assertEqual(s.lease(5, now=T0), [])


if __name__ == '__main__':
    unittest.main()
