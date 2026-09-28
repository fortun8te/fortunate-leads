"""Isolated, bounded raw-edge benchmark. No network and no live database writes."""
import hashlib
import json
import random
import sqlite3
import time
from pathlib import Path

ARMS = [(f'{route}{size}', route, size) for route in ('web_rest', 'mobile_rest') for size in (50, 100, 200)] + [('mobile_graphql', 'mobile_graphql', None)]
CUSTOM_ARMS = ARMS + [(f'web_rest{size}', 'web_rest', size) for size in (300, 500, 1500)]
PRESETS = {
    'standard': {'arms': [a[0] for a in ARMS], 'direction': 'following'},
    'chrome-large-following': {'arms': ['web_rest200','web_rest300','web_rest500','web_rest1500'], 'direction': 'following'},
    'followers-feasibility': {'arms': [a[0] for a in ARMS], 'direction': 'followers'},
}
EXCLUSIONS = ['private or unknown privacy', 'missing numeric Instagram ID', 'missing following or verification', 'viewer itself', 'duplicate numeric ID']


def _id(value):
    value = str(value or '')
    return str(int(value)) if value.isascii() and value.isdigit() and int(value) > 0 else None


def _connect(path):
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _meta(db):
    return json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])


def _save(db, meta):
    db.execute('UPDATE metadata SET value=?', (json.dumps(meta, sort_keys=True),))


def _stop(db, meta, reason, now):
    meta.update(state='stopped', stop_reason=reason, ended_at=now)
    _save(db, meta)


def _stratum(row):
    n = int(row['following'])
    return ('small' if n < 500 else 'medium' if n < 2000 else 'large') + ('_verified' if row['is_verified'] else '_unverified')


