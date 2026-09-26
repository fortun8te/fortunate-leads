"""One offline path from a leased list page through profile scoring to the map."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import server


class IngestMapIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'leads.sqlite')
        self.conn = db.init(self.path)
        self.addCleanup(self.conn.close)
        self.old_db = server.CFG['db']
        server.CFG['db'] = self.path
        self.addCleanup(server.CFG.__setitem__, 'db', self.old_db)
        self.addCleanup(server.clear_caches)
        server.clear_caches()

    def page(self, members, total, refresh=False):
        c = self.conn
        self.assertTrue(db.queue_list(c, 'brandseed', 'following', refresh=refresh))
        c.commit()
        job = server.ext_next(c, {'lane': ['lane-a']}, {})['job']
        self.assertIsNotNone(job)
        result = server.ext_list_page(c, {'lane': ['lane-a']}, {
            'job_id': job['id'], 'seed': job['seed'], 'direction': job['direction'],
            'lease_token': job['lease_token'], 'requested_cursor': job['cursor'],
            'users': members, 'done': True, 'total': total, 'total_source': 'current_run',
        })
        self.assertEqual(result['received'], len(members))
        self.assertEqual(c.execute("SELECT state FROM lists WHERE seed='brandseed'").fetchone()[0], 'done')

    def lead(self):
        graph = server.api_map(self.conn, {'scope': ['all']}, {})
        return graph, next((n for n in graph['nodes'] if n.get('handle') == 'maker'), None)

    def test_list_profile_qualification_and_refresh_update_map(self):
        c = self.conn
        self.page([{'ig_id': '200', 'handle': 'maker', 'name': 'Maker'}], 1)
        first, lead = self.lead()
        self.assertIsNotNone(lead)
        self.assertEqual((first['total'], lead['degree'], lead['seeds']), (1, 1, ['brandseed']))
        self.assertEqual(lead['fit'], 'unread')

        server.ext_profile(c, {}, {'profile': {'ig_id': '200', 'handle': 'maker',
                                              'bio': 'Founder of a skincare brand',
                                              'website': 'https://maker.example'}})
        self.assertEqual(server.qualify_batch(c), 1)
        second, scored = self.lead()
        self.assertGreater(second['rev'], first['rev'])
        self.assertIsNotNone(scored['score'])
        self.assertIsNotNone(scored['business_fit'])
        self.assertEqual(c.execute("SELECT bio_src FROM people WHERE handle='maker'").fetchone()[0], 'extension')

        self.page([], 0, refresh=True)
        third, missing = self.lead()
        self.assertGreater(third['rev'], second['rev'])
        self.assertEqual(third['total'], 0)
        self.assertIsNone(missing)
        self.assertEqual(c.execute('SELECT count(*) FROM edges').fetchone()[0], 1)
        self.assertEqual(c.execute('SELECT active FROM edge_evidence').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
