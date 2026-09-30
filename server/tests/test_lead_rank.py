"""Compare maintained ordering against the complete legacy SQL, including writes."""
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import db
import lead_rank
import server
import test_lead_thin_page


class IndexedLeadPage(test_lead_thin_page.ThinLeadPage):
    def check_pages(self):
        self.assertTrue(lead_rank.ready(self.conn))
        for sort in ('fit', 'score'):
            for offset in (0, 3, 12, 50):
                query = {'sort': [sort], 'limit': ['4'], 'offset': [str(offset)]}
                total, rows = self.oracle(query)
                page = server.api_leads(self.conn, query, {})
                self.assertEqual(page['total'], total)
                self.assertEqual(page['rows'], rows)

    def test_profile_verdict_degree_status_and_owner_changes(self):
        self.check_pages()
        self.conn.execute("UPDATE people SET followers=999 WHERE id=2")
        self.conn.execute("UPDATE verdicts SET tier='unread',content_fit=99,score=1000 WHERE person_id=2")
        self.conn.execute("DELETE FROM verdicts WHERE person_id=3")
        self.conn.execute("INSERT OR REPLACE INTO marks VALUES(2,'no','', 'now')")
        self.conn.execute("INSERT OR REPLACE INTO marks VALUES(4,'interested','', 'now')")
        db.add_edge(self.conn, 'another', 5, 'followers')
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('person4',1)")
        self.conn.execute("UPDATE people SET handle='fortun8te' WHERE id=6")
        self.check_pages()
        self.conn.execute('DELETE FROM people WHERE id=2')
        self.conn.execute('DELETE FROM edges WHERE person_id=5')
        self.conn.execute('DELETE FROM marks WHERE person_id=4')
        self.check_pages()

    def test_preparation_is_explicit_and_repairs_exactly(self):
        self.conn.execute("UPDATE settings SET value='false' WHERE key='lead_rank_ready'")
        self.conn.execute('DELETE FROM lead_rank WHERE person_id=3')
        lead_rank.prepare(self.conn)
        self.check_pages()

    def test_missing_trigger_uses_exact_fallback_until_prepared(self):
        self.conn.execute('DROP TRIGGER lead_rank_sync_verdicts_update')
        self.conn.execute('UPDATE verdicts SET score=1000 WHERE person_id=3')
        self.assertFalse(lead_rank.ready(self.conn))
        lead_rank.ensure(self.conn)
        self.assertFalse(lead_rank.ready(self.conn))
        query = {'sort': ['score'], 'limit': ['4'], 'offset': ['0']}
        total, rows = self.oracle(query)
        page = server.api_leads(self.conn, query, {})
        self.assertEqual((page['total'], page['rows']), (total, rows))
        lead_rank.prepare(self.conn)
        self.check_pages()

    def test_interrupted_autocommit_prepare_preserves_prior_snapshot(self):
        path = Path(self.temp.name) / 'leads.sqlite'
        self.conn.commit()
        writer = sqlite3.connect(path, isolation_level=None)
        reader = sqlite3.connect(path, isolation_level=None)
        original = list(reader.execute('SELECT * FROM lead_rank ORDER BY person_id'))
        original_totals = list(reader.execute('SELECT * FROM lead_rank_totals ORDER BY hidden'))
        real_ensure = lead_rank.ensure
        calls = 0

        def interrupt(connection):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.assertEqual(list(reader.execute('SELECT * FROM lead_rank ORDER BY person_id')), original)
                self.assertTrue(lead_rank.ready(reader))
                raise RuntimeError('interrupted preparation')
            real_ensure(connection)

        try:
            with patch.object(lead_rank, 'ensure', side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    lead_rank.prepare(writer)
            self.assertFalse(writer.in_transaction)
            self.assertTrue(lead_rank.ready(writer))
            self.assertEqual(list(writer.execute('SELECT * FROM lead_rank ORDER BY person_id')), original)
            self.assertEqual(list(writer.execute('SELECT * FROM lead_rank_totals ORDER BY hidden')), original_totals)
            self.assertEqual(writer.execute("SELECT count(*) FROM sqlite_master WHERE type='index' AND name IN ('lead_rank_fit','lead_rank_score')").fetchone()[0], 2)
        finally:
            writer.close()
            reader.close()

    def test_unfiltered_counts_match_legacy_and_dynamic_owners(self):
        def check():
            fast = server.counts(self.conn, {})
            with patch.object(lead_rank, 'counts', return_value=None):
                legacy = server.counts(self.conn, {})
            self.assertEqual(fast, legacy)

        check()
        self.conn.execute("UPDATE people SET bio=NULL WHERE id=2")
        self.conn.execute("UPDATE people SET bio='' WHERE id=3")
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('person4',1)")
        self.conn.execute("UPDATE people SET handle='Fortun8Te' WHERE id=6")
        self.conn.execute("UPDATE verdicts SET tier=NULL WHERE person_id=2")
        self.conn.execute("INSERT OR REPLACE INTO marks VALUES(4,'no','', 'now')")
        check()
        self.conn.execute("UPDATE seeds SET is_me=0 WHERE handle='person4'")
        self.conn.execute('DELETE FROM verdicts WHERE person_id=3')
        self.conn.execute('DELETE FROM marks WHERE person_id=4')
        self.conn.execute('DELETE FROM people WHERE id=2')
        check()
        for query in ({'tier': ['warm']}, {'status': ['no']}, {'fit': ['unread']}, {'has_bio': ['0']}):
            with patch.object(lead_rank, 'counts', side_effect=AssertionError('filtered facet shortcut')):
                server.counts(self.conn, query)

    def test_missing_populated_index_requires_explicit_preparation(self):
        self.conn.execute('DROP INDEX lead_rank_fit')
        lead_rank.ensure(self.conn)
        self.assertFalse(lead_rank.ready(self.conn))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='lead_rank_fit'").fetchone())
        lead_rank.prepare(self.conn)
        self.check_pages()

    def test_query_plan_walks_ordering_index_without_sort(self):
        for sort, order in lead_rank.ORDERS.items():
            plan = ' '.join(str(row[3]) for row in self.conn.execute(
                f'EXPLAIN QUERY PLAN SELECT person_id FROM lead_rank WHERE hidden=0 ORDER BY {order} LIMIT 50'))
            self.assertIn('lead_rank_' + sort, plan)
            self.assertNotIn('TEMP B-TREE', plan)

    def test_new_schema_does_not_backfill_an_existing_database(self):
        path = Path(self.temp.name) / 'old.sqlite'
        connection = sqlite3.connect(path)
        connection.executescript('CREATE TABLE people(id INTEGER PRIMARY KEY,followers INTEGER); '
                                 'CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); '
                                 'CREATE TABLE verdicts(person_id INTEGER,tier TEXT,content_fit REAL,score REAL); '
                                 'CREATE TABLE marks(person_id INTEGER,status TEXT); '
                                 'CREATE TABLE map_person_degree(person_id INTEGER,degree INTEGER); '
                                 'INSERT INTO people VALUES(1,10);')
        lead_rank.ensure(connection)
        self.assertEqual(connection.execute('SELECT count(*) FROM lead_rank').fetchone()[0], 0)
        self.assertFalse(lead_rank.ready(connection))
        connection.close()


if __name__ == '__main__':
    unittest.main()