def create_plan(live_db_path, bench_path, viewer_id, seed=20260928, *, corpus=None,
                max_requests=None, window_seconds=None, pages_per_run=2, fingerprints=None, directions=None,
                preset='standard', arms=None, target_ids=None, target_count=None):
    viewer_id = _id(viewer_id)
    if not viewer_id:
        raise ValueError('numeric viewer_id required')
    if Path(bench_path).resolve() == Path(live_db_path).resolve():
        raise ValueError('benchmark must be separate from live database')
    if Path(bench_path).exists():
        raise ValueError('benchmark already exists; immutable plans cannot be replaced')
    if preset not in PRESETS:
        raise ValueError('unknown benchmark preset')
    custom_selection = preset != 'standard' or arms is not None or target_ids is not None or target_count is not None
    selected_names = list(PRESETS[preset]['arms'] if arms is None else arms)
    known_arms = {a[0]: a for a in CUSTOM_ARMS}
    if not selected_names or len(set(selected_names)) != len(selected_names) or any(a not in known_arms for a in selected_names):
        raise ValueError('arms must be distinct supported arm names')
    selected_arms = [known_arms[a] for a in selected_names]
    directions = tuple(directions) if directions is not None else (PRESETS[preset]['direction'],)
    if not directions or len(set(directions)) != len(directions) or any(d not in ('following','followers') for d in directions):
        raise ValueError('invalid directions')
    if any(size and size > 200 for _,_,size in selected_arms) and directions != ('following',):
        raise ValueError('larger Chrome pages are authorized for following only')
    if preset != 'standard' and directions != (PRESETS[preset]['direction'],):
        raise ValueError('preset direction cannot be changed; use custom arms instead')
    if pages_per_run < 1 or (custom_selection and pages_per_run > 2):
        raise ValueError('feasibility plans require one or two pages per run')
    requested_ids = None
    if target_ids is not None:
        requested_ids = [_id(value) for value in target_ids]
        if not requested_ids or None in requested_ids or len(set(requested_ids)) != len(requested_ids):
            raise ValueError('target IDs must be distinct numeric Instagram IDs')
        if target_count is not None and target_count != len(requested_ids):
            raise ValueError('target count does not match explicit IDs')
    target_count = len(requested_ids) if requested_ids is not None else (target_count if target_count is not None else 2)
    if custom_selection and not 1 <= target_count <= 3:
        raise ValueError('feasibility corpus must contain one to three targets')
    expected_targets = target_count if custom_selection else 12
    planned_requests = len(selected_arms)*len(directions)*(1+expected_targets*pages_per_run)
    max_requests = planned_requests if max_requests is None else max_requests
    window_seconds = (900 if custom_selection else 3600) if window_seconds is None else window_seconds
    if max_requests < 1 or window_seconds <= 0:
        raise ValueError('positive bounds required')
    if custom_selection and max_requests > planned_requests:
        raise ValueError('feasibility request cap cannot exceed its declared plan')
    rng = random.Random(seed)
    live = sqlite3.connect(Path(live_db_path).resolve().as_uri() + '?mode=ro', uri=True)
    live.row_factory = sqlite3.Row
    try:
        live.execute('BEGIN')
        candidates = list(corpus) if corpus is not None else [dict(r) for r in live.execute('SELECT ig_id,handle,following,is_private,is_verified FROM people ORDER BY ig_id')]
        buckets = {f'{size}_{verified}': [] for size in ('small','medium','large') for verified in ('verified','unverified')}
        seen = set()
        for r in sorted(candidates, key=lambda r: str(r.get('ig_id', ''))):
            pid = _id(r.get('ig_id'))
            if not pid or pid == viewer_id or pid in seen or r.get('is_private') != 0 or r.get('is_verified') not in (0,1) or r.get('following') is None or int(r['following']) < 0:
                continue
            seen.add(pid)
            r = dict(r, ig_id=pid)
            buckets[_stratum(r)].append(r)
        fills = []
        if custom_selection:
            eligible = {r['ig_id']: dict(r,stratum=key) for key,bucket in buckets.items() for r in bucket}
            if requested_ids is not None:
                missing = [pid for pid in requested_ids if pid not in eligible]
                if missing:
                    raise ValueError('explicit targets missing or ineligible: '+','.join(missing))
                chosen = [eligible[pid] for pid in requested_ids]
            else:
                # Feasibility favors targets that can expose page-size truncation.
                pool = list(eligible.values())
                rng.shuffle(pool)
                pool.sort(key=lambda r: int(r['following']), reverse=True)
                if len(pool) < target_count:
                    raise ValueError('not enough eligible public feasibility targets')
                chosen = pool[:target_count]
        else:
            chosen = []
            fills = []
            for size in ('small','medium','large'):
                selected = []
                for verified in ('verified','unverified'):
                    key = size+'_'+verified
                    bucket = buckets[key]
                    rng.shuffle(bucket)
                    selected.extend(dict(r,stratum=key) for r in bucket[:2])
                if len(selected) < 4:
                    used = {r['ig_id'] for r in selected}
                    pool = [dict(r,stratum=key) for key,bucket in buckets.items() if key.startswith(size+'_') for r in bucket if r['ig_id'] not in used]
                    rng.shuffle(pool)
                    fills.append({'size':size,'same_size_verification_fill':4-len(selected)})
                    selected.extend(pool[:4-len(selected)])
                if len(selected) != 4:
                    raise ValueError('corpus shortage: '+size+' requires four eligible targets')
                chosen.extend(selected)
        baseline = set()
        # Snapshot identity resolution is explicit; historic handle-only rows cannot prove capture-time identity.
        for r in live.execute('SELECT s.ig_id seed_id,p.ig_id person_id,e.direction FROM edges e JOIN people p ON p.id=e.person_id LEFT JOIN seeds s ON s.handle=e.seed'):
            a,b = _id(r['seed_id']), _id(r['person_id'])
            if a and b and r['direction'] in ('following','followers'):
                baseline.add((a,b) if r['direction']=='following' else (b,a))
    finally:
        live.close()
    rng.shuffle(chosen)
    arm_order = list(selected_arms)
    rng.shuffle(arm_order)
    tasks = []
    for warmup, targets in ((True, chosen[:1]), (False, chosen)):
        for index,target in enumerate(targets):
            order = arm_order[index % len(arm_order):] + arm_order[:index % len(arm_order)]
            if warmup and preset == 'chrome-large-following':
                order = sorted(selected_arms, key=lambda arm: arm[2])
            for arm,route,size in order:
                run_directions = list(directions)
                if (index + seed) % 2:
                    run_directions.reverse()
                for direction in run_directions:
                    tasks.append((arm,route,size,target['ig_id'],target['handle'],direction,int(warmup),'chrome' if route=='web_rest' else 'mobile'))
    meta = dict(version=2,preset=preset,arms=[list(a) for a in selected_arms],planned_requests=planned_requests,
                corpus_mode='explicit_ids' if requested_ids is not None else 'largest_following_feasibility' if custom_selection else 'stratified_12',
                feasibility_only=custom_selection,seed=seed,viewer_id=viewer_id,state='ready',phase='warmup',created_at=time.time(),started_at=None,ended_at=None,
                max_requests=max_requests,window_seconds=window_seconds,pages_per_run=pages_per_run,corpus=chosen,
                exclusions=EXCLUSIONS,corpus_fills=fills,fill_policy='explicit eligible target IDs, no substitutions' if requested_ids is not None else 'largest following counts among eligible public targets; feasibility only' if custom_selection else 'aim two verified and two unverified per size; fill missing slots within same size',directions=list(directions),fingerprints=fingerprints or {},baseline_count=len(baseline),
                baseline_identity='numeric IDs resolved from seeds and people at snapshot; historical identity not proven',
                baseline_sha256=hashlib.sha256(json.dumps(sorted(baseline)).encode()).hexdigest())
    Path(bench_path).parent.mkdir(parents=True,exist_ok=True)
    with _connect(bench_path) as db:
        db.executescript('''
        CREATE TABLE metadata(value TEXT NOT NULL);
        CREATE TABLE baseline(src TEXT,dst TEXT,PRIMARY KEY(src,dst));
        CREATE TABLE tasks(task_id INTEGER PRIMARY KEY,arm TEXT,route TEXT,page_size INT,target_id TEXT,target_handle TEXT,direction TEXT,warmup INT,transport TEXT,cursor TEXT,state TEXT DEFAULT 'pending',pages INT DEFAULT 0,lane TEXT);
        CREATE TABLE requests(request_id TEXT PRIMARY KEY,task_id INT,role TEXT,started_at REAL,finished_at REAL,status TEXT,result TEXT,fingerprints TEXT);
        CREATE TABLE observations(request_id TEXT,src TEXT,dst TEXT,PRIMARY KEY(request_id,src,dst));
        CREATE TABLE novel(arm TEXT,src TEXT,dst TEXT,PRIMARY KEY(arm,src,dst));
        CREATE TABLE waits(request_id TEXT,reason TEXT,seconds REAL);
        CREATE TABLE cursors(task_id INT,cursor TEXT,PRIMARY KEY(task_id,cursor));
        ''')
        db.execute('INSERT INTO metadata VALUES(?)',(json.dumps(meta,sort_keys=True),))
        db.executemany('INSERT INTO baseline VALUES(?,?)',sorted(baseline))
        db.executemany('INSERT INTO tasks(arm,route,page_size,target_id,target_handle,direction,warmup,transport) VALUES(?,?,?,?,?,?,?,?)',tasks)
    return report(bench_path)


