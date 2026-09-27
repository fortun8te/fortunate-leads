"""Exercise the extension API sequence between saved list checkpoints."""
import db
import test_server_accounts as lanes


class FairnessIntegrationTest(lanes.Base):
    nxt = lanes.LaneTest.nxt
    post = lanes.LaneTest.post
    seeds = lanes.LaneTest.seeds
    page = lanes.LaneTest.page

    def test_four_saved_pages_yield_to_bio_then_other_list(self):
        self.seeds('large','small',direction='following')
        self.conn.execute("UPDATE jobs SET priority=200 WHERE seed='large'")
        self.conn.execute("UPDATE jobs SET priority=100 WHERE seed='small'")
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','bio',1)")
        self.conn.commit()
        for page in range(1,5):
            job = self.nxt('a')['job']
            self.assertEqual(job['seed'],'large')
            self.assertEqual(job['cursor'], str(page-1) if page>1 else None)
            self.assertEqual(self.page('a',job,1,str(page))[0],200)
        bio = self.nxt('a')['job']
        self.assertEqual((bio['kind'],bio['handle']),('profile','bio'))
        self.assertEqual(self.post('a','/api/ext/profile', {'job_id':bio['id'], 'route':'profile_page',
                         'event_id':'bio-result', 'profile':{'ig_id':'9911','handle':'bio','bio':'Saved'}})[0],200)
        self.assertEqual(self.nxt('a')['job']['seed'],'small')
        checkpoint=self.conn.execute("SELECT cursor,received,lane FROM lists WHERE seed='large'").fetchone()
        self.assertEqual(tuple(checkpoint),('4',4,'lane-a'))
