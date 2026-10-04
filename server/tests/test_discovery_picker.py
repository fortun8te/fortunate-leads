import importlib.util, sqlite3, sys, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import discovery_picker as dp
import collection_suggestions as cs

spec = importlib.util.spec_from_file_location('picker_replay', Path(__file__).resolve().parents[2] / 'ops' / 'discovery_picker_replay.py')
replay = importlib.util.module_from_spec(spec); spec.loader.exec_module(replay)


def cand(h, **k):
    return dp.Candidate(handle=h, **k)


class Scoring(unittest.TestCase):
    def test_overlap_and_verified_lower_score_size_raises_it(self):
        ctx = dp.Context()
        base = dp.score(cand('a', following=800, overlap=1, parents=frozenset({'x'})), ctx)['score']
        self.assertLess(dp.score(cand('a', following=800, overlap=9), ctx)['score'], base)
        self.assertLess(dp.score(cand('a', following=800, overlap=1, verified=True), ctx)['score'], base)
        self.assertGreater(dp.score(cand('a', following=2500, overlap=1), ctx)['score'], base)

    def test_size_limits_and_unknown_size(self):
        ctx = dp.Context()
        picks = dp.choose([cand('tiny', following=20), cand('huge', following=90000), cand('ok', following=500), cand('unk')], ctx, 4)
        self.assertEqual({c.handle for c, _ in picks}, {'ok', 'unk'})

    def test_saturated_neighbourhood_moves_down_and_fresh_one_up(self):
        ctx = dp.Context()
        for _ in range(5):
            dp.update_context(ctx, {'stale'}, 0.2, 500)
            dp.update_context(ctx, {'fresh'}, 0.95, 500)
        s = lambda p: dp.score(cand('c', following=800, overlap=1, parents=frozenset({p})), ctx)['score']
        self.assertGreater(s('fresh'), s('stale'))

    def test_diversity_avoids_lookalikes(self):
        ctx = dp.Context()
        a = cand('a', following=1500, parents=frozenset({'p', 'q'}), overlap=2)
        b = cand('b', following=990, parents=frozenset({'p', 'q'}), overlap=2)
        c = cand('c', following=900, parents=frozenset({'z'}), overlap=2)
        self.assertEqual([x.handle for x, _ in dp.choose([a, b, c], ctx, 2)], ['a', 'c'])
        self.assertEqual([x.handle for x, _ in dp.choose([b, c], ctx, 1, pending_parents=[a.parents])], ['c'])

    def test_owner_evidence_boosts(self):
        ctx = dp.Context()
        self.assertGreater(dp.score(cand('a', following=800, owner_status='client'), ctx)['score'],
                           dp.score(cand('a', following=800), ctx)['score'])

    def test_reason_is_plain(self):
        ctx = dp.Context()
        text = dp.reason(cand('a', following=900, parents=frozenset({'x'}), overlap=1), dp.score(cand('a'), ctx), ctx)
        self.assertTrue(text.endswith('.')); self.assertNotIn('_', text); self.assertIn('@x', text)


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'p.sqlite'))
        cs.ensure(self.conn)
        # one collected list (seed 'root') whose members are candidates
        db.queue_list(self.conn, 'root', 'following')
        for i in range(6):
            pid = db.upsert_person(self.conn, {'handle': f'cand{i}', 'following': 600 + i * 100})
            self.conn.execute("INSERT INTO edges(seed,person_id,direction,first_seen) VALUES('root',?,'following','2026-10-01')", (pid,))
        pid = db.upsert_person(self.conn, {'handle': 'priv', 'following': 900, 'is_private': 1})
        self.conn.execute("INSERT INTO edges(seed,person_id,direction,first_seen) VALUES('root',?,'following','2026-10-01')", (pid,))
        self.conn.execute("UPDATE jobs SET state='done'")
        self.conn.commit()

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_tops_up_to_depth_tags_auto_skips_private_never_readds(self):
        self.assertEqual(cs.policy(self.conn), 'adaptive_yield')
        added = dp.top_up(self.conn, 1)
        self.assertEqual(len(added), 2)
        self.assertNotIn('priv', [a['handle'] for a in added])
        rows = self.conn.execute("SELECT priority FROM jobs WHERE state='queued'").fetchall()
        self.assertTrue(all(r[0] == dp.AUTO_PRIORITY for r in rows))
        self.assertEqual(dp.top_up(self.conn, 1), [])            # already at depth
        dp.remove(self.conn, added[0]['handle'])
        self.conn.execute("UPDATE jobs SET state='done'")
        again = dp.top_up(self.conn, 1)
        self.assertNotIn(added[0]['handle'], [a['handle'] for a in again])
        self.assertNotIn(added[1]['handle'], [a['handle'] for a in again])

    def test_daily_cap_and_off_switch(self):
        dp.set_settings(self.conn, daily_cap=1)
        self.assertEqual(len(dp.top_up(self.conn, 3)), 1)
        self.conn.execute("UPDATE jobs SET state='done'")
        self.assertEqual(dp.top_up(self.conn, 3), [])
        dp.set_settings(self.conn, daily_cap=12, enabled=False)
        self.assertEqual(dp.top_up(self.conn, 3), [])
        with self.assertRaises(ValueError): dp.set_settings(self.conn, depth=0)

    def test_manual_pending_counts_and_pin_gives_manual_priority(self):
        db.queue_list(self.conn, 'mine1', 'following'); db.queue_list(self.conn, 'mine2', 'following')
        self.assertEqual(dp.top_up(self.conn, 1), [])
        self.conn.execute("UPDATE jobs SET state='done'")
        a = dp.top_up(self.conn, 1)[0]['handle']
        self.assertEqual(dp.pin(self.conn, a), 'ok')
        self.assertEqual(self.conn.execute("SELECT priority FROM jobs WHERE seed=? AND state='queued'", (a,)).fetchone()[0], 0)
        self.assertEqual(dp.listing(self.conn)['items'][0]['pinned'] or dp.listing(self.conn)['items'][1]['pinned'], True)
        self.assertEqual(dp.pin(self.conn, 'nobody'), 'missing')


