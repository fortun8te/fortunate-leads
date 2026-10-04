"""The seed workflow: reading pasted text, atomic idempotent adds, holds, undo and the plain-words listing."""
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import Base, db, server  # noqa: E402

import accounts  # noqa: E402
import seeds  # noqa: E402
from storage.checkpoints import repair_lists  # noqa: E402

LANE = '/api/ext/next?lane=lane-a&ig_id=101&handle=acct.a'


class SeedBase(Base):
    def batch(self, text, directions=('followers', 'following'), request_id=None):
        body = {'text': text, 'directions': list(directions)}
        if request_id:
            body['request_id'] = request_id
        return self.call('/api/seeds/batch', body)

    def act(self, action, handles, **extra):
        return self.call('/api/seeds/act', dict({'action': action, 'handles': handles}, **extra))

    def listing(self, query=''):
        return self.call('/api/seeds' + (('?' + query) if query else ''))[1]

    def item(self, handle):
        return next(i for i in self.listing('status=all&limit=500')['items'] + self.listing('status=removed')['items']
                    if i['handle'] == handle)

    def rows(self, sql, *args):
        return [tuple(r) for r in self.conn.execute(sql, args)]

    def lists(self, seed):
        return self.rows('SELECT direction,state,hold FROM lists WHERE seed=? ORDER BY direction', seed)

    def fetch_job(self, lane='lane-a', ig='101', handle='acct.a'):
        return self.call(f'/api/ext/next?lane={lane}&ig_id={ig}&handle={handle}')[1]['job']


