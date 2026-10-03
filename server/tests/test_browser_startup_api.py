"""Start reports connection intent honestly and never needs a manual Chrome prerequisite."""
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_server import Base
import browser_startup
import db
import server


class BrowserStartupApiTests(Base):
    def setUp(self):
        super().setUp()
        # Host launching is tested with injected browser functions only.
        self.app.collection.host_operations_allowed = True

    def test_seed_start_reuses_isolation_and_reports_connecting_not_started(self):
        db.set_setting(self.conn,'instagram_collection_isolation',{'accounts':{'one':'101','two':'102'}})
        self.conn.commit()
        pending={'collection_startup':{'state':'opening'},'stages':[{'id':'lists','paused':True},{'id':'bios','paused':True}]}
        with patch.object(server.get_application().collection,'api_control_set',return_value=pending) as start:
            code,out=self.call('/api/start',{'handle':'brand','directions':['following']})
        self.assertEqual(code,200)
        self.assertTrue(out['starting']);self.assertFalse(out['started'])
        self.assertEqual(start.call_args.args[2],{'action':'resume_selected_accounts','accounts':[{'lane_id':'one','ig_id':'101'},{'lane_id':'two','ig_id':'102'}]})

    def test_warning_acknowledgment_requires_pending_connection_to_stop(self):
        db.set_setting(self.conn,'collection_startup',{'id':'pending','state':'waiting','deadline_at':9999999999})
        self.conn.commit()
        code,out=self.call('/api/control',{'action':'acknowledge_scraping_warning','account':'one','reviewed':True})
        self.assertEqual(code,400)
        self.assertIn('Stop the connection',out['error'])
        self.assertEqual(self.call('/api/control',{'action':'pause','stage':'collection'})[0],200)
        self.assertEqual(db.get_setting(self.conn,'collection_startup')['state'],'cancelled')

    def test_connect_and_deferred_selected_start_do_not_resume_early(self):
        for action in ('connect_accounts','resume_selected_accounts'):
            with self.subTest(action=action),patch.object(browser_startup,'begin',return_value=True),patch.object(server.control,'apply') as apply:
                code,_=self.call('/api/control',{'action':action,'accounts':[{'lane_id':'one','ig_id':'101'},{'lane_id':'two','ig_id':'102'}]})
                self.assertEqual(code,200)
                apply.assert_not_called()

    def test_seed_queue_survives_a_start_validation_error(self):
        with patch.object(server.get_application().collection,'api_control_set',side_effect=ValueError('Account needs attention')):
            code,out=self.call('/api/start',{'handle':'brand','directions':['following']})
        self.assertEqual(code,200)
        self.assertEqual(out['queued'],1)
        self.assertFalse(out['started']);self.assertFalse(out['starting'])
        self.assertEqual(out['note'],'Account needs attention')
