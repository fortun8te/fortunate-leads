"""`igscrape ui`: a tiny local web page (localhost only). Paste usernames, click Run, watch progress,
download JSON / CSV. Standard library HTTP server + one background asyncio thread running igscraper."""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from igscraper.output import render

from . import mock
from .runner import RESULTS_DIR, Job, build_config, parse_usernames, write_outputs

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Instagram Profile Scraper</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:760px;margin:32px auto;padding:0 16px;color:#1d1d1f}
 h1{font-size:22px;margin:0 0 4px} .sub{color:#666;margin:0 0 18px}
 textarea{width:100%;height:220px;font:14px ui-monospace,Menlo,monospace;padding:10px;border:1px solid #ccc;border-radius:8px;box-sizing:border-box}
 button,a.btn{font:inherit;padding:9px 18px;border-radius:8px;border:0;background:#0071e3;color:#fff;cursor:pointer;text-decoration:none;display:inline-block;margin:10px 8px 0 0}
 button.sec{background:#e8e8ed;color:#1d1d1f} button:disabled{opacity:.45;cursor:default} a.btn.off{pointer-events:none;opacity:.45}
 .bar{height:12px;background:#e8e8ed;border-radius:6px;overflow:hidden;margin:16px 0 6px} .bar>div{height:100%;width:0;background:#34c759;transition:width .3s}
 #stats{font:13px ui-monospace,Menlo,monospace;white-space:pre-wrap;color:#333} .err{color:#c00} label{color:#444}
 details{margin-top:14px;color:#555;font-size:13px}
</style></head><body>
<h1>Instagram Profile Scraper</h1>
<p class="sub">Paste usernames, @handles or profile links (one per line, or separated by commas). __MOCK__</p>
<textarea id="names" placeholder="nasa&#10;natgeo&#10;https://www.instagram.com/instagram/"></textarea>
<div><label><input type="checkbox" id="fresh"> ignore cache (always fetch fresh)</label></div>
<button id="run">Run</button><button id="stop" class="sec" disabled>Stop</button>
<a id="dj" class="btn off" href="#">Download JSON</a><a id="dc" class="btn off" href="#">Download CSV</a>
<div class="bar"><div id="fill"></div></div>
<div id="stats">Ready.</div>
<details><summary>Connections (egresses)</summary><div id="eg" style="font:12px ui-monospace,Menlo,monospace;white-space:pre"></div></details>
<script>
const T="__TOKEN__", $=id=>document.getElementById(id);
function fmt(s){if(s==null)return"--";s=Math.round(s);if(s<60)return s+"s";if(s<3600)return Math.floor(s/60)+"m"+String(s%60).padStart(2,"0")+"s";return Math.floor(s/3600)+"h"+String(Math.floor(s%3600/60)).padStart(2,"0")+"m"}
async function api(p,body){const r=await fetch(p,{method:body?"POST":"GET",headers:{"X-Token":T,"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined});return r.json()}
function paint(s){
  const pct=s.total?100*s.done/s.total:0; $("fill").style.width=pct+"%";
  let t=s.state==="idle"?"Ready.":`${s.done} / ${s.total} done  (${s.ok} ok, ${s.errors} failed${s.cached?", "+s.cached+" from cache":""})
${s.perMinute} profiles/min   healthy egresses ${s.healthyEgresses}/${s.egresses}   ETA ${s.state==="running"?fmt(s.etaS):"--"}   elapsed ${fmt(s.elapsedS)}
status: ${s.state}${s.message?" - "+s.message:""}${s.saved?"\nsaved to: "+s.saved.join("\n          "):""}`;
  $("stats").textContent=t; $("stats").className=s.state==="failed"?"err":"";
  const running=s.state==="running"; $("run").disabled=running; $("stop").disabled=!running;
  const has=s.hasResults; for(const [id,f] of [["dj","json"],["dc","csv"]]){$(id).className="btn"+(has?"":" off");$(id).href="/download?format="+f+"&token="+T}
  $("eg").textContent=(s.egressDetail||[]).map(e=>`${e.state.padEnd(8)} ${e.egress}  ok=${e.ok} blocks=${e.blocks}${e.parkedForS?" parked "+e.parkedForS+"s":""}`).join("\n")||s.egressSource||"";
}
let timer=null;
async function poll(){const s=await api("/status");paint(s);if(s.state!=="running"){clearInterval(timer);timer=null}}
$("run").onclick=async()=>{const r=await api("/run",{usernames:$("names").value,fresh:$("fresh").checked});
  if(r.error){$("stats").textContent=r.error;$("stats").className="err";return} if(!timer)timer=setInterval(poll,500);poll()};
$("stop").onclick=()=>api("/stop",{});
poll(); timer=setInterval(poll,500);
</script></body></html>"""


class App:
    def __init__(self, egress_file: str | None):
        self.egress_file = egress_file
        self.token = secrets.token_urlsafe(16)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.job: Job | None = None
        self.task = None
        self.saved: list[str] = []
        self.source = ""
        self.lock = threading.Lock()

    def start(self, text: str, fresh: bool) -> dict:
        with self.lock:
            if self.job and self.job.progress.state == "running":
                return {"error": "a run is already in progress"}
            names = parse_usernames(text or "")
            if not names:
                return {"error": "paste at least one username"}
            try:
                cfg, self.source = build_config(egress_file=self.egress_file, cache=not fresh)
            except (OSError, ValueError) as e:
                return {"error": f"egress config problem: {e}"}
            self.job, self.saved = Job(names, cfg), []
            self.task = asyncio.run_coroutine_threadsafe(self._run(self.job), self.loop)
            return {"ok": True, "total": self.job.progress.total}

    async def _run(self, job: Job) -> None:
        try:
            await job.run()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        items = job.partial_items
        if items:
            try:
                RESULTS_DIR.mkdir(parents=True, exist_ok=True)
                stem = RESULTS_DIR / f"ig-results-{time.strftime('%Y%m%d-%H%M%S')}"
                self.saved.extend(write_outputs(items, str(stem.with_suffix(".json")),
                                                str(stem.with_suffix(".csv"))))
            except OSError as e:
                job.progress.message = f"could not save results: {e}"

    def stop(self) -> dict:
        if self.task and not self.task.done():
            self.task.cancel()
        return {"ok": True}

    def status(self) -> dict:
        if not self.job:
            return {"state": "idle", "total": 0, "done": 0, "hasResults": False, "egressSource": self._src_hint()}
        d = self.job.progress.as_dict()
        d.update(hasResults=bool(self.job.partial_items) and d["state"] != "running", saved=self.saved,
                 egressSource=self.source)
        return d

    def _src_hint(self) -> str:
        try:
            return "egresses from: " + build_config(egress_file=self.egress_file)[1]
        except Exception as e:
            return f"egress config problem: {e}"

    def download(self, fmt: str) -> tuple[bytes, str, str]:
        items = self.job.partial_items if self.job else []
        body = render(items, fmt).encode("utf-8")
        ctype = "text/csv; charset=utf-8" if fmt == "csv" else "application/json; charset=utf-8"
        return body, ctype, f"ig-results.{fmt}"


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "igscrape-ui"

        def log_message(self, *a):          # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _authed(self, qs=None) -> bool:
            tok = self.headers.get("X-Token") or (qs or {}).get("token", [""])[0]
            return secrets.compare_digest(tok or "", app.token)

        def _host_ok(self) -> bool:      # blocks DNS-rebinding: only answer to localhost names
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in ("127.0.0.1", "localhost", "::1")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/":
                note = "<b>MOCK MODE: offline fake data.</b>" if mock.enabled() else ""
                page = PAGE.replace("__TOKEN__", app.token).replace("__MOCK__", note)
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if not self._authed(qs):
                return self._json({"error": "unauthorized"}, 401)
            if u.path == "/status":
                return self._json(app.status())
            if u.path == "/download":
                fmt = qs.get("format", ["json"])[0]
                if fmt not in ("json", "csv", "jsonl"):
                    return self._json({"error": "format must be json or csv"}, 400)
                body, ctype, name = app.download(fmt)
                return self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._host_ok() or not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            n = int(self.headers.get("Content-Length") or 0)
            if n > 5_000_000:
                return self._json({"error": "too much input"}, 413)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"error": "bad JSON"}, 400)
            path = urlparse(self.path).path
            if path == "/run":
                names = body.get("usernames", "")
                if isinstance(names, list):
                    names = "\n".join(map(str, names))
                return self._json(app.start(str(names), bool(body.get("fresh"))))
            if path == "/stop":
                return self._json(app.stop())
            self._json({"error": "not found"}, 404)

    return H


def _free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)) + [0]:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise SystemExit("no free port")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="igscrape ui", description="Open the local web page.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("-e", "--egresses", metavar="FILE", help="egress config (default ~/ig-scraper/egresses.yaml)")
    a = ap.parse_args(argv)
    app = App(a.egresses)
    port = _free_port(a.port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"igscrape UI running at {url}" + ("  [MOCK MODE]" if mock.enabled() else ""), file=sys.stderr)
    print("keep this window open while you use the page; press Ctrl-C here to quit.", file=sys.stderr)
    print(f"results are also saved to {RESULTS_DIR}", file=sys.stderr)
    sys.stderr.flush()
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)
    finally:
        srv.server_close()
    return 0