class Replay(unittest.TestCase):
    def test_overlap_first_is_not_better_than_fresh_first_on_toy_history(self):
        conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
        conn.executescript('''
          CREATE TABLE people(id,handle,first_seen,followers,following,is_verified,is_business,category,is_private);
          CREATE TABLE edges(seed,person_id,direction,first_seen);
          CREATE TABLE lists(seed,direction);''')
        for i in range(1, 400):
            conn.execute("INSERT INTO people VALUES(?,?,?,?,?,?,?,?,?)", (i, f'p{i}', '2026-10-01T00:00:00+00:00' if i < 50 else '2026-10-02T00:00:00+00:00', 1, 1, 0, None, None, 0))
        conn.execute("INSERT INTO people VALUES(1000,'a','2026-09-01T00:00:00+00:00',1,500,0,NULL,NULL,0)")
        conn.execute("INSERT INTO people VALUES(1001,'b','2026-09-01T00:00:00+00:00',1,500,0,NULL,NULL,0)")
        for seed, rng in (('a', range(1, 50)), ('b', range(100, 300))):
            conn.execute("INSERT INTO lists VALUES(?, 'following')", (seed,))
            for i in rng: conn.execute("INSERT INTO edges VALUES(?,?,'following','2026-10-03T00:00:00+00:00')", (seed, i))
        ds = replay.Dataset(conn, cutoff='2026-10-02T00:00:00+00:00', min_members=10)
        self.assertEqual(set(ds.pool), {'a', 'b'})
        curve = replay.mean_curve(replay.run(ds, replay.largest_first, depth=1)[0])
        self.assertGreater(curve[250], 0)


if __name__ == '__main__':
    unittest.main()
