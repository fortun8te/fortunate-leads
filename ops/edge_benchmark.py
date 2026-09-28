#!/usr/bin/env python3
"""Create/report isolated plans or explicitly arm their server dispatch. Never sends requests."""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'server'))
import db
import edge_benchmark as ledger

KEY = 'raw_edge_benchmark'


def _connection(path):
    conn=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw',uri=True,timeout=15)
    conn.row_factory=sqlite3.Row
    return conn


def _drained(conn,cfg=None):
    gate=db.get_setting(conn,'instagram_request_gate') or {}
    if gate.get('active') or gate.get('queue') or (cfg or {}).get('inflight'):
        raise ValueError('Request permits must drain before changing benchmark activation')


def activate(live_db,bench_db,lane_id):
    if Path(live_db).resolve()==Path(bench_db).resolve():
        raise ValueError('Benchmark database must be isolated')
    with _connection(live_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        current=db.get_setting(conn,KEY) or {}
        if current.get('enabled'):
            raise ValueError('A benchmark is already armed')
        _drained(conn,current)
        meta=ledger.state(bench_db)
        if meta['state']!='ready' or meta.get('phase')!='warmup' or meta['inflight']:
            raise ValueError('Activation requires a fresh warmup plan without outstanding requests')
        row=conn.execute('SELECT * FROM accounts WHERE lane_id=?',(lane_id,)).fetchone()
        if not row or row['is_main'] or row['ig_id']!=meta['viewer_id']:
            raise ValueError('Lane must be the plan viewer and an alternate account')
        if db.get_setting(conn,'instagram_request_attention') or row['hold']:
            raise ValueError('Existing attention or account hold requires review')
        cfg=dict(enabled=True,path=str(Path(bench_db).resolve()),viewer_id=meta['viewer_id'],lane_id=lane_id,
                 phase='warmup',activated_at=time.time(),inflight=None,attempts=0)
        if current.get('viewer_id') == meta['viewer_id']:
            # A new experiment does not reset this viewer's common pacing.
            cfg['attempts'] = current.get('attempts', 0)
            cfg['next_at'] = current.get('next_at', 0)
        db.set_setting(conn,KEY,cfg)
        conn.commit()
        return cfg


def advance(live_db):
    with _connection(live_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        cfg=db.get_setting(conn,KEY) or {}
        if not cfg.get('enabled') or cfg.get('phase')!='warmup':
            raise ValueError('An armed warmup benchmark is required')
        _drained(conn,cfg)
        if db.get_setting(conn,'instagram_request_attention'):
            raise ValueError('Attention hold requires review')
        row=conn.execute('SELECT * FROM accounts WHERE lane_id=?',(cfg['lane_id'],)).fetchone()
        if not row or row['hold'] or row['is_main'] or row['ig_id']!=cfg['viewer_id']:
            raise ValueError('Pinned alternate is unavailable or held')
        with ledger._connect(cfg['path']) as bench:
            tasks=list(bench.execute("SELECT * FROM tasks WHERE warmup=1 AND state!='deferred'"))
            protocol=ledger._meta(bench).get('preset')=='followers-protocol'
            if not tasks or any(t['state'] not in (('done','stopped') if protocol else ('done',)) for t in tasks):
                raise ValueError('Every warmup must finish successfully')
            requests=list(bench.execute('SELECT r.* FROM requests r JOIN tasks t USING(task_id) WHERE t.warmup=1'))
            if len(requests)!=len(tasks):
                raise ValueError('Warmup request coverage mismatch')
            for req in requests:
                result=json.loads(req['result']) if req['result'] else {}
                if (req['finished_at'] is None or (result.get('status')!='ok' or result.get('terminal_warning')) and not (protocol and result.get('stop_scope')=='target')
                        or result.get('actual_http_requests')!=1 or result.get('transport_completed') is not True):
                    raise ValueError('Every warmup must have one confirmed successful transport')
        ledger.advance_phase(cfg['path'])
        cfg['phase']='measured'
        cfg['measured_activated_at']=time.time()
        db.set_setting(conn,KEY,cfg)
        conn.commit()
        return cfg


def deactivate(live_db):
    with _connection(live_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        cfg=db.get_setting(conn,KEY) or {}
        if not cfg.get('enabled'):
            return cfg
        _drained(conn,cfg)
        meta=ledger.state(cfg['path'])
        if meta['inflight']:
            raise ValueError('Uncertain benchmark transport requires resolution before disarming')
        ledger.stop(cfg['path'],'explicitly_disarmed')
        cfg.update(enabled=False,disarmed_at=time.time())
        db.set_setting(conn,KEY,cfg)
        # Deliberately preserve paused settings, cooldowns and attention/account holds.
        conn.commit()
        return cfg


def enable_search_fallback(live_db,reason,query):
    with _connection(live_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        cfg=db.get_setting(conn,KEY) or {}
        if not cfg.get('enabled'):
            raise ValueError('An armed follower protocol is required')
        _drained(conn,cfg)
        if db.get_setting(conn,'instagram_request_attention'):
            raise ValueError('Attention hold requires review')
        result=ledger.enable_fallback(cfg['path'],reason,query=query)
        conn.commit()
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    create=sub.add_parser('create')
    create.add_argument('--live-db',required=True)
    create.add_argument('--bench-db',required=True)
    create.add_argument('--viewer-id',required=True)
    create.add_argument('--seed',type=int,default=20260928)
    create.add_argument('--max-requests',type=int,help='Defaults to the exact declared request bound')
    create.add_argument('--window-seconds',type=float,help='Default 900 for feasibility presets, 3600 for standard')
    create.add_argument('--preset',choices=sorted(ledger.PRESETS),default='standard')
    create.add_argument('--direction',choices=('following','followers','both'),help='Defaults to the preset direction')
    create.add_argument('--arms',help='Comma-separated arm names, e.g. web_rest200,web_rest300,web_rest500,web_rest1500')
    create.add_argument('--target-ids',help='One to three comma-separated public numeric IDs already present in the live snapshot')
    create.add_argument('--target-count',type=int,help='1-3 feasibility targets; followers-protocol requires 3-6, defaults three')
    create.add_argument('--arm-request-budgets',type=json.loads,help='JSON object of per-arm attempt caps, including warmups')
    create.add_argument('--cohort-index',type=int,choices=(1,2,3),default=1)
    create.add_argument('--pages-per-run',type=int,default=2)
    show=sub.add_parser('report')
    show.add_argument('--bench-db',required=True)
    arm=sub.add_parser('activate')
    arm.add_argument('--live-db',required=True)
    arm.add_argument('--bench-db',required=True)
    arm.add_argument('--lane-id',required=True)
    for command in ('advance','deactivate'):
        sub.add_parser(command).add_argument('--live-db',required=True)
    fallback=sub.add_parser('enable-fallback')
    fallback.add_argument('--live-db',required=True)
    fallback.add_argument('--reason',required=True)
    fallback.add_argument('--query',required=True)
    a=p.parse_args()
    try:
        if a.command=='create':
            result=ledger.create_plan(a.live_db,a.bench_db,a.viewer_id,seed=a.seed,max_requests=a.max_requests,window_seconds=a.window_seconds,
                preset=a.preset,directions=('following','followers') if a.direction=='both' else (a.direction,) if a.direction else None,
                arms=[part.strip() for part in a.arms.split(',')] if a.arms is not None else None,
                target_ids=[part.strip() for part in a.target_ids.split(',')] if a.target_ids is not None else None,
                target_count=a.target_count,pages_per_run=a.pages_per_run,arm_request_budgets=a.arm_request_budgets,cohort_index=a.cohort_index)
        elif a.command=='report':
            result=ledger.report(a.bench_db)
        elif a.command=='activate':
            result=activate(a.live_db,a.bench_db,a.lane_id)
        elif a.command=='enable-fallback':
            result=enable_search_fallback(a.live_db,a.reason,a.query)
        elif a.command=='advance':
            result=advance(a.live_db)
        else:
            result=deactivate(a.live_db)
    except (ValueError,sqlite3.Error) as exc:
        p.error(str(exc))
    print(json.dumps(result,indent=2))

if __name__=='__main__':
    main()
