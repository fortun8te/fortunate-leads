import os; os.environ.setdefault('FL_NO_ORSLOT', '1')  # tests never see the real key pool
"""Staged qualifier: network signals in the prefilter and packet, batched LLM verdicts with rubric, evidence and few-shot."""
import json
import re
import sys
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import qualify as q  # noqa: E402


def P(handle, bio=None, name=None, **kw):
    return dict({'handle': handle, 'bio': bio, 'name': name}, **kw)


class Proxy(BaseHTTPRequestHandler):
    bodies = []
    drop = set()   # ids the fake model "forgets" in a batch reply

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Proxy.bodies.append(body)
        user = body['messages'][1]['content']
        ids = [int(x) for x in re.findall(r'### id=(\d+)', user)]
        handles = re.findall(r'### id=\d+\n@([^\s·]+)', user)
        res = [{'id': i, 'handle': handles[i], 'role': 'buyer', 'fit': 82, 'niche': 'Skincare', 'brand_handle': '@glowco', 'decision_maker': True,
                'evidence': ['founder of glow', 'invented quote'], 'reason': 'Runs a skincare brand.'} for i in ids if i not in Proxy.drop]
        content = json.dumps({'results': res} if len(ids) > 1 else res[0] if res else {})
        data = json.dumps({'model': body['model'], 'choices': [{'message': {'content': content}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.pool = q.llm.Providers(keys=[], proxy=f'http://127.0.0.1:{self.httpd.server_address[1]}/api/v1/chat/completions')
        self.provider_patch = patch.object(q.llm, 'get', return_value=self.pool)
        self.provider_patch.start()
        Proxy.bodies, Proxy.drop = [], set()

    def tearDown(self):
        self.provider_patch.stop()
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_network_raises_prefilter(self):
        person = P('jan.devries', name='Jan')
        plain = q.prefilter(person, ['a'])
        net = {'seeds': [('a', 'following'), ('b', 'following')], 'lists': 2, 'me': 'mutual', 'seed_yield': 0.6, 'seed_marked': 10,
               'client_seeds': 2}
        self.assertGreater(q.prefilter(person, ['a', 'b'], net), plain + 30)
        low = dict(net, lists=1, seed_yield=0.05, me=None, client_seeds=0, seeds=[('a', 'followers')])
        self.assertLess(q.prefilter(person, ['a'], low), plain)       # a low-yield seed drags down
        self.assertLessEqual(q.prefilter(dict(person, is_private=1), ['a'], net), 35)
        self.assertEqual(q.prefilter(person, ['a'], None, 100), round(0.75 * plain + 25))   # Laya: one soft weighted signal

    def test_batched_verdicts_with_rubric_fewshot_and_evidence(self):
        items = [{'person': P(f'glow{i}', 'Founder of Glow skincare brand @glowco, ships to the USA'), 'tags': [('Founder', 'signal')], 'edges': [],
                  'net': {'seeds': [('seedx', 'following')], 'lists': 1, 'me': 'follows', 'seed_yield': 0.5, 'seed_marked': 6,
                          'client_seeds': 1}} for i in range(q.LLM_BATCH + 2)]
        ex = [{'handle': 'goodbrand', 'bio': 'Founder @goodbrand candles', 'label': 'good'},
              {'handle': 'nope', 'bio': 'UGC creator for brands', 'label': 'no'}]
        Proxy.drop = {1}
        out = q.llm_verdicts(items, ex, models=('m/a:free',))
        self.assertEqual(len(Proxy.bodies), 2)               # one full batch + 2
        sysmsg, user = Proxy.bodies[0]['messages'][0]['content'], Proxy.bodies[0]['messages'][1]['content']
        self.assertIn('Marked Interested', sysmsg)
        self.assertIn('@goodbrand', sysmsg)
        self.assertIn('Scoring rubric', sysmsg)
        self.assertIn('Bios may be in Dutch', sysmsg)
        self.assertIn('Followed BY these operators', user)
        self.assertIn('follows Michael', user)
        self.assertIn('marked good/client', user)
        self.assertEqual(Proxy.bodies[0]['response_format'], {'type': 'json_object'})
        self.assertIsNone(out[1])                            # missing from the batch reply: only that person falls back
        v = out[0]
        self.assertEqual((v['role'], v['fit'], v['brand_handle']), ('buyer', 82, '@glowco'))
        self.assertEqual(v['evidence'], ['founder of glow'])  # invented quotes are dropped
        self.assertIn(('Fit: strong', 'signal'), v['tags'])
        self.assertIn(('Skincare', 'niche'), v['tags'])
        self.assertEqual(v['prompt'], q.prompt_version(ex))
        self.assertNotEqual(q.prompt_version(ex), q.prompt_version(ex[:1]))
        self.assertNotEqual(q.prompt_version(ex), q.prompt_version([dict(ex[0], bio='Changed bio'), ex[1]]))
        self.assertEqual(q._evidence({'evidence': ['External Cat', 'founder of glow']},
                                     dict(items[0]['person'], category='External Cat')), ['founder of glow'])

    def test_single_verdict_still_works_and_down_is_none(self):
        v = q.llm_verdict(P('glow', 'Founder of Glow skincare brand @glowco'), [], [], models=('m/a:free',))
        self.assertEqual(v['role'], 'buyer')
        self.pool.proxy = 'http://127.0.0.1:9/none'
        self.assertIsNone(q.llm_verdict(P('x', 'bio'), [], [], timeout=1, models=('m/a:free',)))


if __name__ == '__main__':
    unittest.main()
