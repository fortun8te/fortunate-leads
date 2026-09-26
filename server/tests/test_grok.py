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

    def test_grok_engine_sends_one_call_of_25(self):
        items = [item(f'h{i}') for i in range(25)]
        calls = []

        def fake(msgs, timeout=0):
            calls.append(msgs)
            return '{"results": []}', grok.MODEL
        with mock.patch.object(grok, 'available', return_value=True), mock.patch.object(grok, 'chat', fake):
            qualify.llm_verdicts(items, engine='grok')
        self.assertEqual(len(calls), 1)

    def test_auto_falls_back_to_grok(self):
        class Dead:
            def chat(self, *a, **k):
                raise llm.Unavailable('none')
        used = []
        with mock.patch.object(qualify, '_providers', return_value=Dead()), \
                mock.patch.object(grok, 'available', return_value=True), \
                mock.patch.object(grok, 'chat', lambda m, timeout=0: used.append(1) or ('{"results": []}', grok.MODEL)):
            qualify.llm_verdicts([item('a')], engine='auto')
        self.assertEqual(used, [1])

    def test_grok_failure_leaves_people_for_retry(self):
        def boom(m, timeout=0):
            raise grok.Failed('quota')
        with mock.patch.object(grok, 'available', return_value=True), mock.patch.object(grok, 'chat', boom):
            self.assertEqual(qualify.llm_verdicts([item('a'), item('b')], engine='grok'), [None, None])


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
