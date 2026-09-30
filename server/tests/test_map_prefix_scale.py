"""A complete indexed prefix page does not depend on the name-search pool."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import map_view as MV


class MapPrefixScale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'map.sqlite'
        self.conn = db.init(self.path)
        self.prefix = []
        for i in range(90):
            pid = db.upsert_person(self.conn, {'handle': f'needle{i:03}'})
            db.add_edge(self.conn, 'source', pid, 'followers')
            self.prefix.append(pid)
        self.extra = db.upsert_person(self.conn, {'handle': 'another', 'name': 'Needle'})
        db.add_edge(self.conn, 'source', self.extra, 'followers')

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_full_prefix_page_never_loads_name_pool(self):
        with patch.object(MV, '_index_for', side_effect=AssertionError('unnecessary name-search pool')):
            ids, capped = MV.search_ids(self.conn, self.path, '@NEEDLE', limit=20)
        self.assertEqual(ids, self.prefix[:20])
        self.assertTrue(capped)

    def test_incomplete_eligible_prefix_page_keeps_name_fallback(self):
        # Prefix candidates outside the current map must not suppress names.
        self.conn.execute('DELETE FROM edges WHERE person_id>?', (self.prefix[1],))
        db.add_edge(self.conn, 'source', self.extra, 'followers')
        with patch.object(MV, '_index_for', return_value=[(self.extra, 'another', 'needle')]) as pool:
            ids, capped = MV.search_ids(self.conn, self.path, 'needle', limit=20)
        pool.assert_called_once()
        self.assertEqual(ids, [*self.prefix[:2], self.extra])
        self.assertTrue(capped)

    def test_exact_result_outranks_prefixes_and_name_results(self):
        exact = db.upsert_person(self.conn, {'handle': 'needle'})
        db.add_edge(self.conn, 'source', exact, 'following')
        with patch.object(MV, '_index_for', side_effect=AssertionError('unnecessary name-search pool')):
            ids, capped = MV.search_ids(self.conn, self.path, 'needle', limit=20)
        self.assertEqual(ids[0], exact)
        self.assertEqual(ids[1:], self.prefix[:19])
        self.assertTrue(capped)


if __name__ == '__main__':
    unittest.main()
