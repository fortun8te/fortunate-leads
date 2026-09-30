"""Prepared default facets compared with the independent original SQL query."""
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import tag_facets
import server
from test_tag_facets_scale import TagFacetsScale, legacy_facets


class PreparedTagFacets(TagFacetsScale):
    def check(self, query=None):
        query = query or {}
        self.assertTrue(tag_facets.ready(self.conn))
        self.assertEqual(server.tag_facets(self.conn, query), legacy_facets(self.conn, query))

    def test_each_dependency_and_identity_change_preserves_original_query(self):
        mutations = [
            "INSERT INTO tags VALUES(2,'Fit: strong',NULL,'manual')",
            "INSERT INTO tags VALUES(3,'Shared',NULL,'manual')",
            "INSERT INTO tags VALUES(999,'Shared','a','manual')",
            "INSERT INTO tags VALUES(998,'Shared','z','manual')",
            "INSERT INTO tags VALUES(4,'AI: Top fit','ai','auto')",
            "INSERT INTO tags VALUES(4,'Too big','signal','auto')",
            "INSERT INTO tags VALUES(4,'Not reachable','signal','auto')",
            "INSERT INTO owner_context VALUES(4,'[\"friend\",\"client\"]',NULL,'now')",
            "UPDATE owner_context SET relationships='[\"friend\"]' WHERE person_id=4",
            "UPDATE verdicts SET tier='hot',content_fit=90,role='buyer' WHERE person_id=4",
            "INSERT INTO tags VALUES(4,'Client','relationship','manual')",
            "UPDATE tags SET tag='Former client',grp=NULL WHERE person_id=4 AND tag='Client'",
            "UPDATE marks SET status='no' WHERE person_id=4",
            "INSERT OR REPLACE INTO marks(person_id,status) VALUES(4,'talking')",
            "DELETE FROM marks WHERE person_id=4",
            "DELETE FROM tags WHERE person_id=2 AND tag='Fit: strong'",
            "UPDATE verdicts SET person_id=999 WHERE person_id=4",
            "INSERT INTO people(id,handle,first_seen,updated_at) VALUES(999,'rescued','now','now')",
            "UPDATE tags SET person_id=999,tag='Moved shared' WHERE person_id=998",
            "DELETE FROM people WHERE id=999",
            "DELETE FROM owner_context WHERE person_id=4",
            "DELETE FROM verdicts WHERE person_id=999",
            "INSERT INTO seeds(handle,is_me) VALUES('person3',1)",
            "UPDATE people SET handle='FORTUN8TE' WHERE id=2",
            "UPDATE seeds SET is_me=0 WHERE handle='person3'",
        ]
        for sql in mutations:
            with self.subTest(sql=sql):
                self.conn.execute(sql)
                self.check()
        tag_facets.prepare(self.conn)
        self.check()

    def test_nullable_groups_and_branch_priority_are_not_coalesced(self):
        self.conn.execute("INSERT INTO tags VALUES(2,'Friend',NULL,'manual')")
        self.conn.execute("INSERT INTO owner_context VALUES(3,'[\"friend\"]',NULL,'now')")
        self.check()
        friend = next(item for item in tag_facets.default(self.conn) if item['tag'] == 'Friend')
        self.assertEqual(friend['grp'], 'relationship')
        self.conn.execute("INSERT INTO tags VALUES(999,'Friend','a','manual')")
        self.check()
        friend = next(item for item in tag_facets.default(self.conn) if item['tag'] == 'Friend')
        self.assertEqual(friend['grp'], 'relationship')
        self.conn.execute("DELETE FROM tags WHERE person_id=999")
        self.check()
        self.conn.execute('DELETE FROM owner_context WHERE person_id=3')
        self.check()
        friend = next(item for item in tag_facets.default(self.conn) if item['tag'] == 'Friend')
        self.assertIsNone(friend['grp'])

    def test_owner_change_does_not_rewrite_projection(self):
        before = list(self.conn.execute('SELECT rowid,* FROM tag_facet_rows ORDER BY rowid'))
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('person2',1)")
        self.conn.execute("UPDATE people SET handle='fortun8te' WHERE id=3")
        self.check()
        self.assertEqual(before, list(self.conn.execute('SELECT rowid,* FROM tag_facet_rows ORDER BY rowid')))

    def test_missing_trigger_and_index_require_explicit_repair(self):
        for sql in ('DROP TRIGGER tag_facet_sync_verdicts_update', 'DROP INDEX tag_facet_person'):
            self.conn.execute(sql)
            self.conn.execute('UPDATE verdicts SET content_fit=65 WHERE person_id=2')
            self.assertFalse(tag_facets.ready(self.conn))
            self.assertIsNone(tag_facets.default(self.conn))
            self.assertEqual(server.tag_facets(self.conn, {}), legacy_facets(self.conn, {}))
            tag_facets.ensure(self.conn)
            self.assertFalse(tag_facets.ready(self.conn))
            tag_facets.prepare(self.conn)
            self.check()

    def test_schema_install_does_not_backfill_existing_people(self):
        for name in tag_facets.TRIGGERS:
            self.conn.execute(f'DROP TRIGGER {name}')
        for table in tag_facets.TABLES:
            self.conn.execute(f'DROP TABLE {table}')
        tag_facets.ensure(self.conn)
        self.assertFalse(tag_facets.ready(self.conn))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tag_facet_rows').fetchone()[0], 0)
        self.conn.execute("INSERT INTO tags VALUES(2,'Unprepared edit','signal','manual')")
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tag_facet_rows').fetchone()[0], 0)
        tag_facets.prepare(self.conn)
        self.check()

    def test_missing_index_skips_sync_until_explicit_preparation(self):
        before = list(self.conn.execute('SELECT * FROM tag_facet_rows'))
        self.conn.execute('DROP INDEX tag_facet_person')
        self.conn.execute("INSERT INTO tags VALUES(2,'Deferred edit','signal','manual')")
        self.assertEqual(before, list(self.conn.execute('SELECT * FROM tag_facet_rows')))
        self.assertFalse(tag_facets.ready(self.conn))
        tag_facets.prepare(self.conn)
        self.check()

    def test_quoted_group_values_have_distinct_summary_keys(self):
        for pid, group in ((2, "a':'b"), (3, 'NULL'), (4, None), (999, "'")):
            self.conn.execute("INSERT INTO tags VALUES(?,'Quoted',?,'manual')", (pid, group))
        self.check()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tag_facet_totals WHERE tag='Quoted'").fetchone()[0], 4)

    def test_filtered_requests_use_original_relation(self):
        for query in ({'status': ['all']}, {'tier': ['hot']}, {'tags': ['Founder']}, {'q': ['Saved note']}):
            with patch.object(tag_facets, 'default', side_effect=AssertionError('filtered shortcut')):
                self.assertEqual(server.tag_facets(self.conn, query), legacy_facets(self.conn, query))

    def test_interrupted_autocommit_prepare_preserves_readers_and_rolls_back(self):
        self.conn.commit()
        path = Path(self.temp.name) / 'leads.sqlite'
        writer = sqlite3.connect(path, isolation_level=None)
        reader = sqlite3.connect(path, isolation_level=None)
        original = tag_facets.default(reader)
        real = tag_facets.ensure
        calls = 0

        def interrupt(connection):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.assertEqual(tag_facets.default(reader), original)
                raise RuntimeError('interrupted')
            real(connection)

        try:
            with patch.object(tag_facets, 'ensure', side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    tag_facets.prepare(writer)
            self.assertFalse(writer.in_transaction)
            self.assertTrue(tag_facets.ready(writer))
            self.assertEqual(tag_facets.default(writer), original)
        finally:
            writer.close()
            reader.close()


if __name__ == '__main__':
    unittest.main()
