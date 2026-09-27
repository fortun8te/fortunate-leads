"""The map summary must equal current directed evidence through real writes."""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import server  # noqa: E402


class MapSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'synthetic.sqlite')
        self.conn = db.init(self.path)
        self.addCleanup(self.conn.close)
        server.clear_caches()
        self.ts = '2026-01-01T00:00:00+00:00'
        self.conn.executemany('INSERT INTO people(id,handle,first_seen,updated_at) VALUES(?,?,?,?)',
                              [(1, 'alice', self.ts, self.ts), (2, 'bob', self.ts, self.ts),
                               (3, 'carol', self.ts, self.ts)])
        self.conn.executemany('INSERT INTO seeds(handle) VALUES(?)', [('sourcea',), ('sourceb',), ('sourcec',)])
        self.conn.commit()

    def assert_summary_exact(self):
        actual = dict(self.conn.execute('SELECT person_id,degree FROM map_person_degree'))
        expected = dict(self.conn.execute('SELECT e.person_id,count(DISTINCT e.seed) FROM current_edges e '
                                          'JOIN people p ON p.id=e.person_id GROUP BY e.person_id'))
        self.assertEqual(actual, expected)
        source_actual = dict(self.conn.execute('SELECT handle,refs FROM map_source_handles'))
        source_expected = dict(self.conn.execute('SELECT handle,count(*) FROM ('
                                                 'SELECT seed AS handle FROM edges UNION ALL SELECT handle FROM seeds) '
                                                 'GROUP BY handle'))
        self.assertEqual(source_actual, source_expected)
        member_actual = {(r['person_id'], r['seed']) for r in self.conn.execute('SELECT * FROM map_seed_member')}
        member_expected = {(r['person_id'], r['seed']) for r in self.conn.execute(
            'SELECT DISTINCT person_id,seed FROM current_edges')}
        self.assertEqual(member_actual, member_expected)
        seed_actual = dict(self.conn.execute('SELECT seed,degree FROM map_seed_degree'))
        seed_expected = dict(self.conn.execute('SELECT seed,count(*) FROM map_seed_member GROUP BY seed'))
        self.assertEqual(seed_actual, seed_expected)
        return actual

    def edge(self, seed, pid, direction='followers', active=1):
        self.conn.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?,?)', (seed, pid, direction, self.ts))
        if active is not None:
            self.conn.execute('INSERT OR REPLACE INTO edge_evidence VALUES(?,?,?,?,?,?)',
                              (seed, pid, direction, active, self.ts, self.ts))

    def test_insert_update_delete_and_historical_only(self):
        self.edge('sourcea', 1, active=None)
        self.assertEqual(self.assert_summary_exact(), {})
        self.conn.execute('INSERT INTO edge_evidence VALUES(?,?,?,?,?,?)',
                          ('sourcea', 1, 'followers', 1, self.ts, self.ts))
        self.assertEqual(self.assert_summary_exact(), {1: 1})
        self.edge('sourcea', 1, 'following')  # two directions, one unique source
        self.edge('sourceb', 1)
        self.assertEqual(self.assert_summary_exact(), {1: 2})
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='sourceb' AND person_id=1")
        self.assertEqual(self.assert_summary_exact(), {1: 1})
        self.conn.execute("DELETE FROM edges WHERE seed='sourcea' AND person_id=1 AND direction='followers'")
        self.assertEqual(self.assert_summary_exact(), {1: 1})
        self.conn.execute("DELETE FROM edge_evidence WHERE seed='sourcea' AND person_id=1")
        self.assertEqual(self.assert_summary_exact(), {})

    def test_rename_merge_duplicate_observation_and_rollback_replay(self):
        self.edge('sourcea', 1)
        self.edge('sourcea', 2)
        self.edge('sourceb', 2)
        self.conn.execute('UPDATE edges SET seed=? WHERE seed=? AND person_id=?', ('sourcec', 'sourceb', 2))
        self.conn.execute('UPDATE edge_evidence SET seed=? WHERE seed=? AND person_id=?', ('sourcec', 'sourceb', 2))
        self.assertEqual(self.assert_summary_exact(), {1: 1, 2: 2})
        db.merge_people(self.conn, keep=1, drop=2)
        self.assertEqual(self.assert_summary_exact(), {1: 2})
        self.conn.execute('INSERT INTO edge_observations VALUES(?,?,?,?,?,?)',
                          ('sourcea', 1, 'followers', 'page-1', 1, self.ts))
        self.conn.execute('INSERT OR IGNORE INTO edge_observations VALUES(?,?,?,?,?,?)',
                          ('sourcea', 1, 'followers', 'page-1', 1, self.ts))
        self.assertEqual(self.assert_summary_exact(), {1: 2})
        self.conn.commit()
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='sourcec'")
        self.assertEqual(self.assert_summary_exact(), {1: 1})
        self.conn.rollback()
        self.assertEqual(self.assert_summary_exact(), {1: 2})
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='sourcec'")
        self.conn.commit()
        self.assertEqual(self.assert_summary_exact(), {1: 1})

    def test_backfill_and_unready_fallback(self):
        self.edge('sourcea', 1)
        self.edge('sourceb', 1)
        self.conn.commit()
        self.conn.execute('DELETE FROM map_person_degree')
        self.conn.execute("DELETE FROM settings WHERE key='map_person_degree_v1'")
        self.conn.commit()
        # Missing ready marker uses the real current_edges view, even though
        # the summary has been deliberately emptied.
        fallback = server.map_graph(self.conn, {'scope': ['all']})
        self.assertEqual(next(n for n in fallback['nodes'] if n['kind'] == 'lead')['degree'], 2)
        self.conn.close()
        self.conn = db.init(self.path)
        self.assertEqual(self.assert_summary_exact(), {1: 2})
        self.assertTrue(db.get_setting(self.conn, 'map_person_degree_v1'))

    def test_rank_and_visibility_follow_verdict_and_mark_edits(self):
        self.edge('sourcea', 1)
        self.edge('sourceb', 2)
        self.conn.execute("INSERT INTO verdicts(person_id,score,tier) VALUES(1,90,'hot')")
        self.conn.execute("INSERT INTO verdicts(person_id,score,tier) VALUES(2,10,'cold')")
        self.assertEqual(self.conn.execute('SELECT score FROM map_person_degree WHERE person_id=1').fetchone()[0], 90)
        self.conn.execute('UPDATE verdicts SET score=5 WHERE person_id=1')
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(2,'no')")
        self.assertEqual(self.conn.execute('SELECT score FROM map_person_degree WHERE person_id=1').fetchone()[0], 5)
        self.assertEqual(self.conn.execute('SELECT hidden FROM map_person_degree WHERE person_id=2').fetchone()[0], 1)
        graph = server.map_graph(self.conn, {'scope': ['all']})
        self.assertEqual((graph['total'], [n['id'] for n in graph['nodes'] if n['kind'] == 'lead']), (1, ['p:1']))
        self.conn.execute("UPDATE marks SET status='interested' WHERE person_id=2")
        self.conn.execute('DELETE FROM verdicts WHERE person_id=1')
        graph = server.map_graph(self.conn, {'scope': ['all']})
        self.assertEqual(graph['total'], 2)
        self.assertEqual(self.conn.execute('SELECT score FROM map_person_degree WHERE person_id=1').fetchone()[0], None)
        self.conn.execute('DELETE FROM marks WHERE person_id=2')
        self.assert_summary_exact()

    def test_person_deletion_and_later_restore_do_not_overcount(self):
        self.edge('sourcea', 1)
        self.assertEqual(self.assert_summary_exact(), {1: 1})
        self.conn.execute('DELETE FROM people WHERE id=1')
        self.assertEqual(self.assert_summary_exact(), {})
        graph = server.map_graph(self.conn, {})
        self.assertEqual(graph['total'], 0)
        self.conn.execute('INSERT INTO people(id,handle,first_seen,updated_at) VALUES(?,?,?,?)',
                          (1, 'alice_restored', self.ts, self.ts))
        self.assertEqual(self.assert_summary_exact(), {1: 1})

    def test_summary_and_read_through_queries_agree_across_filters(self):
        for pid in range(1, 25):
            for seed in (('sourcea', 'sourceb') if pid % 3 == 0 else ('sourcea',)):
                self.edge(seed, pid if pid <= 3 else 3, 'following' if pid % 2 else 'followers')
        self.conn.execute("INSERT INTO verdicts(person_id,score,tier) VALUES(1,80,'hot')")
        self.conn.execute("INSERT INTO verdicts(person_id,score,tier) VALUES(2,20,'cold')")
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(2,'no')")
        self.conn.execute("INSERT INTO tags(person_id,tag,grp,source) VALUES(1,'Fixture','signal','manual')")
        self.conn.commit()
        queries = ({}, {'scope': ['all']}, {'scope': ['all'], 'seed': ['sourceb']},
                   {'tags': ['Fixture']}, {'status': ['all']}, {'min_lists': ['2']})
        expected = [server.api_map(self.conn, q, None) for q in queries]
        self.conn.execute('SAVEPOINT disable_summary')
        self.conn.execute("UPDATE settings SET value='false' WHERE key IN ('map_person_degree_v1','map_source_handles_v1',"
                          "'map_seed_member_v1','map_rank_v1')")
        server.clear_caches()
        actual = [server.api_map(self.conn, q, None) for q in queries]
        self.assertEqual(actual, expected)
        self.conn.execute('ROLLBACK TO disable_summary')
        self.conn.execute('RELEASE disable_summary')

    def test_committed_refresh_tracks_observation_overlap_and_mark_revision(self):
        self.edge('sourcea', 1)
        self.conn.commit()
        before = server.api_map(self.conn, {}, None)
        self.assertEqual(before['total'], 1)
        self.assertEqual(before['seed_links'], [])
        self.edge('sourceb', 1)
        self.conn.commit()
        added = server.api_map(self.conn, {}, None)
        self.assertGreater(added['rev'], before['rev'])
        self.assertEqual(next(n for n in added['nodes'] if n['id'] == 'p:1')['degree'], 2)
        self.assertEqual(added['seed_links'], [{'source': 's:sourcea', 'target': 's:sourceb', 'shared': 1}])
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='sourceb' AND person_id=1")
        self.conn.commit()
        removed = server.api_map(self.conn, {}, None)
        self.assertGreater(removed['rev'], added['rev'])
        self.assertEqual(next(n for n in removed['nodes'] if n['id'] == 'p:1')['degree'], 1)
        self.assertEqual(removed['seed_links'], [])
        self.edge('sourcec', 1, active=None)
        self.conn.commit()
        historical = server.api_map(self.conn, {}, None)
        self.assertEqual(next(n for n in historical['nodes'] if n['id'] == 'p:1')['degree'], 1)
        self.assertEqual(historical['seed_links'], [])
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(1,'no')")
        self.conn.commit()
        hidden = server.api_map(self.conn, {}, None)
        self.assertEqual(hidden['total'], 0)
        self.assertGreater(hidden['rev'], historical['rev'])

    def test_filtered_rank_and_network_yield_match_read_through(self):
        for pid in range(1, 4):
            self.edge('sourcea', pid)
        self.edge('sourceb', 1)
        self.edge('sourceb', 2, 'following')
        self.edge('sourceb', 2, 'followers')  # two directions count once
        self.conn.execute("INSERT INTO verdicts(person_id,score) VALUES(1,50)")
        self.conn.execute("INSERT INTO verdicts(person_id,score) VALUES(2,70)")
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(1,'client')")
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(2,'no')")
        self.conn.commit()
        query = {'status': ['all'], 'seed': ['sourcea'], 'limit': ['10']}
        graph = server.map_graph(self.conn, query)
        self.assertEqual(graph['total'], 3)
        self.assertEqual([n['id'] for n in graph['nodes'] if n['kind'] == 'lead'], ['p:2', 'p:1', 'p:3'])
        expected_net = server.network_context(self.conn, [1, 2, 3], me='')
        self.conn.execute('SAVEPOINT unready_members')
        self.conn.execute("UPDATE settings SET value='false' WHERE key='map_seed_member_v1'")
        self.assertEqual(server.network_context(self.conn, [1, 2, 3], me=''), expected_net)
        self.assertEqual(server.seed_links(self.conn, cacheable=False),
                         [{'source': 's:sourcea', 'target': 's:sourceb', 'shared': 2}])
        self.conn.execute('ROLLBACK TO unready_members')
        self.conn.execute('RELEASE unready_members')

    def test_ui_query_uses_indexed_path_and_repeated_poll_uses_cache(self):
        self.edge('sourcea', 1)
        self.conn.commit()
        query = {'today': ['2026-09-27'], 'scope': ['leads'], 'limit': ['3000']}
        statements = []
        self.conn.set_trace_callback(statements.append)
        with mock.patch.object(server, 'map_graph', wraps=server.map_graph) as build:
            first = server.api_map(self.conn, query, None)
            second = server.api_map(self.conn, query, None)
            self.assertEqual(build.call_count, 1)
        self.conn.set_trace_callback(None)
        self.assertEqual(first, second)
        self.assertTrue(any('SELECT count(*) FROM map_person_degree WHERE hidden=0' in sql
                            for sql in statements))
        self.assertFalse(any('WITH b AS MATERIALIZED' in sql for sql in statements))
        # A date paired with a follow-up filter still takes the general path.
        self.conn.execute("INSERT INTO followups(person_id,due_on,updated_at) VALUES(1,'2026-09-28',?)",
                          (self.ts,))
        self.conn.commit()
        due = server.api_map(self.conn, dict(query, follow_up=['due']), None)
        self.assertEqual(due['total'], 0)
        self.conn.execute("UPDATE followups SET due_on='2026-09-27' WHERE person_id=1")
        self.conn.commit()
        self.assertEqual(server.api_map(self.conn, dict(query, follow_up=['due']), None)['total'], 1)

    def test_profile_edits_reuse_overlap_but_connection_changes_invalidate(self):
        self.edge('sourcea', 1)
        self.edge('sourceb', 1)
        self.conn.commit()
        expected = server.seed_links(self.conn)
        revision = db.get_setting(self.conn, 'map_membership_rev')
        self.conn.execute("UPDATE people SET bio='New bio' WHERE id=1")
        self.conn.execute("INSERT INTO tags VALUES(1,'Client','role','manual')")
        self.conn.commit()
        statements = []
        self.conn.set_trace_callback(statements.append)
        self.assertEqual(server.seed_links(self.conn), expected)
        self.conn.set_trace_callback(None)
        self.assertEqual(db.get_setting(self.conn, 'map_membership_rev'), revision)
        self.assertFalse(any('GROUP BY a.seed,b.seed' in sql for sql in statements))
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='sourceb'")
        self.conn.commit()
        self.assertEqual(server.seed_links(self.conn), [])

    def test_uncommitted_overlap_and_rollback_never_poison_committed_cache(self):
        self.edge('sourcea', 1)
        self.conn.commit()
        self.assertEqual(server.seed_links(self.conn), [])
        self.edge('sourceb', 1)
        self.assertEqual(server.seed_links(self.conn)[0]['shared'], 1)
        self.conn.rollback()
        self.assertEqual(server.seed_links(self.conn), [])
        self.edge('sourcec', 1)
        self.conn.commit()
        self.assertEqual(server.seed_links(self.conn)[0]['target'], 's:sourcec')

    def test_missing_revision_trigger_cannot_serve_stale_overlap(self):
        self.edge('sourcea', 1)
        self.edge('sourceb', 1)
        self.conn.commit()
        self.assertEqual(len(server.seed_links(self.conn)), 1)
        self.conn.execute('DROP TRIGGER map_overlap_rev_delete')
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='sourceb'")
        self.conn.commit()
        self.assertEqual(server.seed_links(self.conn), [])

    def test_missing_maintenance_trigger_falls_back_then_rebuilds(self):
        self.edge('sourcea', 1)
        self.conn.commit()
        self.conn.execute('DROP TRIGGER map_degree_edge_evidence_update')
        self.conn.execute('DROP TRIGGER map_member_edge_evidence_update')
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='sourcea' AND person_id=1")
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT degree FROM map_person_degree WHERE person_id=1').fetchone()[0], 1)
        self.assertEqual(server.map_graph(self.conn, {})['total'], 0)
        self.assertEqual(server.seed_links(self.conn), [])
        self.conn.close()
        self.conn = db.init(self.path)
        self.assert_summary_exact()
        self.assertEqual(server.map_graph(self.conn, {})['total'], 0)


if __name__ == '__main__':
    unittest.main()
