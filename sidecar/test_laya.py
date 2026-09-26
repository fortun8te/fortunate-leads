"""HTTP-layer tests with a stubbed model: no torch, no download.  Run: python -m unittest sidecar/test_laya.py"""
import json
import io
import os
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import laya_server as ls  # noqa: E402


class StubBackend:
    model = "stub"
    device = "cpu"

    def __init__(self):
        self.calls = []

    def predict_batch(self, states, questions):
        self.calls.append((states, questions))
        out = []
        for s in states:
            ans = {}
            for k, q in questions.items():
                if q["type"] == "noul":
                    ans[k] = {"type": "noul", "noul": 0.9 if "founder" in s.lower() else 0.1,
                              "confidence": 0.9, "answer_confidence": 0.8}
                else:
                    labs = q["criteria"]
                    probs = {l: (0.7 if i == 1 else 0.3 / (len(labs) - 1)) for i, l in enumerate(labs)}
                    ans[k] = {"type": "choice", "choice": labs[1], "probabilities": probs,
                              "confidence": 0.7, "answer_confidence": 0.6}
            out.append({"answers": ans})
        return out


class HandlerHarness:
    @classmethod
    def setUpClass(cls):
        cls.backend = StubBackend()
        cls.handler = ls.make_handler(cls.backend, ls.load_default_questions())

    def req(self, path, body=None):
        obj = self.handler.__new__(self.handler)
        obj.path = path
        obj.client_address = ('127.0.0.1', 1)
        data = json.dumps(body).encode() if body is not None else b''
        obj.headers = {'Content-Length': str(len(data))}
        obj.rfile = io.BytesIO(data)
        sent = []
        obj._send = lambda code, payload: sent.append((code, payload))
        if body is None:
            obj.do_GET()
        else:
            obj.do_POST()
        return sent[0]


class HttpTest(HandlerHarness, unittest.TestCase):
    def test_health(self):
        self.assertEqual(self.req("/health"), (200, {"ok": True, "model": "stub", "device": "cpu",
                                                     "deployment_version": ls.DEPLOYMENT_VERSION,
                                                     "questions_signature": ls.question_signature(ls.load_default_questions())}))

    def test_decide_shapes(self):
        code, body = self.req("/decide", {
            "items": [{"id": "a", "text": "Founder of a skincare brand"}, {"id": 2, "text": "gym selfies"}],
            "questions": [{"key": "dtc", "q": "Founder?"},
                          {"key": "size", "q": "Size?", "labels": ["s", "m", "l"]},
                          {"key": "niche", "q": "Niche?", "labels": ["x", "y"], "multi": True}]})
        self.assertEqual(code, 200)
        r = body["results"]
        self.assertEqual([x["id"] for x in r], ["a", 2])
        self.assertAlmostEqual(r[0]["answers"]["dtc"]["p"], 0.9)
        self.assertAlmostEqual(r[1]["answers"]["dtc"]["p"], 0.1)
        self.assertEqual(r[0]["answers"]["size"]["label"], "m")
        self.assertAlmostEqual(r[0]["answers"]["size"]["p"], 0.7)
        self.assertEqual(set(r[0]["answers"]["niche"]["probs"]), {"x", "y"})
        # one batched model call with 2 states; multi expanded to per-label noul
        states, qs = self.backend.calls[-1]
        self.assertEqual(len(states), 2)
        self.assertEqual(qs["niche__0"]["type"], "noul")
        self.assertEqual(qs["size"]["type"], "choice")

    def test_default_questions_and_person(self):
        code, body = self.req("/decide", {"items": [{"id": "p", "person": {
            "handle": "glowco", "name": "Ana", "bio": "Founder @glowco", "followers": 1200}}]})
        self.assertEqual(code, 200)
        keys = {q["key"] for q in ls.load_default_questions()}
        self.assertEqual(set(body["results"][0]["answers"]), keys)
        self.assertIn("Handle: @glowco", self.backend.calls[-1][0][0])

    def test_bad_requests(self):
        self.assertEqual(self.req("/decide", {"items": []})[0], 400)
        self.assertEqual(self.req("/decide", {"items": [{"id": 1}]})[0], 400)
        self.assertEqual(self.req("/decide", {"items": [{"id": 1, "text": "x"}],
                                              "questions": [{"key": "a", "q": "?", "labels": ["one"]}]})[0], 400)
        self.assertEqual(self.req("/nope")[0], 404)

    def test_person_text(self):
        t = ls.person_text({"handle": "@a", "name": "A", "category": "Brand", "bio": "b",
                            "website": "a.com", "followers": 5})
        self.assertEqual(t, "Handle: @a\nName: A\nCategory: Brand\nBio: b\nWebsite: a.com\nFollowers: 5")

    def test_non_local_rejected(self):
        h = ls.make_handler(self.backend, [])
        obj = h.__new__(h)
        obj.client_address = ("10.0.0.5", 1)
        sent = []
        obj._send = lambda code, o: sent.append(code)
        self.assertFalse(obj._local())
        self.assertEqual(sent, [403])