def next_task(bench_path, lane, viewer, transport='chrome', now=None):
    now = time.time() if now is None else now
    with _connect(bench_path) as db:
        db.execute('BEGIN IMMEDIATE')
        m = _meta(db)
        if _id(viewer) != m['viewer_id']:
            raise ValueError('viewer mismatch')
        if m['state'] in ('stopped','complete'):
            return None
        if m['started_at'] is None:
            m.update(started_at=now,state='running')
            _save(db,m)
        if now-m['started_at'] >= m['window_seconds'] or db.execute('SELECT count(*) FROM requests').fetchone()[0] >= m['max_requests']:
            _stop(db,m,'window_or_request_bound',now)
            return None
        if db.execute('SELECT 1 FROM requests WHERE finished_at IS NULL').fetchone():
            return None
        row=db.execute("SELECT * FROM tasks WHERE state!='done' ORDER BY task_id LIMIT 1").fetchone()
        if row is None:
            m.update(state='complete',ended_at=now)
            _save(db,m)
            return None
        if not row['warmup'] and m.get('phase') != 'measured':
            return None
        if row['transport'] != transport or (row['lane'] and row['lane'] != lane):
            return None
        db.execute('UPDATE tasks SET lane=? WHERE task_id=?',(lane,row['task_id']))
        return dict(row, lane=lane,viewer_id=m['viewer_id'])


