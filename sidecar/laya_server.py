#!/usr/bin/env python3
"""Local Laya decision sidecar for Fortunate Leads.

Loads a Laya checkpoint (https://huggingface.co/convaiinnovations/laya) once and serves
yes/no and choice questions over profile text on 127.0.0.1:18742.

  GET  /health  -> {"ok": true, "model": "...", "device": "mps|cuda|cpu"}
  POST /decide  {"items":[{"id","text"} | {"id","person":{...}}],
                 "questions":[{"key","q","labels"?,"multi"?}]}   (questions optional -> questions.json)
             -> {"results":[{"id","answers":{key:{"p":float,"label"?,...}}}], "model","device","ms"}

Question mapping onto Laya's typed questions:
  no labels            -> Laya "noul":   p = P(yes)
  labels (list)        -> Laya "choice": label = argmax, p = its probability, probs = all labels
  labels + multi:true  -> one Laya "noul" per label (Laya has no multi-label type):
                          probs = {label: P(yes)}, label = best, p = its probability
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

HOST = "127.0.0.1"
DEFAULT_PORT = 18742
DEFAULT_MODEL = os.environ.get("LAYA_MODEL", "convaiinnovations/laya")
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_ITEMS = 500
MAX_BODY = 5 * 1024 * 1024
BATCH_SIZE = int(os.environ.get("LAYA_BATCH_SIZE", "16"))

log = logging.getLogger("laya_sidecar")


# ---------------------------------------------------------------- text format

def person_text(p: Dict[str, Any]) -> str:
    """Build the state text for one person/profile. Fields are optional."""
    lines = []
    for label, keys in (("Handle", ("handle", "username")), ("Name", ("name", "full_name")),
                        ("Category", ("category",)), ("Bio", ("bio", "biography")),
                        ("Website", ("website", "external_url", "url")),
                        ("Followers", ("followers", "follower_count"))):
        v = next((p.get(k) for k in keys if p.get(k) not in (None, "")), None)
        if v is None:
            continue
        if label == "Handle":
            v = "@" + str(v).lstrip("@")
        lines.append("%s: %s" % (label, str(v).strip()))
    return "\n".join(lines)


def load_default_questions(path: Optional[str] = None) -> List[Dict[str, Any]]:
    with open(path or os.path.join(HERE, "questions.json"), encoding="utf-8") as f:
        return json.load(f)["questions"]


# ---------------------------------------------------------------- question mapping

def validate_questions(qs: Any) -> List[Dict[str, Any]]:
    if not isinstance(qs, list) or not qs:
        raise ValueError("questions must be a non-empty list")
    seen = set()
    for q in qs:
        if not isinstance(q, dict) or not isinstance(q.get("key"), str) or not isinstance(q.get("q"), str):
            raise ValueError("each question needs string 'key' and 'q'")
        if q["key"] in seen:
            raise ValueError("duplicate question key %r" % q["key"])
        seen.add(q["key"])
        labels = q.get("labels")
        if labels is not None and (not isinstance(labels, list) or len(labels) < 2
                                   or not all(isinstance(l, str) and l for l in labels)):
            raise ValueError("question %r: labels must be a list of >= 2 strings" % q["key"])
        if q.get("multi") and not labels:
            raise ValueError("question %r: multi requires labels" % q["key"])
    return qs


def to_laya_questions(qs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for q in qs:
        labels = q.get("labels")
        if not labels:
            out[q["key"]] = {"type": "noul", "instructions": q["q"]}
        elif q.get("multi"):
            for i, lab in enumerate(labels):
                out["%s__%d" % (q["key"], i)] = {
                    "type": "noul",
                    "instructions": "%s Answer true if it includes: %s" % (q["q"], lab)}
        else:
            out[q["key"]] = {"type": "choice", "instructions": q["q"], "criteria": list(labels)}
    return out


def from_laya_answers(qs: List[Dict[str, Any]], answers: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    res: Dict[str, Any] = {}
    for q in qs:
        k, labels = q["key"], q.get("labels")
        if not labels:
            a = answers[k]
            res[k] = {"p": float(a["noul"]), "confidence": a.get("answer_confidence", a.get("confidence"))}
        elif q.get("multi"):
            probs = {lab: float(answers["%s__%d" % (k, i)]["noul"]) for i, lab in enumerate(labels)}
            best = max(probs, key=probs.get)
            res[k] = {"p": probs[best], "label": best, "probs": probs}
        else:
            a = answers[k]
            probs = {lab: float(v) for lab, v in a["probabilities"].items()}
            res[k] = {"p": probs[a["choice"]], "label": a["choice"], "probs": probs,
                      "confidence": a.get("answer_confidence", a.get("confidence"))}
    return res


# ---------------------------------------------------------------- model backend

def pick_device() -> str:
    forced = os.environ.get("LAYA_DEVICE")
    if forced:
        return forced
    import torch
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class LayaBackend:
    """Thin wrapper over laya.Agent; anything with .model, .device and .predict_batch works."""

    def __init__(self, model_id: str = DEFAULT_MODEL, device: Optional[str] = None):
        import laya  # heavy import deferred so tests never need torch
        sub = None
        if model_id.endswith(":multilingual"):
            model_id, sub = model_id.split(":")[0], "multilingual"
        self.model = model_id + (":" + sub if sub else "")
        self.agent = laya.load(model_id, device=device or pick_device(), subfolder=sub)
        self.device = str(self.agent.device)

    def predict_batch(self, states: List[str], questions: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        return self.agent.predict_batch(states, questions, batch_size=BATCH_SIZE)


# ---------------------------------------------------------------- HTTP layer

def make_handler(backend, default_questions: List[Dict[str, Any]]):
    lock = threading.Lock()  # one forward pass at a time; the model is not re-entrant on MPS

    class Handler(BaseHTTPRequestHandler):
        server_version = "LayaSidecar/1"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.client_address[0], *args)

        def _send(self, code: int, obj: Any):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _local(self) -> bool:
            if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                self._send(403, {"error": "forbidden"})
                return False
            return True

        def do_GET(self):
            if not self._local():
                return
            if self.path.split("?")[0] == "/health":
                self._send(200, {"ok": True, "model": backend.model, "device": backend.device})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._local():
                return
            if self.path.split("?")[0] != "/decide":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > MAX_BODY:
                    raise ValueError("bad Content-Length")
                req = json.loads(self.rfile.read(n))
                items = req.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError("items must be a non-empty list")
                if len(items) > MAX_ITEMS:
                    raise ValueError("at most %d items per request" % MAX_ITEMS)
                texts = []
                for it in items:
                    if not isinstance(it, dict) or "id" not in it:
                        raise ValueError("each item needs an 'id'")
                    t = it.get("text")
                    if not t and isinstance(it.get("person"), dict):
                        t = person_text(it["person"])
                    if not isinstance(t, str) or not t.strip():
                        raise ValueError("item %r needs 'text' or 'person'" % (it["id"],))
                    texts.append(t)
                qs = validate_questions(req.get("questions") or default_questions)
                lq = to_laya_questions(qs)
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            try:
                t0 = time.perf_counter()
                with lock:
                    raw = backend.predict_batch(texts, lq)
                ms = round((time.perf_counter() - t0) * 1000, 1)
                results = [{"id": it["id"], "answers": from_laya_answers(qs, r["answers"])}
                           for it, r in zip(items, raw)]
            except Exception as e:  # model failure -> 500, caller should skip
                log.exception("predict failed")
                return self._send(500, {"error": "model error: %s" % e})
            self._send(200, {"results": results, "model": backend.model, "device": backend.device, "ms": ms})

    return Handler


def make_server(backend, port: int = DEFAULT_PORT, questions=None) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((HOST, port), make_handler(backend, questions or load_default_questions()))
    srv.daemon_threads = True
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("LAYA_PORT", DEFAULT_PORT)))
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="HF repo id; append ':multilingual' for the 322M multilingual checkpoint")
    ap.add_argument("--device", default=None, help="force mps|cuda|cpu (default: auto)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t0 = time.time()
    backend = LayaBackend(a.model, a.device)
    log.info("loaded %s on %s in %.1fs", backend.model, backend.device, time.time() - t0)
    srv = make_server(backend, a.port)
    log.info("listening on http://%s:%d", HOST, a.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