class OfflineProtocolTest(unittest.TestCase):
    """Drive the handler directly so these checks do not bind a local port."""

    def test_short_model_batch_is_error(self):
        class ShortBackend(StubBackend):
            def predict_batch(self, states, questions):
                return super().predict_batch(states[:1], questions)

        h = ls.make_handler(ShortBackend(), [{'key': 'dtc', 'q': 'Founder?'}])
        obj = h.__new__(h)
        body = json.dumps({'items': [{'id': 1, 'text': 'founder'}, {'id': 2, 'text': 'brand'}]}).encode()
        obj.path = '/decide'
        obj.client_address = ('127.0.0.1', 1)
        obj.headers = {'Content-Length': str(len(body))}
        obj.rfile = io.BytesIO(body)
        sent = []
        obj._send = lambda code, payload: sent.append((code, payload))
        obj.do_POST()
        self.assertEqual(sent[0][0], 500)

    def test_health_reports_deployment_version_without_model(self):
        h = ls.make_handler(StubBackend(), [{'key': 'dtc', 'q': 'Founder?'}])
        obj = h.__new__(h)
        obj.path = '/health'
        obj.client_address = ('127.0.0.1', 1)
        sent = []
        obj._send = lambda code, payload: sent.append((code, payload))
        obj.do_GET()
        self.assertEqual(sent[0][1]['deployment_version'], ls.DEPLOYMENT_VERSION)


class StrictProtocolTest(HandlerHarness, unittest.TestCase):
    def test_invalid_request_contract(self):
        item = {"id": 1, "text": "founder"}
        for body in ([], {"items": [item, item]}, {"items": [item, dict(item, id="1")]},
                     {"items": [dict(item, id=True)]}, {"items": [dict(item, id={})]},
                     {"items": [item], "questions": []}, {"items": [item], "questions": None}):
            with self.subTest(body=body):
                self.assertEqual(self.req('/decide', body)[0], 400)

    def test_invalid_question_contract(self):
        for qs in ([{"key": " ", "q": "?"}], [{"key": "a", "q": " "}],
                   [{"key": "a", "q": "?", "labels": ["x", "x"]}],
                   [{"key": "a", "q": "?", "multi": "false"}],
                   [{"key": "a", "q": "?", "labels": ["x", "y"], "multi": True},
                    {"key": "a__0", "q": "?"}]):
            with self.subTest(qs=qs):
                self.assertEqual(self.req('/decide', {"items": [{"id": 1, "text": "x"}],
                                                      "questions": qs})[0], 400)

    def test_invalid_backend_result_never_returns_partial_answers(self):
        request = {"items": [{"id": 1, "text": "first"}, {"id": 2, "text": "second"}],
                   "questions": [{"key": "q", "q": "?"}]}
        valid = {"answers": {"q": {"noul": .5}}}
        cases = [[valid], [valid, valid, valid], [valid, {}],
                 [valid, {"answers": {"q": {"noul": float('nan')}}}],
                 [valid, {"answers": {"q": {"noul": float('inf')}}}],
                 [valid, {"answers": {"q": {"noul": True}}}],
                 [valid, {"answers": {"q": {"noul": 1.1}}}],
                 [valid, {"answers": {"q": {"noul": .5}, "extra": {"noul": .5}}}],
                 [dict(valid, state='second'), dict(valid, state='first')]]
        for raw in cases:
            with self.subTest(raw=raw), patch.object(self.backend, 'predict_batch', return_value=raw):
                code, payload = self.req('/decide', request)
                self.assertEqual(code, 500)
                self.assertEqual(payload, {'error': 'model unavailable'})

    def test_order_ids_and_multilingual_text_preserved(self):
        items = [{"id": 3, "text": "Nederlandse oprichter"},
                 {"id": "2", "text": "創業者"}]
        code, payload = self.req('/decide', {"items": items, "questions": [{"key": "q", "q": "?"}]})
        self.assertEqual(code, 200)
        self.assertEqual([r['id'] for r in payload['results']], [3, '2'])
        self.assertEqual(self.backend.calls[-1][0], [i['text'] for i in items])

    def test_choice_contract(self):
        questions = [{"key": "a", "q": "?", "labels": ["x", "y"]}]
        for probs in ({"x": .5}, {"x": .5, "y": .5, "z": 0}, {"x": .5, "y": -1}):
            with self.assertRaises(ValueError):
                ls.from_laya_answers(questions, {"a": {"choice": "x", "probabilities": probs}})

    def test_startup_uses_fixed_cache_and_offline_mode(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(ls, 'LayaBackend', side_effect=RuntimeError('missing cache')):
            with self.assertRaises(RuntimeError):
                ls.main([])
            self.assertEqual(os.environ['HF_HUB_OFFLINE'], '1')
            self.assertEqual(os.environ['HF_HOME'], os.path.join(ls.HERE, '.cache', 'huggingface'))
        with patch.dict(os.environ, {'HF_HOME': '/custom/cache'}, clear=True), patch.object(ls, 'LayaBackend', side_effect=RuntimeError('fake')):
            with self.assertRaises(RuntimeError):
                ls.main(['--allow-download'])
            self.assertNotIn('HF_HUB_OFFLINE', os.environ)
            self.assertEqual(os.environ['HF_HOME'], '/custom/cache')

    def test_backend_load_contract_without_loading_model(self):
        calls = []
        fake = SimpleNamespace(load=lambda *args, **kwargs: (calls.append((args, kwargs)) or SimpleNamespace(device='cpu')))
        with patch.object(ls, 'version', return_value=ls.LAYA_VERSION), patch.dict(sys.modules, {'laya': fake}):
            backend = ls.LayaBackend(device='cpu')
        self.assertEqual(backend.model, ls.DEFAULT_MODEL)
        self.assertEqual(calls, [((ls.DEFAULT_MODEL,), {'device': 'cpu', 'subfolder': None})])
        with patch.object(ls, 'version', return_value='0.0.0'):
            with self.assertRaises(RuntimeError):
                ls.LayaBackend(device='cpu')


if __name__ == "__main__":
    unittest.main()