def begin_request(bench_path, request_id, task_id, viewer_id, *, role='page', started_at=None, fingerprints=None):
    now=time.time() if started_at is None else started_at
    with _connect(bench_path) as db:
        db.execute('BEGIN IMMEDIATE')
        m=_meta(db)
        old=db.execute('SELECT * FROM requests WHERE request_id=?',(request_id,)).fetchone()
        if old:
            return dict(old,send_allowed=False)
        task=db.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone()
        first=db.execute("SELECT task_id FROM tasks WHERE state!='done' ORDER BY task_id LIMIT 1").fetchone()
        if _id(viewer_id)!=m['viewer_id'] or m['state']!='running' or not task or task['state']=='done' or not first or first[0]!=task_id:
            raise ValueError('inactive plan, task, or viewer mismatch')
        if db.execute('SELECT 1 FROM requests WHERE finished_at IS NULL').fetchone():
            raise ValueError('request outstanding; never replay uncertain sends')
        if now-m['started_at'] >= m['window_seconds'] or db.execute('SELECT count(*) FROM requests').fetchone()[0]>=m['max_requests']:
            _stop(db,m,'window_or_request_bound',now)
            return {'send_allowed':False,'status':'stopped'}
        if not task['warmup'] and m.get('phase') != 'measured':
            raise ValueError('measured phase not opened')
        if role not in ('page','auxiliary'):
            raise ValueError('role must be page or auxiliary')
        db.execute('INSERT INTO requests VALUES(?,?,?,?,NULL,NULL,NULL,?)',(request_id,task_id,role,now,json.dumps(fingerprints or {},sort_keys=True)))
        return {'request_id':request_id,'task_id':task_id,'send_allowed':True}


def finish_request(bench_path, request_id, *, rows=None, next_cursor=None, has_more=False, status='ok', terminal_warning=None, finished_at=None, waits=None, actual_http_requests=1, http_status=None, duration_ms=None, transport_completed=True, **metrics):
    now=time.time() if finished_at is None else finished_at
    with _connect(bench_path) as db:
        db.execute('BEGIN IMMEDIATE')
        req=db.execute('SELECT * FROM requests WHERE request_id=?',(request_id,)).fetchone()
        if not req:
            raise ValueError('unregistered request')
        if req['finished_at'] is not None:
            return json.loads(req['result'])
        if now < req['started_at']:
            raise ValueError('finish precedes start')
        m=_meta(db)
        t=db.execute('SELECT * FROM tasks WHERE task_id=?',(req['task_id'],)).fetchone()
        rows=rows or []
        cursor=str(next_cursor) if next_cursor not in (None,'') else None
        warning=terminal_warning
        if metrics.get('uncertain'):
            warning=warning or 'transport_uncertain'
        if actual_http_requests == 0:
            warning=warning or 'local_no_send'
        if actual_http_requests not in (None,0,1):
            warning=warning or 'unexpected_transport_count'
        if not transport_completed or actual_http_requests is None:
            warning=warning or 'transport_uncertain'
        if status!='ok':
            warning=warning or 'transport_'+str(status)
        if req['role']=='page' and status=='ok' and actual_http_requests == 1:
            if bool(has_more)!=bool(cursor):
                warning=warning or 'contradictory_or_missing_cursor'
            if cursor and (cursor==t['cursor'] or db.execute('SELECT 1 FROM cursors WHERE task_id=? AND cursor=?',(t['task_id'],cursor)).fetchone()):
                warning=warning or 'repeated_cursor'
        count=0
        unique_before=db.execute('SELECT count(*) FROM observations WHERE request_id=?',(request_id,)).fetchone()[0]
        for row in rows if req['role']=='page' and status=='ok' and actual_http_requests == 1 else []:
            pid=_id(row.get('ig_id',row.get('pk',row.get('id'))) if isinstance(row,dict) else row)
            if not pid:
                warning=warning or 'malformed_numeric_id'
                continue
            a,b=(t['target_id'],pid) if t['direction']=='following' else (pid,t['target_id'])
            db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?)',(request_id,a,b))
            if not t['warmup'] and not db.execute('SELECT 1 FROM baseline WHERE src=? AND dst=?',(a,b)).fetchone():
                count += db.execute('INSERT OR IGNORE INTO novel VALUES(?,?,?)',(t['arm'],a,b)).rowcount
        for reason,seconds in (waits or {}).items():
            if reason not in ('pacing','permit','cooldown','db') or float(seconds)<0:
                raise ValueError('invalid wait')
            db.execute('INSERT INTO waits VALUES(?,?,?)',(request_id,reason,float(seconds)))
        observed_pairs=db.execute('SELECT count(*) FROM observations WHERE request_id=?',(request_id,)).fetchone()[0]-unique_before
        result=dict(request_id=request_id,status=status,returned_rows=len(rows),observed_pairs=observed_pairs,novel_pairs=count,warmup=bool(t['warmup']),latency_seconds=now-req['started_at'],terminal_warning=warning,actual_http_requests=actual_http_requests,http_status=http_status,duration_ms=duration_ms,transport_completed=transport_completed,metrics=metrics)
        db.execute('UPDATE requests SET finished_at=?,status=?,result=? WHERE request_id=?',(now,status,json.dumps(result),request_id))
        if warning:
            _stop(db,m,str(warning),now)
        elif req['role']=='page':
            if cursor:
                db.execute('INSERT INTO cursors VALUES(?,?)',(t['task_id'],cursor))
            done=not has_more or t['pages']+1>=(1 if t['warmup'] else m['pages_per_run'])
            db.execute('UPDATE tasks SET pages=pages+1,cursor=?,state=? WHERE task_id=?',(cursor,'done' if done else 'pending',t['task_id']))
            if not db.execute("SELECT 1 FROM tasks WHERE state!='done'").fetchone():
                m.update(state='complete',ended_at=now)
                _save(db,m)
        return result


