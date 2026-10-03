"""Selected collection scope and actual runtime state across public controls."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import engine_controls
import processing_modes
import server
from test_server import Base


class StartWorkflow(Base):
    def connect(self, kinds='list'):
        return self.call('/api/ext/next?lane=lane-test&ig_id=101&handle=test.account&kinds=' + kinds)[1]

    def flow(self):
        return self.call('/api/onboarding')[1]['flow']

    def test_chosen_direction_and_checkpoint_survive_stop_and_continue(self):
        _, started = self.call('/api/start', {'handle': 'brand', 'directions': ['following']})
        self.assertEqual((started['queued'], started['directions']), (1, ['following']))
        self.assertEqual([row[0] for row in self.conn.execute('SELECT direction FROM lists')], ['following'])
        self.conn.execute("UPDATE lists SET received=42,cursor='saved-page' WHERE seed='brand'")
        self.conn.commit()
        self.call('/api/control', {'stage': 'collection', 'action': 'pause'})
        self.assertEqual(self.flow()['state'], 'paused')
        _, resumed = self.call('/api/start', {'handle': 'brand', 'directions': ['following']})
        self.assertEqual(resumed['queued'], 0)
        self.assertEqual(tuple(self.conn.execute("SELECT received,cursor FROM lists WHERE seed='brand'").fetchone()), (42, 'saved-page'))
        self.assertFalse(db.get_setting(self.conn, 'qualify'))

    def test_invalid_scope_queues_nothing(self):
        for directions in ([], ['bios'], 'following', None):
            with self.subTest(directions=directions):
                code, _ = self.call('/api/start', {'handle': 'brand', 'directions': directions})
                self.assertEqual(code, 400)
                self.assertEqual(self.conn.execute('SELECT count(*) FROM lists').fetchone()[0], 0)

    def test_connected_queued_work_waits_until_a_request_is_active(self):
        self.connect()
        self.call('/api/start', {'handle': 'brand', 'directions': ['following']})
        self.assertEqual(self.flow()['state'], 'wait')
        self.assertNotIn('Collecting', self.flow()['headline'])
        job = self.connect()['job']
        self.assertIsNotNone(job)
        self.assertEqual(self.flow()['state'], 'wait')
        code, permit = self.call('/api/ext/request', {'action': 'acquire', 'kind': 'list', 'job_id': job['id'],
                                                     'lane_id': 'lane-test', 'account': {'ig_id': '101', 'handle': 'test.account'}})
        self.assertEqual(code, 200)
        self.assertTrue(permit['granted'])
        flow = self.flow()
        self.assertEqual(flow['state'], 'running')
        self.assertIn('following', flow['headline'])
        self.assertEqual(flow['totals_scope'], 'workspace')

    def test_stop_is_acknowledged_after_the_existing_request_finishes(self):
        self.call('/api/start', {'handle': 'brand', 'directions': ['following']})
        job = self.connect()['job']
        code, permit = self.call('/api/ext/request', {'action': 'acquire', 'kind': 'list', 'job_id': job['id'],
                                                     'lane_id': 'lane-test', 'account': {'ig_id': '101', 'handle': 'test.account'}})
        self.assertEqual(code, 200)
        self.assertTrue(permit['granted'])
        self.call('/api/control', {'stage': 'collection', 'action': 'pause'})
        flow = self.flow()
        self.assertEqual(flow['state'], 'stopping')
        self.assertFalse(flow['stop_acknowledged'])
        self.call('/api/ext/request', {'action': 'release', 'token': permit['token'],
                                      'lane_id': 'lane-test', 'account': {'ig_id': '101', 'handle': 'test.account'}})
        flow = self.flow()
        self.assertEqual(flow['state'], 'paused')
        self.assertTrue(flow['stop_acknowledged'])

    def test_stopped_work_lease_is_not_mistaken_for_a_network_request(self):
        self.call('/api/start', {'handle': 'brand', 'directions': ['following']})
        self.assertIsNotNone(self.connect()['job'])
        self.call('/api/control', {'stage': 'collection', 'action': 'pause'})
        self.assertEqual(self.conn.execute("SELECT state FROM jobs WHERE kind='list'").fetchone()[0], 'leased')
        self.assertEqual(self.flow()['state'], 'paused')
        self.assertTrue(self.flow()['stop_acknowledged'])
        self.assertTrue(self.call('/api/control')[1]['stop_acknowledged'])

    def test_expired_unreleased_request_is_uncertain_without_get_mutation(self):
        self.call('/api/start', {'handle': 'brand', 'directions': ['following']})
        self.call('/api/control', {'stage': 'collection', 'action': 'pause'})
        gate = {'active': {'lane': 'lane-test', 'kind': 'list', 'token': 'test-token', 'until': time.time() - 1}, 'queue': []}
        db.set_setting(self.conn, 'instagram_request_gate', gate)
        self.conn.commit()
        controls = self.call('/api/control')[1]
        self.assertFalse(controls['active'])
        self.assertTrue(controls['collection']['unconfirmed'])
        self.assertFalse(controls['stop_acknowledged'])
        self.assertIn('did not confirm', controls['instagram_request_attention']['message'])
        self.assertEqual(db.get_setting(self.conn, 'instagram_request_gate'), gate)
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_attention'))
        self.assertEqual(self.flow()['state'], 'wait')
        self.assertFalse(self.flow()['stop_acknowledged'])

    def test_a_profile_job_is_never_described_as_a_list(self):
        self.connect('profile')
        pid = db.upsert_person(self.conn, {'handle': 'profile.brand'})
        self.conn.commit()
        self.call(f'/api/person/{pid}/read', {})
        self.assertEqual(self.flow()['state'], 'wait')
        job = self.connect('profile')['job']
        self.assertIsNotNone(job)
        self.assertEqual(self.flow()['state'], 'wait')
        code, permit = self.call('/api/ext/request', {'action': 'acquire', 'kind': 'profile', 'job_id': job['id'],
                                                     'lane_id': 'lane-test', 'account': {'ig_id': '101', 'handle': 'test.account'}})
        self.assertEqual(code, 200)
        self.assertTrue(permit['granted'])
        flow = self.flow()
        self.assertEqual(flow['state'], 'running')
        self.assertIn('profile.brand', flow['headline'])
        self.assertNotIn('None', flow['headline'])
        self.assertNotIn('followers', flow['headline'])


class QualificationRuntimeState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = db.init(Path(self.temp.name) / 'qualifier.sqlite')
        self.addCleanup(self.conn.close)
        processing_modes.set_mode(self.conn, 'RLAI')
        db.upsert_person(self.conn, {'handle': 'brand', 'bio': 'Founder of a physical product brand'})
        self.conn.commit()

    def snapshot(self, busy, paused=False):
        processing_modes.set_paused(self.conn, paused)
        self.conn.commit()
        runtime = {'ready': True, 'busy': busy, 'resources': {'allowed': True}}

        def engines(conn, model):
            return dict(engine_controls.snapshot(processing_modes.snapshot(conn),
                        {'k2': dict(model, managed_local=False)}), external_active=False)

        with patch.object(server.local_model, 'status', return_value=runtime), patch.object(server.get_application().processing, 'engine_snapshot', side_effect=engines):
            return server.api_local_processing(self.conn, {}, {})

    def test_waiting_work_and_stop_ack_follow_actual_engine_activity(self):
        waiting = self.snapshot(busy=False)
        self.assertGreater(waiting['queue'], 0)
        self.assertEqual(waiting['state'], 'waiting')
        self.assertEqual(self.snapshot(busy=True)['state'], 'working')
        stopping = self.snapshot(busy=True, paused=True)
        self.assertEqual(stopping['state'], 'stopping')
        self.assertFalse(stopping['stop_acknowledged'])
        stopped = self.snapshot(busy=False, paused=True)
        self.assertEqual(stopped['state'], 'paused')
        self.assertTrue(stopped['stop_acknowledged'])
        self.assertGreater(stopped['queue'], 0)
        self.assertEqual(stopped['processing']['mode'], 'RLAI')


if __name__ == '__main__':
    unittest.main()
