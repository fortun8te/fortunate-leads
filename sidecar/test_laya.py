"""HTTP-layer tests with a stubbed model: no torch, no download.  Run: python -m unittest sidecar/test_laya.py"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

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


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = StubBackend()
        cls.srv = ls.make_server(cls.backend, port=0)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health(self):
        self.assertEqual(self.req("/health"), (200, {"ok": True, "model": "stub", "device": "cpu"}))

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


if __name__ == "__main__":
    unittest.main()
