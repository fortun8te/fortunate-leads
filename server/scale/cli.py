#!/usr/bin/env python3
"""Scale pipeline CLI.

  python3 server/scale/cli.py model [--accounts 3]
  python3 server/scale/cli.py stats [--store data/scale.sqlite]
  python3 server/scale/cli.py queue --handles handles.txt [--store ...]
  python3 server/scale/cli.py import-leads --db data/leads.sqlite            # known -> done, unread -> queue
  python3 server/scale/cli.py enrich --tor 4x40 --minutes 60 [--out out.jsonl | --db data/leads.sqlite]
  python3 server/scale/cli.py check-egress --config data/scale.json         # exit IP of every egress vs home

`enrich` needs curl_cffi (browser TLS) in the worker's own venv: python3 -m venv .venv-scale &&
.venv-scale/bin/pip install curl_cffi. Tor circuits come from our own tor processes (never the home IP
as exit). Nothing here logs into Instagram.
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, os.path.dirname(HERE))   # server/ for db/rules/control when --db is used

from scale import model  # noqa: E402
from scale.dedupe import SeenStore  # noqa: E402
from scale.egress import Egress, HomeGuard, Registry  # noqa: E402
from scale.metrics import Metrics  # noqa: E402

DEFAULT_STORE = os.path.join(HERE, '..', '..', 'data', 'scale.sqlite')

def _store(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return SeenStore(path)

def cmd_enrich(a):
    from scale.enrich import CurlTransport, EnrichmentPool, FortunateDBSink, JsonlSink
    from scale.tor_pool import TorPool
    st = _store(a.store)
    if a.handles:
        with open(a.handles) as f:
            st.add_discovered([{'handle': h.strip().lstrip('@')} for h in f if h.strip() and not h.startswith('#')],
                              source='cli')
    procs, cpp = (int(x) for x in a.tor.lower().split('x'))
    pool = TorPool(processes=procs, circuits_per_process=cpp, base_port=a.base_port,
                   root=a.tor_root).start()
    reg = Registry(HomeGuard())
    for e in pool.egresses():
        reg.add(e)
    sink = FortunateDBSink(a.db) if a.db else JsonlSink(a.out)
    m = Metrics()
    ep = EnrichmentPool(st, reg.pool(), CurlTransport(), sink, metrics=m, registry=reg).start()
    t0 = time.time()
    try:
        while time.time() - t0 < a.minutes * 60:
            time.sleep(min(30, a.minutes * 60))
            snap = m.snapshot()
            done = snap.get('pipeline', {}).get('enrich', {}).get('profiles', {})
            print(json.dumps({'t_min': round((time.time() - t0) / 60, 1), 'profiles_total': done.get('total', 0),
                              'profiles_last_min': done.get('last_min', 0), 'store': st.stats()}), flush=True)
            stats = st.stats()
            if not stats.get('new') and not stats.get('leased'):
                break   # queue drained
    finally:
        ep.stop()
        pool.stop()
    if a.metrics_out:
        with open(a.metrics_out, 'w') as f:
            json.dump(m.snapshot(), f, indent=1)

def cmd_check_egress(a):
    from scale.transport import Transport
    cfg = json.load(open(a.config))
    guard = HomeGuard(cfg.get('home_addresses'))
    t = Transport()
    ok = True
    for d in cfg.get('egresses', []):
        e = Egress.from_dict(d)
        try:
            print(e.id, 'exit', guard.verify(e, t), 'OK')
        except Exception as x:
            ok = False
            print(e.id, 'REFUSED:', x)
    return 0 if ok else 1

def main(argv=None):
    ap = argparse.ArgumentParser(description='Fortunate Leads scale pipeline')
    sub = ap.add_subparsers(dest='cmd', required=True)
    m = sub.add_parser('model')
    m.add_argument('--accounts', type=int, default=3)
    s = sub.add_parser('stats')
    s.add_argument('--store', default=DEFAULT_STORE)
    q = sub.add_parser('queue')
    q.add_argument('--handles', required=True)
    q.add_argument('--store', default=DEFAULT_STORE)
    il = sub.add_parser('import-leads')
    il.add_argument('--db', required=True)
    il.add_argument('--store', default=DEFAULT_STORE)
    e = sub.add_parser('enrich')
    e.add_argument('--tor', default='2x16', help='PROCESSESxCIRCUITS, e.g. 4x40')
    e.add_argument('--base-port', type=int, default=9260)
    e.add_argument('--tor-root', default=None)
    e.add_argument('--minutes', type=float, default=10)
    e.add_argument('--handles')
    e.add_argument('--out', default='enriched.jsonl')
    e.add_argument('--db')
    e.add_argument('--store', default=DEFAULT_STORE)
    e.add_argument('--metrics-out')
    c = sub.add_parser('check-egress')
    c.add_argument('--config', required=True)
    a = ap.parse_args(argv)
    if a.cmd == 'model':
        model.main(['--accounts', str(a.accounts)])
    elif a.cmd == 'stats':
        print(json.dumps(_store(a.store).stats()))
    elif a.cmd == 'queue':
        with open(a.handles) as f:
            new = _store(a.store).add_discovered([{'handle': h.strip().lstrip('@')} for h in f if h.strip()],
                                                 source='cli')
        print(json.dumps({'queued_new': len(new)}))
    elif a.cmd == 'import-leads':
        import db
        from scale import bridge
        conn = db.connect(a.db)
        st = _store(a.store)
        print(json.dumps({'known': bridge.import_known_from_leads(conn, st),
                          'queued_new': bridge.queue_unread_from_leads(conn, st)}))
    elif a.cmd == 'enrich':
        cmd_enrich(a)
    elif a.cmd == 'check-egress':
        return cmd_check_egress(a)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
