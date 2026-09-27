import os
import sqlite3
import tempfile
import unittest

from scale_helpers import Clock
from scale import bridge, gate, model
from scale.dedupe import SeenStore
from scale.metrics import Metrics


class GateTest(unittest.TestCase):
    def test_isolated_lanes_run_in_parallel(self):
        le = {'a': 'phone-a', 'b': 'phone-b'}
        ok_a, _, st = gate.acquire({}, 'a', le, 100.0)
        ok_b, _, st = gate.acquire(st, 'b', le, 100.0)
        self.assertTrue(ok_a and ok_b)

    def test_unrouted_lanes_share_today_behaviour(self):
        le = {'a': None, 'b': None, 'c': 'phone-c'}
        ok_a, _, st = gate.acquire({}, 'a', le, 100.0)
        ok_b, wait, st = gate.acquire(st, 'b', le, 100.0)
        self.assertTrue(ok_a)
        self.assertFalse(ok_b)
        self.assertEqual(gate.cooldown_scope('a', le), {'a', 'b'})

    def test_hit_cools_only_its_group(self):
        le = {'a': 'x', 'b': 'y'}
        st = gate.hit({}, 'a', le, 1000.0)
        self.assertFalse(gate.acquire(st, 'a', le, 500.0)[0])
        self.assertTrue(gate.acquire(st, 'b', le, 500.0)[0])
        st = gate.release(gate.acquire(st, 'b', le, 500.0)[2], 'b', le, 501.0)
        self.assertFalse(gate.acquire(st, 'b', le, 502.0)[0])      # spacing
        self.assertTrue(gate.acquire(st, 'b', le, 503.1)[0])


class ModelTest(unittest.TestCase):
    def test_central_40k(self):
        r = model.plan(40000, 'central', 3)
        self.assertEqual(r['bot_accounts_needed'], 9)
        self.assertEqual(r['enrichment_units_needed'], 160)
        self.assertEqual(r['binding'], 'bot accounts (list pagination)')

    def test_monotonic(self):
        for s in model.SCEN:
            a, b = model.plan(40000, s), model.plan(200000, s)
            self.assertLessEqual(a['bot_accounts_needed'], b['bot_accounts_needed'])
            self.assertLessEqual(a['enrichment_units_needed'], b['enrichment_units_needed'])
        self.assertIn('| 200,000 |', model.table())


class MetricsTest(unittest.TestCase):
    def test_rolling_counts(self):
        c, m = Clock(), Metrics()
        m.inc('egress', 't0', 'ok', now=c())
        c.advance(30)
        m.inc('egress', 't0', 'ok', now=c(), n=2)
        self.assertEqual(m.rate('egress', 't0', 'ok', 60, now=c()), 3)
        c.advance(3601)
        snap = m.snapshot(now=c())
        self.assertEqual(snap['egress']['t0']['ok']['last_hour'], 0)
        self.assertEqual(snap['egress']['t0']['ok']['total'], 3)
        self.assertIn('fl_scale_events_total{scope="egress",id="t0",event="ok"} 3', m.prometheus(now=c()))


class BridgeTest(unittest.TestCase):
    def test_list_page_and_leads_import(self):
        st = SeenStore()
        self.assertEqual(bridge.on_list_page([{'handle': 'a'}, {'handle': 'b'}], 'Seed', 'followers', st), ['a', 'b'])
        conn = sqlite3.connect(':memory:')
        conn.executescript("CREATE TABLE people(handle TEXT, ig_id TEXT, is_private INT, is_verified INT, bio_at TEXT);"
                           "CREATE TABLE seeds(handle TEXT);"
                           "INSERT INTO people VALUES('a',NULL,0,0,'2026-09-27'),('c','9',0,1,NULL),('s',NULL,0,0,NULL),"
                           "('old~1',NULL,0,0,NULL);INSERT INTO seeds VALUES('s');")
        self.assertEqual(bridge.import_known_from_leads(conn, st), 1)
        self.assertEqual(bridge.queue_unread_from_leads(conn, st), 1)
        leased = [h for h, _ in st.lease(10)]
        self.assertEqual(sorted(leased), ['b', 'c'])            # 'a' already has a bio

    def test_default_store_path_env(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ['FL_SCALE_DB'] = os.path.join(d, 'x', 'scale.sqlite')
            try:
                bridge._store = None
                self.assertTrue(bridge.store().known('nobody') is False)
                self.assertTrue(os.path.exists(os.environ['FL_SCALE_DB']))
            finally:
                bridge._store = None
                del os.environ['FL_SCALE_DB']


if __name__ == '__main__':
    unittest.main()
