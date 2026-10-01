"""Explicit continuation is bound to two existing healthy identities, never a warning bypass."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import Base
import accounts
import db


class CollectionIsolationTests(Base):
    def beat(self, lane, uid, tab='ok', **fields):
        return self.call('/api/ext/heartbeat', dict(lane_id=lane, account={'ig_id':uid,'handle':lane},
            version='3.9.30', tab=tab, hold=None, **fields))

    def prepare(self):
        self.beat('warned','100','tab_scraping_warning')
        self.beat('one','101')
        self.beat('two','102')
        db.set_setting(self.conn,'budget',{'list':200,'profile':100})
        self.conn.commit()

    def resume(self, **overrides):
        body={'action':'resume_selected_accounts','accounts':[{'lane_id':'one','ig_id':'101'},{'lane_id':'two','ig_id':'102'}]}
        body.update(overrides)
        return self.call('/api/control',body)

    def test_only_explicit_pairs_can_collect_and_warning_survives(self):
        self.prepare()
        self.assertEqual(self.resume()[0],200)
        self.assertTrue(db.get_setting(self.conn,'instagram_scraping_warning'))
        self.assertFalse(db.get_setting(self.conn,'qualify'))
        self.assertFalse(db.get_setting(self.conn,'paused_lists'))
        for lane in ('one','two'):
            row=self.conn.execute('SELECT * FROM accounts WHERE lane_id=?',(lane,)).fetchone()
            self.assertFalse(accounts.paused_for(self.conn,row))
        self.beat('warned','100')
        self.beat('new-lane','100')
        self.beat('third','103')
        for lane in ('warned','new-lane','third'):
            row=self.conn.execute('SELECT * FROM accounts WHERE lane_id=?',(lane,)).fetchone()
            self.assertTrue(accounts.paused_for(self.conn,row))
            self.assertFalse(accounts.request_permit(self.conn,lane,kind='list')['granted'])
            self.assertIsNone(self.call(f'/api/ext/next?lane={lane}&ig_id={row["ig_id"]}')[1]['job'])
        self.beat('one','104')
        self.assertFalse(accounts.request_permit(self.conn,'one',kind='list')['granted'])
        self.beat('one','101')
        permit=accounts.request_permit(self.conn,'one',kind='list')
        self.assertTrue(permit['granted'])
        accounts.request_permit(self.conn,'one',token=permit['token'])
        self.assertEqual(self.call('/api/control',{'action':'start_all'})[0],400)

    def test_warned_wrong_identity_and_unlimited_selection_are_rejected(self):
        self.prepare()
        for selected in ([{'lane_id':'warned','ig_id':'100'},{'lane_id':'two','ig_id':'102'}],
                         [{'lane_id':'one','ig_id':'999'},{'lane_id':'two','ig_id':'102'}]):
            self.assertEqual(self.resume(accounts=selected)[0],400)
        db.set_setting(self.conn,'budget',{'list':0,'profile':100});self.conn.commit()
        self.assertEqual(self.resume()[0],400)

    def test_any_new_warning_stops_selected_collection(self):
        for code in ('rate_limit','soft_block','login','challenge'):
            with self.subTest(code=code):
                self.prepare()
                self.assertEqual(self.resume()[0],200)
                self.call('/api/ext/error',{'lane_id':'one','account':{'ig_id':'101'},'code':code,'kind':'profile'})
                self.assertIsNone(db.get_setting(self.conn,'instagram_collection_isolation'))
                self.assertTrue(db.get_setting(self.conn,'paused_lists'))
                self.assertFalse(accounts.request_permit(self.conn,'two',kind='list')['granted'])
                # Reset only the isolated test fixture for the next error variant.
                db.set_setting(self.conn,'cooldown',None)
                self.conn.execute('UPDATE accounts SET hold=NULL,cooldown_until=NULL,list_cool_until=NULL,profile_cool_until=NULL')
                self.conn.execute('DELETE FROM account_identity_state')
                self.conn.commit()

    def test_new_warning_tab_revokes_isolation(self):
        self.prepare();self.assertEqual(self.resume()[0],200)
        self.beat('two','102','tab_scraping_warning')
        self.assertIsNone(db.get_setting(self.conn,'instagram_collection_isolation'))
        self.assertTrue(db.get_setting(self.conn,'paused_bios'))
        self.assertIn('two',db.get_setting(self.conn,'instagram_scraping_warning')['pending'])

    def test_acknowledgment_requires_selected_collection_to_stop(self):
        self.prepare();self.assertEqual(self.resume()[0],200)
        self.beat('warned','100')
        acknowledgment={'action':'acknowledge_scraping_warning','account':'warned','reviewed':True}
        self.assertEqual(self.call('/api/control',acknowledgment)[0],400)
        self.assertTrue(db.get_setting(self.conn,'instagram_scraping_warning'))
        self.assertEqual(self.call('/api/control',{'action':'pause','stage':'collection'})[0],200)
        self.assertEqual(self.call('/api/control',acknowledgment)[0],200)
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
        self.assertTrue(db.get_setting(self.conn,'paused_bios'))
        self.assertTrue(db.get_setting(self.conn,'instagram_collection_isolation'))
        self.beat('third','103')
        self.assertEqual(self.resume(accounts=[{'lane_id':'one','ig_id':'101'},{'lane_id':'third','ig_id':'103'}])[0],400)
        self.assertEqual(self.resume()[0],200)
        self.assertIsNone(db.get_setting(self.conn,'instagram_scraping_warning'))
        self.assertEqual(db.get_setting(self.conn,'instagram_collection_isolation')['accounts'],{'one':'101','two':'102'})
        self.assertFalse(accounts.request_permit(self.conn,'warned',kind='list')['granted'])

    def test_cooldowns_saved_for_same_identity_prevent_selection(self):
        for column in ('cooldown_until','list_cool_until','profile_cool_until'):
            with self.subTest(column=column):
                self.prepare()
                self.conn.execute(f"UPDATE account_identity_state SET {column}='2099-01-01T00:00:00+00:00' WHERE ig_id='101'")
                self.conn.commit()
                self.assertEqual(self.resume()[0],400)
                self.conn.execute(f'UPDATE account_identity_state SET {column}=NULL')
                self.conn.commit()

    def test_first_cooldown_heartbeat_stops_other_lane_before_error_report(self):
        for fields, column in (({'cooldown_until':'2099-01-01T00:00:00+00:00'},'cooldown_until'),
                               ({'cool':{'list':'2099-01-01T00:00:00+00:00'}},'list_cool_until'),
                               ({'cool':{'profile':'2099-01-01T00:00:00+00:00'}},'profile_cool_until')):
            with self.subTest(column=column):
                self.prepare();self.assertEqual(self.resume()[0],200)
                self.beat('one','101',**fields)
                self.assertIsNone(db.get_setting(self.conn,'instagram_collection_isolation'))
                self.assertTrue(db.get_setting(self.conn,'paused_lists'))
                self.assertTrue(db.get_setting(self.conn,'instagram_scraping_warning'))
                self.assertFalse(accounts.request_permit(self.conn,'two',kind='list')['granted'])
                saved=self.conn.execute(f"SELECT {column} FROM accounts WHERE lane_id='one'").fetchone()[0]
                self.assertTrue(saved.startswith('2099-01-01'))
                self.conn.execute(f'UPDATE accounts SET {column}=NULL')
                self.conn.execute('DELETE FROM account_identity_state')
                self.conn.commit()
