"""Streaming large-map benchmark. Retained synthetic DB allows isolated stage RSS measurements."""
import argparse
import hashlib
import json
import resource
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db
import map_layout as ML
import map_view as MV

STAMP = '2026-01-01T00:00:00+00:00'
RESERVE = 8 * 1024**3


def guard(directory):
    if shutil.disk_usage(directory).free < RESERVE:
        raise RuntimeError('Synthetic benchmark stopped: fewer than 8 GiB free')


def fixture(path, people, batch=25000):
    if path.exists():
        raise RuntimeError('Fixture already exists; use build or requests stage to preserve it')
    conn = db.init(path)
    try:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            conn.execute('DROP TRIGGER "' + name.replace('"','""') + '"')
        conn.execute("DELETE FROM settings WHERE key LIKE 'map_%_v1'")
        conn.commit()
        conn.execute('PRAGMA journal_mode=OFF')
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('PRAGMA temp_store=FILE')
        conn.execute('PRAGMA cache_size=-32768')
        conn.execute("INSERT INTO seeds(handle,added_at,is_me) VALUES('fortun8te',?,1)",(STAMP,))
        conn.executemany('INSERT INTO seeds(handle,added_at) VALUES(?,?)',((f'src{i:02}',STAMP) for i in range(30)))
        conn.executemany('INSERT INTO people(id,handle,name,first_seen,updated_at) VALUES(?,?,?,?,?)',
                         ((i+1,h,h,STAMP,STAMP) for i,h in enumerate(['fortun8te']+[f'src{i:02}' for i in range(30)])))
        conn.execute('CREATE TEMP TABLE batch_ids(id INTEGER PRIMARY KEY)')
        for low in range(32, people+1, batch):
            guard(path.parent)
            high = min(people,low+batch-1)
            conn.execute('DELETE FROM batch_ids')
            conn.execute('WITH RECURSIVE n(i) AS (SELECT ? UNION ALL SELECT i+1 FROM n WHERE i<?) INSERT INTO batch_ids SELECT i FROM n',(low,high))
            conn.execute("INSERT INTO people(id,handle,name,followers,first_seen,updated_at) SELECT id,printf('user%09d',id),printf('Person %d',id),100+(id*7919)%200000,?,? FROM batch_ids",(STAMP,STAMP))
            conn.execute("INSERT INTO edges SELECT printf('src%02d',CASE WHEN id%100<40 THEN 0 ELSE id%30 END),id,CASE WHEN id%2=0 THEN 'following' ELSE 'followers' END,? FROM batch_ids",(STAMP,))
            conn.execute("INSERT OR IGNORE INTO edges SELECT printf('src%02d',(id%30+1)%30),id,'following',? FROM batch_ids WHERE id%7=0",(STAMP,))
            conn.execute("INSERT INTO edges SELECT 'fortun8te',id,'followers',? FROM batch_ids WHERE id%50=0",(STAMP,))
            conn.execute('INSERT INTO edge_evidence SELECT e.seed,e.person_id,e.direction,1,?,? FROM edges e WHERE e.person_id BETWEEN ? AND ?',(STAMP,STAMP,low,high))
            conn.execute("INSERT INTO verdicts(person_id,score,tier,content_fit) SELECT id,(id*31)%101,'maybe',CASE WHEN id%100=0 THEN NULL ELSE (id*17)%101 END FROM batch_ids WHERE id%10=0")
            conn.execute("INSERT INTO marks(person_id,status,updated_at) SELECT id,CASE id%600 WHEN 0 THEN 'client' WHEN 100 THEN 'talking' WHEN 200 THEN 'contacted' WHEN 300 THEN 'interested' WHEN 400 THEN 'no' ELSE 'spoke_before' END,? FROM batch_ids WHERE id%100=0",(STAMP,))
            conn.commit()
            if high%1000000 < batch:
                print(json.dumps({'stage':'fixture','loaded':high}),flush=True)
        conn.execute('UPDATE person_id_sequence SET value=?',(people,))
        conn.commit()
    finally:
        conn.close()
    # Reinstall the real production summaries/triggers, with disk-backed temporary sorting.
    connect = db.connect
    def bounded_connect(p):
        c = connect(p);c.execute('PRAGMA temp_store=FILE');c.execute('PRAGMA cache_size=-32768');return c
    db.connect = bounded_connect
    try:
        c = db.init(path);c.execute('ANALYZE');c.commit();c.close()
    finally:
        db.connect = connect