def stop(bench_path,reason,now=None):
    with _connect(bench_path) as db:
        db.execute('BEGIN IMMEDIATE')
        m=_meta(db)
        if m['state'] not in ('stopped','complete'):
            _stop(db,m,reason,time.time() if now is None else now)
    return report(bench_path,now=now)


def recover_outstanding(bench_path,now=None):
    with _connect(bench_path) as db:
        outstanding=[r[0] for r in db.execute('SELECT request_id FROM requests WHERE finished_at IS NULL')]
    if outstanding:
        stop(bench_path,'uncertain_outstanding_request',now=now)
    return outstanding


def report(bench_path,now=None):
    """Observed cohort rates, with warmups excluded and no extrapolated winner."""
    now=time.time() if now is None else now
    def percentile(values, fraction):
        if not values:
            return None
        values=sorted(values)
        at=(len(values)-1)*fraction
        lo=int(at)
        return values[lo]+(values[min(lo+1,len(values)-1)]-values[lo])*(at-lo)
    with _connect(bench_path) as db:
        m=_meta(db)
        end=m['ended_at'] if m['ended_at'] is not None else now
        elapsed=0 if m['started_at'] is None else max(0,end-m['started_at'])
        measured_start=m.get('measured_started_at')
        measured_wall=max(0,end-measured_start) if measured_start is not None else 0
        arms={arm:dict(requests=0,warmup_requests=0,returned_rows=0,novel_pairs=0,
                      latency_seconds=0,waits={},confirmed_http_requests=0,uncertain_requests=0,
                      successful_requests=0,errors={},allocated_wall_seconds=0,
                      pagination_completed=0,page_bound_reached=0,cursor_failures=0,
                      duplicate_rows=0,latencies=[]) for arm,_,_ in m.get('arms',ARMS)}
        totals=dict(registered_attempts=0,confirmed_http_requests=0,uncertain_attempts=0,
                    local_no_send=0,warmup_confirmed_http_requests=0)
        previous_end=measured_start
        warmup_results=[]
        for r in db.execute('SELECT r.*,t.arm,t.warmup FROM requests r JOIN tasks t USING(task_id) ORDER BY r.started_at,r.rowid'):
            a=arms[r['arm']]
            a['warmup_requests' if r['warmup'] else 'requests']+=1
            totals['registered_attempts']+=1
            result=json.loads(r['result']) if r['result'] else {}
            actual=result.get('actual_http_requests')
            uncertain=(actual is None or result.get('transport_completed') is False
                       or result.get('metrics',{}).get('uncertain') is True)
            totals['uncertain_attempts']+=int(uncertain)
            if actual==0:
                totals['local_no_send']+=1
            elif actual is not None:
                totals['confirmed_http_requests']+=actual
                if r['warmup']:
                    totals['warmup_confirmed_http_requests']+=actual
            if r['warmup']:
                warmup_results.append(dict(arm=r['arm'],actual_http_requests=actual,
                    returned_rows=result.get('returned_rows',0),status=result.get('status','outstanding'),
                    warning=result.get('terminal_warning'),duration_ms=result.get('duration_ms'),
                    uncertain=uncertain))
                continue
            finish=r['finished_at'] if r['finished_at'] is not None else end
            start=previous_end if previous_end is not None else r['started_at']
            a['allocated_wall_seconds']+=max(0,finish-start)
            previous_end=finish
            a['confirmed_http_requests']+=actual or 0
            a['uncertain_requests']+=int(uncertain)
            a['returned_rows']+=result.get('returned_rows',0)
            a['latency_seconds']+=result.get('latency_seconds',0)
            duration=result.get('duration_ms')
            if isinstance(duration,(int,float)) and duration>=0:
                a['latencies'].append(duration/1000)
            error=result.get('terminal_warning') or (None if result.get('status')=='ok' else result.get('status','outstanding'))
            if error:
                a['errors'][error]=a['errors'].get(error,0)+1
                a['cursor_failures']+=int('cursor' in error)
            elif actual==1:
                a['successful_requests']+=1
        # Assign still-unfinished waiting to the next scheduled measured arm.
        pending=db.execute("SELECT arm,warmup FROM tasks WHERE state!='done' ORDER BY task_id LIMIT 1").fetchone()
        if measured_start is not None and pending and not pending['warmup']:
            arms[pending['arm']]['allocated_wall_seconds']+=max(0,end-(previous_end if previous_end is not None else measured_start))
        for r in db.execute('SELECT arm,count(*) n FROM novel GROUP BY arm'):
            arms[r['arm']]['novel_pairs']=r['n']
        for r in db.execute('SELECT t.arm,w.reason,sum(w.seconds) seconds FROM waits w JOIN requests r USING(request_id) JOIN tasks t USING(task_id) WHERE t.warmup=0 GROUP BY t.arm,w.reason'):
            arms[r['arm']]['waits'][r['reason']]=r['seconds']
        for r in db.execute("SELECT arm,cursor FROM tasks WHERE warmup=0 AND state='done'"):
            arms[r['arm']]['page_bound_reached' if r['cursor'] else 'pagination_completed']+=1
        for a in arms.values():
            http=a['confirmed_http_requests']; wall=a['allocated_wall_seconds']
            a['duplicate_rows']=max(0,a['returned_rows']-a['novel_pairs'])
            a['duplicate_edge_percent']=100*a['duplicate_rows']/a['returned_rows'] if a['returned_rows'] else None
            a['rows_per_http_request']=a['returned_rows']/http if http else None
            a['unique_edges_per_http_request']=a['novel_pairs']/http if http else None
            a['observed_unique_edges_per_allocated_hour']=a['novel_pairs']*3600/wall if wall else None
            a['successful_requests_per_allocated_hour']=a['successful_requests']*3600/wall if wall else None
            a['unique_edges_per_full_measured_cohort_hour']=a['novel_pairs']*3600/measured_wall if measured_wall else None
            a['error_rates']={key:value/a['requests'] for key,value in a['errors'].items()} if a['requests'] else {}
            a['latency_p50_seconds']=percentile(a['latencies'],.5)
            a['latency_p95_seconds']=percentile(a.pop('latencies'),.95)
        return dict(plan=m,arms=arms,transport_totals=totals,warmup_results=warmup_results,
                    wall_seconds_including_all_waits=elapsed,measured_wall_seconds=measured_wall,
                    denominator='Warmups excluded from arm metrics. Allocated wall time assigns inter-request waits to the next scheduled arm; rates describe this bounded cohort, not sustained independent runs.',
                    pending_tasks=db.execute("SELECT count(*) FROM tasks WHERE state!='done'").fetchone()[0],
                    outstanding_requests=db.execute('SELECT count(*) FROM requests WHERE finished_at IS NULL').fetchone()[0],
                    sustainable_winner=None,
                    completion_note='A page-bound stop is partial coverage, not a completed list. Repeated independent windows are required for a sustainable winner.')


def state(bench_path):
    """Cheap control-plane state; no report aggregation."""
    with _connect(bench_path) as db:
        m = _meta(db)
        task = db.execute("SELECT * FROM tasks WHERE state!='done' ORDER BY task_id LIMIT 1").fetchone()
        inflight = db.execute('SELECT * FROM requests WHERE finished_at IS NULL').fetchone()
        return dict(m, current_task=dict(task) if task else None, inflight=dict(inflight) if inflight else None)


def advance_phase(bench_path, now=None):
    """Explicitly open measured phase after excluded warmup."""
    with _connect(bench_path) as db:
        db.execute('BEGIN IMMEDIATE')
        m = _meta(db)
        if m['state'] in ('stopped','complete') or db.execute("SELECT 1 FROM tasks WHERE warmup=1 AND state!='done'").fetchone() or db.execute('SELECT 1 FROM requests WHERE finished_at IS NULL').fetchone():
            raise ValueError('warmup incomplete or plan stopped')
        m.update(phase='measured',measured_started_at=time.time() if now is None else now)
        _save(db,m)
    return state(bench_path)