class ReadingPastedText(SeedBase):
    def test_every_separator_and_link_shape_is_one_handle(self):
        text = '@Alpha, https://www.instagram.com/beta/?hl=en;gamma\n  instagram.com/delta#x  (@epsilon) "zeta"'
        out = self.call('/api/seeds/check', {'text': text})[1]
        self.assertEqual([i['handle'] for i in out['items']], ['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta'])
        self.assertEqual(out['summary']['ready'], 6)

    def test_bad_input_says_why_and_never_blocks_the_good_input(self):
        out = self.call('/api/seeds/check', {'text': 'good https://instagram.com/p/Cxyz/ https://evil.test/x '
                                                    'thirty_one_characters_long_name_xx a!b alice~123 good'})[1]
        by = [(i['input'], i['state'], i['reason']) for i in out['items']]
        self.assertEqual(by[0], ('good', 'new', None))
        self.assertEqual([r for _, s, r in by[1:] if s == 'invalid'], ['post_link', 'not_instagram', 'too_long', 'not_valid', 'not_valid'])
        self.assertEqual(by[-1][1:], ('duplicate', 'duplicate'))
        self.assertTrue(all(i['message'] for i in out['items'] if i['state'] in ('invalid', 'duplicate')))
        self.assertEqual(out['summary'], dict(ready=1, already=0, duplicate=1, invalid=5, renamed=0, private=0, restore=0))

    def test_checking_changes_nothing(self):
        self.call('/api/seeds/check', {'text': 'one two three'})
        self.assertEqual(self.rows('SELECT count(*) FROM seeds'), [(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM jobs'), [(0,)])

    def test_check_flags_tracked_collected_private_and_your_own(self):
        self.batch('tracked', ['following'])
        self.conn.execute("INSERT INTO lists(seed,direction,state,updated_at) VALUES('finished','following','done',?)", (db.now(),))
        self.conn.execute("INSERT INTO seeds(handle,added_at,directions) VALUES('finished',?,'[\"following\"]')", (db.now(),))
        self.conn.execute("INSERT INTO people(handle,is_private,first_seen,updated_at) VALUES('closed',1,?,?)", (db.now(), db.now()))
        self.conn.execute("INSERT INTO accounts(lane_id,handle,first_seen) VALUES('l','mine',?)", (db.now(),))
        self.conn.commit()
        out = self.call('/api/seeds/check', {'text': 'tracked finished closed mine', 'directions': ['following']})[1]['items']
        self.assertEqual([(i['handle'], i['state'], i['reason']) for i in out],
                         [('tracked', 'exists', 'tracked'), ('finished', 'exists', 'collected'), ('closed', 'new', None), ('mine', 'new', None)])
        self.assertEqual([i['warn'] for i in out], [[], [], ['private'], ['yours']])

    def test_a_second_list_on_a_tracked_seed_is_an_extension(self):
        self.batch('brand', ['following'])
        item = self.call('/api/seeds/check', {'text': 'brand', 'directions': ['followers', 'following']})[1]['items'][0]
        self.assertEqual((item['state'], item['add']), ('extend', ['followers']))


class AddingIsAtomicAndIdempotent(SeedBase):
    def test_directions_are_kept_and_the_repair_job_does_not_add_the_other_one(self):
        out = self.batch('Brand, https://instagram.com/founder/', ['following'])[1]
        self.assertEqual([i['result'] for i in out['items']], ['added', 'added'])
        self.assertEqual(self.lists('brand'), [('following', 'queued', None)])
        self.assertEqual(self.rows('SELECT directions FROM seeds WHERE handle=?', 'brand'), [('["following"]',)])
        repair_lists(self.conn)
        self.conn.commit()
        self.assertEqual(self.lists('brand'), [('following', 'queued', None)])   # followers never appear on their own
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='list'"), [(2,)])

    def test_legacy_seeds_without_a_choice_still_get_both_lists(self):
        db.queue_list(self.conn, 'old', 'following')
        self.conn.commit()
        repair_lists(self.conn)
        self.conn.commit()
        self.assertEqual([d for d, *_ in self.lists('old')], ['followers', 'following'])

    def test_the_same_request_id_is_answered_once(self):
        first = self.batch('alpha beta', ['following'], request_id='req-aaaaaaaa')[1]
        again = self.batch('alpha beta gamma', ['following'], request_id='req-aaaaaaaa')[1]
        self.assertTrue(again['replayed'])
        self.assertEqual(again['items'], first['items'])
        self.assertEqual(self.rows('SELECT count(*) FROM seeds'), [(2,)])   # the changed second paste was ignored

    def test_without_a_request_id_repeating_is_still_harmless(self):
        self.batch('alpha', ['following'])
        out = self.batch('alpha', ['following'])[1]
        self.assertEqual(out['items'][0]['result'], 'exists')
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='list'"), [(1,)])

    def test_one_failing_item_rolls_back_alone_and_the_rest_land(self):
        self.conn.execute("INSERT INTO jobs(kind,seed,direction,collection_backend) VALUES('list','mobile1','following','mobile')")
        self.conn.commit()
        out = self.batch('mobile1 fine', ['following'])[1]
        self.assertEqual([(i['handle'], i['result']) for i in out['items']], [('mobile1', 'failed'), ('fine', 'added')])
        self.assertEqual(self.rows('SELECT handle FROM seeds ORDER BY handle'), [('fine',)])

    def test_limits_and_bad_requests_change_nothing(self):
        self.assertEqual(self.batch('x ' * 501)[0], 400)
        self.assertEqual(self.batch('x', [])[0], 400)
        self.assertEqual(self.batch('x', ['sideways'])[0], 400)
        self.assertEqual(self.batch('x', request_id='short')[0], 400)
        self.assertEqual(self.call('/api/seeds/batch', {'directions': ['following']})[0], 400)
        self.assertEqual(self.rows('SELECT count(*) FROM seeds'), [(0,)])

    def test_a_pasted_old_name_adds_the_current_seed_and_cached_identity_is_kept(self):
        self.conn.execute("INSERT INTO people(handle,ig_id,name,first_seen,updated_at) VALUES('newname','777','Ann',?,?)", (db.now(), db.now()))
        self.conn.commit()
        self.batch('newname', ['following'])
        self.assertEqual(self.rows('SELECT ig_id FROM seeds WHERE handle=?', 'newname'), [('777',)])
        self.conn.execute("INSERT INTO seed_aliases VALUES('oldname','newname','777',?)", (db.now(),))
        self.conn.commit()
        item = self.call('/api/seeds/check', {'text': 'oldname', 'directions': ['following']})[1]['items'][0]
        self.assertEqual((item['handle'], item['renamed_from'], item['state']), ('newname', 'oldname', 'exists'))
        # a name someone else holds now is just that person, not an alias
        self.conn.execute("INSERT INTO people(handle,first_seen,updated_at) VALUES('oldname',?,?)", (db.now(), db.now()))
        self.conn.commit()
        item = self.call('/api/seeds/check', {'text': 'oldname', 'directions': ['following']})[1]['items'][0]
        self.assertEqual((item['handle'], item['renamed_from']), ('oldname', None))

    def test_a_real_rename_during_collection_leaves_the_old_name_findable(self):
        self.batch('before', ['following'])
        self.conn.execute("UPDATE seeds SET ig_id='55' WHERE handle='before'")
        self.conn.commit()
        db.rename_seed(self.conn, 'before', 'after', '55')
        self.conn.commit()
        self.assertEqual(self.rows('SELECT old_handle,new_handle FROM seed_aliases'), [('before', 'after')])
        item = self.call('/api/seeds/check', {'text': 'before', 'directions': ['following']})[1]['items'][0]
        self.assertEqual((item['handle'], item['state']), ('after', 'exists'))
        self.assertEqual(self.item('after')['renamed_from'][0]['from'], 'before')


class HoldsRemovalAndUndo(SeedBase):
    def setUp(self):
        super().setUp()
        self.call('/api/ext/heartbeat', {'lane_id': 'lane-a', 'version': '3.9.30', 'state': 'idle',
                                         'account': {'ig_id': '101', 'handle': 'acct.a'}})

    def test_pausing_a_waiting_list_keeps_it_out_of_the_queue_until_resumed(self):
        self.batch('brand', ['following'])
        self.assertEqual(self.act('pause', ['brand'])[1]['items'], [{'handle': 'brand', 'result': 'ok'}])
        self.assertEqual(self.lists('brand'), [('following', 'paused', 'operator')])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='list'"), [('cancelled',)])
        self.assertIsNone(self.fetch_job())
        repair_lists(self.conn); self.conn.commit()
        self.assertEqual(self.lists('brand'), [('following', 'paused', 'operator')])       # repair never undoes a pause
        self.assertFalse(db.queue_list(self.conn, 'brand', 'following'))                     # nor does any other queue path
        self.conn.commit()
        self.assertEqual(self.act('resume', ['brand'])[1]['items'][0]['result'], 'ok')
        self.assertEqual(self.lists('brand'), [('following', 'queued', None)])
        self.assertEqual(self.fetch_job()['seed'], 'brand')

    def test_a_pause_asked_for_mid_request_lands_between_pages(self):
        self.batch('brand', ['following'])
        job = self.fetch_job()
        self.assertEqual(self.act('pause', ['brand'])[1]['items'][0]['result'], 'ok')
        self.assertEqual(self.item('brand')['lists']['following']['status'], 'pausing')
        users = [{'ig_id': str(900 + i), 'handle': f'p{i}'} for i in range(3)]
        out = self.page(job, users, cursor='c1', lane_id='lane-a', account={'ig_id': '101', 'handle': 'acct.a'})[1]
        self.assertEqual(out['received'], 3)                                                  # the page that was already on its way is saved
        self.assertEqual(self.lists('brand'), [('following', 'paused', 'operator')])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='list'"), [('cancelled',)])
        self.assertIsNone(self.fetch_job())
        self.act('resume', ['brand'])
        again = self.fetch_job()
        self.assertEqual((again['seed'], again['cursor'], again['received']), ('brand', 'c1', 3))   # continues from the saved cursor

    def test_pick_job_skips_a_held_list_even_if_something_else_queued_a_job(self):
        self.batch('brand other', ['following'])
        self.conn.execute("UPDATE lists SET hold='operator' WHERE seed='brand'")
        self.conn.commit()
        self.assertEqual(self.fetch_job()['seed'], 'other')

    def test_removing_hides_stops_and_keeps_history_and_restoring_brings_it_back(self):
        self.batch('brand', ['following'])
        job = self.fetch_job()
        self.page(job, [{'ig_id': '900', 'handle': 'p0'}], cursor='c1', lane_id='lane-a', account={'ig_id': '101', 'handle': 'acct.a'})
        self.assertEqual(self.act('remove', ['brand'])[1]['items'][0]['result'], 'ok')
        self.assertEqual(self.rows('SELECT count(*) FROM edges WHERE seed=?', 'brand'), [(1,)])      # saved people stay
        self.assertEqual(self.rows('SELECT count(*) FROM people WHERE handle=?', 'p0'), [(1,)])
        self.assertNotIn('brand', [i['handle'] for i in self.listing()['items']])
        self.assertEqual(self.listing()['counts']['removed'], 1)
        self.assertIsNone(self.fetch_job())
        self.assertFalse(db.queue_list(self.conn, 'brand', 'following'))                      # the old queue paths cannot revive it
        self.conn.commit()
        self.assertEqual(self.act('restore', ['brand'])[1]['items'][0]['result'], 'ok')
        self.assertEqual(self.fetch_job()['cursor'], 'c1')

    def test_adding_a_removed_seed_again_restores_it_with_its_progress(self):
        self.batch('brand', ['following'])
        self.act('remove', ['brand'])
        item = self.call('/api/seeds/check', {'text': 'brand', 'directions': ['following']})[1]['items'][0]
        self.assertEqual(item['state'], 'restore')
        out = self.batch('brand', ['following'])[1]
        self.assertEqual(out['items'][0]['result'], 'restored')
        self.assertEqual(self.lists('brand'), [('following', 'queued', None)])

    def test_a_list_paused_before_removal_stays_paused_after_restore(self):
        self.batch('brand', ['following'])
        self.act('pause', ['brand'])
        self.act('remove', ['brand'])
        self.act('restore', ['brand'])
        self.assertEqual(self.lists('brand'), [('following', 'paused', 'operator')])

    def test_do_next_moves_a_seed_ahead_without_touching_the_rest(self):
        self.batch('first second third', ['following'])
        self.act('next', ['third'])
        self.assertEqual(self.fetch_job()['seed'], 'third')

    def test_turning_a_list_on_or_off_per_seed(self):
        self.batch('brand', ['following'])
        self.assertEqual(self.act('directions', ['brand'], directions=['followers', 'following'])[1]['items'][0]['result'], 'ok')
        self.assertEqual([d for d, *_ in self.lists('brand')], ['followers', 'following'])
        self.act('directions', ['brand'], directions=['followers'])
        self.assertEqual([d for d, *_ in self.lists('brand')], ['followers'])                  # never started: removed, not kept
        self.assertEqual(self.act('directions', ['brand'], directions=[])[0], 400)

    def test_dropping_a_list_that_already_fetched_pauses_it_instead(self):
        self.batch('brand', ['followers', 'following'])
        self.conn.execute("UPDATE lists SET cursor='c9',received=40 WHERE seed='brand' AND direction='followers'")
        self.conn.commit()
        self.act('directions', ['brand'], directions=['following'])
        self.assertEqual(self.lists('brand'), [('followers', 'paused', 'direction_off'), ('following', 'queued', None)])
        self.assertEqual(self.item('brand')['lists']['followers']['status'], 'off')
        repair_lists(self.conn); self.conn.commit()
        self.assertEqual(self.lists('brand')[0], ('followers', 'paused', 'direction_off'))

    def test_bulk_actions_report_each_handle(self):
        self.batch('a1 b1', ['following'])
        out = self.act('pause', ['a1', 'ghost', 'b1', 'a1'])[1]['items']
        self.assertEqual([(i['handle'], i['result']) for i in out], [('a1', 'ok'), ('ghost', 'missing'), ('b1', 'ok')])
        self.assertEqual(self.act('pause', ['a1'])[1]['items'][0]['result'], 'noop')
        self.assertEqual(self.act('explode', ['a1'])[0], 400)
        self.assertEqual(self.act('pause', [])[0], 400)

    def test_undo_deletes_what_never_started_and_pauses_what_did(self):
        out = self.batch('fresh started', ['following'], request_id='undo-aaaaaaaa')[1]
        self.conn.execute("UPDATE lists SET cursor='c1',received=25 WHERE seed='started'")
        self.conn.commit()
        undone = self.call('/api/seeds/undo', {'request_id': 'undo-aaaaaaaa'})[1]
        self.assertEqual({i['handle']: i['result'] for i in undone['items']}, {'fresh': 'removed', 'started': 'paused'})
        self.assertEqual(self.rows('SELECT handle FROM seeds'), [('started',)])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE seed='fresh'"), [(0,)])
        self.assertEqual(self.lists('started'), [('following', 'paused', 'operator')])
        self.assertTrue(self.call('/api/seeds/undo', {'request_id': 'undo-aaaaaaaa'})[1]['replayed'])
        self.assertEqual(self.call('/api/seeds/undo', {'request_id': 'never-seen-aaa'})[0], 404)

    def test_undo_does_not_touch_seeds_that_were_already_there(self):
        self.batch('keep', ['following'])
        self.batch('keep fresh', ['following'], request_id='undo-bbbbbbbb')
        self.call('/api/seeds/undo', {'request_id': 'undo-bbbbbbbb'})
        self.assertEqual(self.rows('SELECT handle FROM seeds'), [('keep',)])

    def test_undoing_an_extension_drops_only_the_added_list(self):
        self.batch('brand', ['following'])
        self.batch('brand', ['followers', 'following'], request_id='undo-cccccccc')
        self.assertEqual([d for d, *_ in self.lists('brand')], ['followers', 'following'])
        self.call('/api/seeds/undo', {'request_id': 'undo-cccccccc'})
        self.assertEqual([d for d, *_ in self.lists('brand')], ['following'])
        self.assertEqual(self.rows('SELECT directions FROM seeds'), [('["following"]',)])


class TheListing(SeedBase):
    def get_raw(self, path, etag=None):
        port = server.CFG['port']
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}')
        req.add_header('Host', f'127.0.0.1:{port}')
        if etag:
            req.add_header('If-None-Match', etag)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get('ETag'), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get('ETag'), e.read()

    def test_an_unchanged_list_costs_a_304_and_any_change_breaks_the_etag(self):
        self.batch('alpha beta', ['following'])
        status, etag, body = self.get_raw('/api/seeds')
        self.assertEqual(status, 200)
        self.assertTrue(etag.startswith('"'))
        self.assertEqual(self.get_raw('/api/seeds', etag)[0], 304)
        self.act('pause', ['alpha'])
        status, newer, _ = self.get_raw('/api/seeds', etag)
        self.assertEqual(status, 200)
        self.assertNotEqual(newer, etag)

    def test_filters_counts_search_and_stable_newest_first_order(self):
        for index, name in enumerate(['alpha', 'beta', 'gamma']):
            self.batch(name, ['following'])
            self.conn.execute('UPDATE seeds SET added_at=? WHERE handle=?', (f'2026-10-0{index + 1}T00:00:00+00:00', name))
            self.conn.commit()
        self.conn.execute("UPDATE lists SET state='partial',error='Instagram limited this list; coverage is partial.' WHERE seed='gamma'")
        self.conn.commit()
        self.act('pause', ['beta'])
        everything = self.listing()
        self.assertEqual([i['handle'] for i in everything['items']], ['gamma', 'beta', 'alpha'])
        self.assertEqual(everything['counts'], dict(all=3, collecting=0, waiting=1, paused=1, attention=0, done=1, removed=0))
        self.assertEqual([i['handle'] for i in self.listing('status=paused')['items']], ['beta'])
        self.assertEqual([i['handle'] for i in self.listing('q=%40ALP')['items']], ['alpha'])
        self.assertEqual([i['handle'] for i in self.listing('sort=name')['items']], ['alpha', 'beta', 'gamma'])
        page = self.listing('limit=2&offset=2')
        self.assertEqual((page['total'], [i['handle'] for i in page['items']]), (3, ['alpha']))
        self.assertEqual(self.call('/api/seeds?status=nope')[0], 400)

    def test_private_capped_and_unreadable_lists_get_plain_words_and_an_attention_flag(self):
        self.batch('closed', ['following'])
        self.batch('capped', ['followers'])
        self.batch('broken', ['following'])
        ts = db.now()
        self.conn.execute("UPDATE lists SET state='private' WHERE seed='closed'")
        self.conn.execute("UPDATE lists SET state='partial',error='Instagram limited this list; coverage is partial.',received=50 WHERE seed='capped'")
        self.conn.execute("UPDATE lists SET state='error',error='Instagram returned its home page instead of list data.' WHERE seed='broken'")
        self.conn.commit()
        by = {i['handle']: i for i in self.listing()['items']}
        self.assertEqual((by['closed']['label'], by['closed']['attention']), ('Private', True))
        self.assertIn('follows @closed', by['closed']['lists']['following']['note'])
        self.assertEqual((by['capped']['label'], by['capped']['attention']), ('Capped by Instagram', False))
        self.assertEqual((by['broken']['label'], by['broken']['attention']), ("Couldn't read", True))
        self.assertEqual(self.listing()['counts']['attention'], 2)

    def test_progress_matches_the_collection_numbers_the_scraper_page_shows(self):
        self.batch('brand', ['following'])
        job = self.fetch_job_for_progress()
        self.page(job, [{'ig_id': str(800 + i), 'handle': f'q{i}'} for i in range(3)], cursor='c1', total=10, lane_id='lane-a',
                  account={'ig_id': '101', 'handle': 'acct.a'})
        row = self.item('brand')['lists']['following']
        scraper = next(l for l in self.call('/api/scraper')[1]['lists'] if l['seed'] == 'brand')
        self.assertEqual((row['saved'], row['expected']), (scraper['saved_entries'], scraper['expected']))
        self.assertEqual(row['saved'], 3)

    def fetch_job_for_progress(self):
        self.call('/api/ext/heartbeat', {'lane_id': 'lane-a', 'version': '3.9.30', 'state': 'idle', 'account': {'ig_id': '101', 'handle': 'acct.a'}})
        return self.fetch_job()

    def test_auto_discovered_seeds_only_show_the_list_they_actually_have(self):
        db.queue_list(self.conn, 'found', 'following', priority=-10)
        self.conn.execute("INSERT INTO collection_discovery(handle,state,at,reason,directions) VALUES('found','queued',?,'x','[\"following\"]')", (db.now(),))
        self.conn.commit()
        item = self.item('found')
        self.assertEqual((item['source'], item['lists']['followers']['status']), ('suggested', 'off'))
        self.assertEqual(self.call('/api/seeds/check', {'text': 'found', 'directions': ['following']})[1]['items'][0]['state'], 'exists')
        self.assertEqual(self.call('/api/seeds/check', {'text': 'found', 'directions': ['followers']})[1]['items'][0]['state'], 'extend')

    def test_following_only_scope_marks_follower_lists_off(self):
        self.batch('brand', ['followers', 'following'])
        db.set_setting(self.conn, 'follower_lists', False)
        self.conn.commit()
        item = self.listing()
        self.assertEqual(item['scope'], 'following')
        lists = item['items'][0]['lists']
        self.assertEqual((lists['followers']['status'], lists['followers']['wanted']), ('off', False))
        self.assertEqual(item['items'][0]['status'], 'waiting')


class DisconnectingAnAccount(SeedBase):
    def setUp(self):
        super().setUp()
        self.call('/api/ext/heartbeat', {'lane_id': 'lane-a', 'version': '3.9.30', 'state': 'idle', 'account': {'ig_id': '101', 'handle': 'acct.a'}})

    def test_a_disconnected_account_gets_no_work_even_after_it_checks_in_again(self):
        self.batch('brand', ['following'])
        card = lambda: next(c for c in self.call('/api/accounts/live')[1]['cards'] if c['lane_id'] == 'lane-a')
        self.assertEqual(self.call('/api/accounts/lane-a/disconnect', {})[0], 200)
        self.assertEqual((card()['state'], card()['action']['id']), ('disconnected', 'reconnect'))
        self.call('/api/ext/heartbeat', {'lane_id': 'lane-a', 'version': '3.9.30', 'state': 'idle', 'account': {'ig_id': '101', 'handle': 'acct.a'}})
        self.assertEqual(card()['state'], 'disconnected')          # the extension's next check-in does not bring it back
        self.assertIsNone(self.fetch_job())
        self.assertEqual(self.call('/api/accounts/lane-a', {'paused': False})[0], 400)
        self.call('/api/start', {'handles': ['brand'], 'directions': ['following']})
        self.assertEqual(card()['state'], 'disconnected')          # Start collection does not wake it either
        self.assertEqual(self.call('/api/accounts/lane-a/reconnect', {})[0], 200)
        self.assertNotEqual(card()['state'], 'disconnected')

    def test_forgetting_waits_until_the_profile_stops_checking_in(self):
        self.call('/api/accounts/lane-a/disconnect', {})
        status, body = self.call('/api/accounts/lane-a/forget', {})
        self.assertEqual(status, 400)
        self.assertIn('still sending updates', body['error'])
        self.conn.execute("UPDATE accounts SET last_seen='2026-01-01T00:00:00+00:00' WHERE lane_id='lane-a'")
        self.conn.commit()
        self.assertEqual(self.call('/api/accounts/lane-a/forget', {})[0], 200)
        self.assertEqual(self.rows('SELECT count(*) FROM accounts'), [(0,)])

    def test_an_account_that_is_not_disconnected_cannot_be_forgotten(self):
        self.assertEqual(self.call('/api/accounts/lane-a/forget', {})[0], 400)
        self.assertEqual(self.call('/api/accounts/nobody/disconnect', {})[0], 404)


if __name__ == '__main__':
    unittest.main()
