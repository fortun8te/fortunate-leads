import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import grok
import llm
import qualify


def item(h):
    return {'person': {'id': h, 'handle': h, 'name': h, 'bio': 'Founder of @x skincare brand', 'followers': 900}, 'tags': [], 'edges': []}


class GrokEngine(unittest.TestCase):
    def test_batch_size(self):
        with mock.patch.object(grok, 'available', return_value=True):
            self.assertEqual(qualify.batch_size('grok'), grok.BATCH)
            self.assertEqual(qualify.batch_size('free'), qualify.LLM_BATCH)
        with mock.patch.object(grok, 'available', return_value=False):
            self.assertEqual(qualify.batch_size('grok'), qualify.LLM_BATCH)

    def test_bulk_one_call_per_batch_and_rows_map_back(self):
        items = [item(f'h{i}') for i in range(grok.BATCH + 5)]
        calls = []

        def fake(system, user, schema, key, timeout=0):
            calls.append(user)
            n = user.count('\n')   # header + one line per person
            return {'r': [{'i': k, 'r': 'b', 'f': 82, 'n': -1, 'g': 'd', 'q': 'skincare brand'} for k in range(n)]}, {}
        with mock.patch.object(grok, 'available', return_value=True), mock.patch.object(grok, 'call', fake):
            vs = qualify.llm_verdicts(items, engine='grok')
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(v and v['role'] == 'buyer' and v['fit'] == 82 for v in vs))
        self.assertIn('@h0\t', calls[0])

    def test_bulk_rejects_unsupported_quotes_but_keeps_clear_nos(self):
        items = [item('a'), item('b')]

        def fake(*a, **k):
            return {'r': [{'i': 0, 'r': 'b', 'f': 90, 'n': -1, 'g': '', 'q': 'made up words'},
                          {'i': 1, 'r': 'u', 'f': 10, 'n': -1, 'g': '', 'q': ''}]}, {}
        with mock.patch.object(grok, 'available', return_value=True), mock.patch.object(grok, 'call', fake):
            vs = qualify.llm_verdicts(items, engine='grok')
        self.assertIsNone(vs[0])
        self.assertEqual(vs[1]['role'], 'unrelated')

    def test_auto_falls_back_to_grok(self):
        class Dead:
            def chat(self, *a, **k):
                raise llm.Unavailable('none')
        used = []
        with mock.patch.object(qualify, '_providers', return_value=Dead()), \
                mock.patch.object(grok, 'available', return_value=True), \
                mock.patch.object(grok, 'call', lambda *a, **k: used.append(1) or ({'r': []}, {})):
            qualify.llm_verdicts([item('a')], engine='auto')
        self.assertEqual(used, [1])

    def test_grok_failure_leaves_people_for_retry(self):
        def boom(*a, **k):
            raise grok.Failed('quota')
        with mock.patch.object(grok, 'available', return_value=True), mock.patch.object(grok, 'call', boom):
            self.assertEqual(qualify.llm_verdicts([item('a'), item('b')], engine='grok'), [None, None])

    def test_usage_log(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.object(grok, 'LOG', Path(d) / 'u.jsonl'):
            grok._log({'at': __import__('time').time(), 'in': 100, 'out': 20, 'cached': 80})
            u = grok.usage()
        self.assertEqual((u['calls'], u['tokens_in'], u['tokens_cached'], u['tokens_out']), (1, 100, 80, 20))


class Caps(unittest.TestCase):
    def test_agency_cannot_outrank_a_buyer(self):
        net = {'lists': 12, 'seeds': [('a', 'following')] * 3}
        self.assertLessEqual(qualify.blend(80, net, 'connector'), qualify.SCORE_CAP['connector'])
        self.assertGreater(qualify.blend(80, net, 'buyer'), qualify.SCORE_CAP['connector'])

    def test_following_a_celebrity_is_no_way_in(self):
        self.assertTrue(qualify.too_big(900000, {'me': 'followed'}))
        self.assertFalse(qualify.too_big(900000, {'me': 'follows'}))
        self.assertLessEqual(qualify.blend(90, {'lists': 14}, 'buyer', blocked=True), qualify.BLOCKED_CAP)


if __name__ == '__main__':
    unittest.main()