def requests(path, runs):
    conn = db.connect(path)
    result = {}
    cases = {'world': {'scope':['all']}, 'qualified':{'scope':['leads'],'min_fit':['70']},
             'clients':{'scope':['all'],'status':['client']},
             'zoom':{'scope':['all'],'x0':['.3'],'y0':['.3'],'x1':['.45'],'y1':['.45']},
             'pan':{'scope':['all'],'x0':['.32'],'y0':['.3'],'x1':['.47'],'y1':['.45']},
             'maximum':{'scope':['all'],'budget':['1500']},
             'qualified_zoom':{'scope':['leads'],'min_fit':['70'],'x0':['.2'],'y0':['.2'],'x1':['.55'],'y1':['.55']},
             'clients_zoom':{'scope':['all'],'status':['client'],'x0':['.2'],'y0':['.2'],'x1':['.55'],'y1':['.55']}}
    try:
        result['counts'] = {t:conn.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('people','edges','map_seed_member')}
        for mode in ML.MODES:
            if not ML.mode_exists(path,mode):continue
            result[mode] = {}
            for name,q in cases.items():
                samples=[]
                for _ in range(runs):
                    start=time.perf_counter();raw=MV.view(conn,path,dict(q,mode=[mode]),cache=False);samples.append(round((time.perf_counter()-start)*1000,2))
                body=json.loads(raw.body)
                assert body['ready'] is True, 'Benchmark requires a completed current layout'
                assert len(body['nodes']) <= int(q.get('budget',['600'])[0])
                assert sum(b['count'] for b in body['clusters']) == body['hidden']
                result[mode][name]={'samples_ms':samples,'bytes':len(raw.body),'nodes':len(body['nodes']),
                                    'groups':len(body['clusters']),'total':body['total'],'hidden':body['hidden']}
        assert 'closeness' in result, 'Default layout must exist before request measurements'
        import server
        server.CFG['db'] = str(path)
        population = result['counts']['people']
        expected_total = population - (((population-400)//600)+1 if population >= 400 else 0) - 1
        # Independent fixture oracle: score 100 occurs at id=720+1010*k.
        # These beat all other scores; follower count and id settle their ties.
        expected_ids = sorted((i for i in range(720,population+1,1010) if i%600 != 400),
                              key=lambda i: (-(100+(i*7919)%200000),i))[:100] if population >= 200000 else None
        def fixture_degree(person_id):
            seeds={0 if person_id%100<40 else person_id%30}
            if person_id%7==0:seeds.add((person_id%30+1)%30)
            if person_id%50==0:seeds.add('owner')
            return len(seeds)
        # Fit 100 occurs at id=600+1010*k, except the deliberately unread every-100th row.
        fit_ids = sorted((i for i in range(600,population+1,1010) if i%100 !=0),
                         key=lambda i:(-fixture_degree(i),i))[:100] if population>=200000 else None
        for sort, oracle in (('score',expected_ids),('fit',fit_ids)):
            pages={}
            for offset in (0,50):
                start=time.perf_counter();page=server.api_leads(conn,{'sort':[sort],'offset':[str(offset)]},{})
                elapsed=round((time.perf_counter()-start)*1000,2)
                assert page['total']==expected_total
                if oracle is not None:
                    assert [row['id'] for row in page['rows']]==oracle[offset:offset+50]
                pages[str(offset)]={'milliseconds':elapsed,'rows':len(page['rows']),
                                    'total':page['total'],'next_offset':page['next_offset'],
                                    'fixture_oracle_checked':oracle is not None}
            result['leads' if sort=='score' else 'leads_fit']=pages
        result['initial_view'] = {}
        for name, function in (('tag_facets', server.tag_facets), ('counts', server.counts)):
            samples=[]
            for _ in range(runs):
                start=time.perf_counter();payload=function(conn,{})
                samples.append(round((time.perf_counter()-start)*1000,2))
            if name == 'counts':
                assert payload['total'] == population
                assert payload['open'] == expected_total
            result['initial_view'][name]={'samples_ms':samples,'payload':payload}
        for query in ('user000123','Person 123','src00'):

            start=time.perf_counter();found=MV.search(conn,path,{'q':[query]})
            result['search_'+query]={'milliseconds':round((time.perf_counter()-start)*1000,2),'results':len(found['results'])}
    finally:
        conn.close();MV.close_pool()
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--people',type=int,default=1000001)
    parser.add_argument('--stage',choices=['fixture','build','requests','all'],default='all')
    parser.add_argument('--modes',default=','.join(ML.MODES))
    parser.add_argument('--runs',type=int,default=5)
    parser.add_argument('--fresh',action='store_true',help='discard an interrupted synthetic layout build')
    args=parser.parse_args()
    if args.people < 32 or args.runs < 1:
        parser.error('people must be at least 32 and runs must be positive')
    args.directory.mkdir(parents=True,exist_ok=True)
    path=args.directory/'synthetic.sqlite';guard(args.directory)
    start=time.perf_counter();output={'stage':args.stage,'path':str(path),'requested_people':args.people,
        'layout_schema':ML.SCHEMA_VERSION, 'layout_source_sha256':hashlib.sha256(Path(ML.__file__).read_bytes()).hexdigest()}
    if args.stage in ('fixture','all'):fixture(path,args.people);output['fixture_seconds']=round(time.perf_counter()-start,2)
    if args.stage in ('build','all'):
        output['build']=ML.build(path,modes=args.modes.split(','),chunk=25000,workers=1,fresh=args.fresh)
    if args.stage in ('requests','all'):output['requests']=requests(path,args.runs)
    output['elapsed_seconds']=round(time.perf_counter()-start,2)
    output['peak_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform=='darwin' else 1024)
    output['disk_bytes']=sum(p.stat().st_size for p in args.directory.rglob('*') if p.is_file())
    output['disk_free_bytes']=shutil.disk_usage(args.directory).free
    (args.directory/f'{args.stage}-results.json').write_text(json.dumps(output,indent=2))
    print(json.dumps(output,indent=2),flush=True)


if __name__=='__main__':main()
