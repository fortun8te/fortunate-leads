#!/usr/bin/env python3
"""Explicit offline synthetic benchmark. Never touches a live database or collectors.

Example heavy opt-in: python3 tests/bench_map_universe.py --nodes 5000000 --allow-large
The 5M measurement has deliberately NOT been run as part of implementation.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--nodes',type=int,default=5000)
    p.add_argument('--allow-large',action='store_true')
    p.add_argument('--max-rss-mb',type=int,default=2048)
    p.add_argument('--max-seconds',type=int,default=1800)
    p.add_argument('--reserve-free-gb',type=float,default=3)
    p.add_argument('--directory',help='Dedicated empty benchmark output directory')
    args=p.parse_args()
    if not 2 <= args.nodes <= 5_000_000:
        p.error('nodes must be 2..5000000')
    if args.nodes>100_000 and not args.allow_large:
        p.error('large benchmark requires explicit --allow-large; run only when the computer is available')
    if args.max_rss_mb<128 or args.max_seconds<1 or args.reserve_free_gb<1:
        p.error('invalid resource limits')
    root=Path(args.directory).resolve() if args.directory else Path(tempfile.mkdtemp(prefix='universe-bench-'))
    if args.directory:
        if root.exists() and any(root.iterdir()):
            p.error('benchmark directory must be empty')
        root.mkdir(parents=True,exist_ok=True)
    estimated=args.nodes*1200
    required=estimated+int(args.reserve_free_gb*1024**3)
    if shutil.disk_usage(root).free<required:
        p.error('insufficient free disk for estimated fixture/index/tiles plus reserve')
    database=root/'synthetic.sqlite'
    fixture_start=time.monotonic()
    c=sqlite3.connect(database)
    c.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; CREATE TABLE people(id INTEGER PRIMARY KEY,handle TEXT,name TEXT,followers INTEGER); CREATE TABLE universe_edges(source_id INTEGER,target_id INTEGER,observed_at TEXT,provenance TEXT);')
    for start in range(1,args.nodes+1,4096):
        end=min(args.nodes+1,start+4096)
        c.executemany('INSERT INTO people VALUES(?,?,?,?)',((i,'synthetic_'+str(i),'Synthetic benchmark',i%1000000) for i in range(start,end)))
        c.executemany('INSERT INTO universe_edges VALUES(?,?,?,?)',((max(1,i//20),i,'synthetic','synthetic_benchmark') for i in range(max(2,start),end)))
        if time.monotonic()-fixture_start>args.max_seconds or shutil.disk_usage(root).free<args.reserve_free_gb*1024**3:
            c.close()
            raise SystemExit('fixture build stopped by time/free-space guard; partial fixture retained')
    c.commit();c.close()
    command=[sys.executable,str(Path(__file__).resolve().parents[1]/'server/map_universe.py'),'--db',str(database),'--anchor','1']
    start=time.monotonic();peak=0
    with open(root/'build.json','w') as out,open(root/'build.stderr','w') as err:
        process=subprocess.Popen(command,stdout=out,stderr=err,start_new_session=True)
        while process.poll() is None:
            time.sleep(.5)
            rss=subprocess.run(['ps','-o','rss=','-p',str(process.pid)],capture_output=True,text=True).stdout.strip()
            if rss:
                peak=max(peak,int(rss)*1024)
            reason=None
            if peak>args.max_rss_mb*1024**2: reason='memory cap'
            if time.monotonic()-start>args.max_seconds: reason='time cap'
            if shutil.disk_usage(root).free<args.reserve_free_gb*1024**3: reason='free-space reserve'
            if reason:
                os.killpg(process.pid,signal.SIGTERM)
                process.wait(timeout=10)
                raise SystemExit('benchmark stopped by '+reason+'; partial files retained at '+str(root))
        if process.returncode:
            raise SystemExit('build failed; see '+str(root/'build.stderr'))
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
    import map_universe as U
    manifest=U.manifest(database)
    t=time.monotonic();page=U.view(database,{'budget':'8192'});latency=(time.monotonic()-t)*1000
    result={'synthetic':True,'nodes':args.nodes,'fixture_seconds':round(start-fixture_start,3),
            'build_seconds':round(time.monotonic()-start,3),'observed_peak_rss_bytes':peak or None,
            'disk_bytes':sum(f.stat().st_size for f in root.rglob('*') if f.is_file()),
            'initial_records':page['record_count'],'initial_binary_bytes':sum(16+t['record_count']*32 for t in page['tiles']),
            'initial_manifest_bytes':len(json.dumps(manifest).encode()),'view_ms':round(latency,3),'directory':str(root)}
    (root/'benchmark.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))


if __name__=='__main__': main()
