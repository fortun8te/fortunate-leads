import sys, os, time, json, random, statistics as S, string
from datetime import datetime, timezone, timedelta
os.environ['FL_NO_ORSLOT']='1'
SRV=os.environ.get('PACING_SERVER_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)),'..','..','..','server'))
sys.path.insert(0,SRV)
import db, server, accounts
PATH=os.environ['PACING_CLONE']  # an APFS clone of the snapshot; this script writes to it
conn=db.connect(PATH)
conn.executescript("CREATE TABLE IF NOT EXISTS pipeline_events(id INTEGER PRIMARY KEY, v INT NOT NULL DEFAULT 1, at TEXT NOT NULL, code TEXT NOT NULL, lane TEXT, kind TEXT, ig_id TEXT, job_id INT, direction TEXT, token_ref TEXT, reason TEXT);")
LANE='ln_sqrscja8tx6z'; IG='64189916997'
now=datetime.now(timezone.utc)
# clone-only setup: lists on, bios off, no cooldown, clean gate
for k,v in (('paused',False),('paused_lists',False),('paused_bios',True),('cooldown',None),
            ('instagram_request_attention',None),('instagram_request_gate',{'active':None,'queue':[],'next_at':0}),
            ('instagram_collection_isolation',{'accounts':{LANE:IG},'collection_stages':['lists'],'at':db.now()})):
    db.set_setting(conn,k,v)
conn.execute("UPDATE accounts SET paused=0,hold=NULL,state='idle',cooldown_until=NULL,list_cool_until=NULL,profile_cool_until=NULL,list_endpoint_until=NULL WHERE lane_id=?",(LANE,))
db.set_setting(conn,'budget',{'list':0,'profile':100})
conn.execute("UPDATE accounts SET budget=? WHERE lane_id=?",(json.dumps({'list':0,'profile':100}),LANE))
conn.commit()
q={'lane':[LANE],'ig_id':[IG],'handle':['trashtheturtles']}
acct={'ig_id':IG,'handle':'trashtheturtles'}
def hb():
    st=datetime.now(timezone.utc)
    b={'lane_id':LANE,'account':acct,'version':'3.9.30','state':'idle','tab':'ok','cooldown_until':None,'cool':{'list':None,'profile':None},
       'hold':None,'list_endpoint_until':None,'day':st.strftime('%Y-%m-%d'),'today':{'list':3,'profile':0},'budget':{'list':0,'profile':100},
       'last_error':None,'activity':'@x following · page 2','text':'Waiting','people_today':100,
       'rate':{'pages_hour':120,'people_hour':20000,'bios_hour':0,'requests_hour':130,'hits_24h':0,'last_hit_at':None},
       'ready':{'list':db.now(),'profile':db.now()}}
    return server.ext_heartbeat(conn,dict(q),b)
def nxt():
    return server.ext_next(conn,{**q,'kinds':['list']},{})
def acq(job):
    return server.ext_request(conn,dict(q),{'lane_id':LANE,'account':acct,'action':'acquire','kind':'list','job_id':job['id'],'lease_token':job['lease_token']})
def rel(tok):
    return server.ext_request(conn,dict(q),{'lane_id':LANE,'account':acct,'action':'release','token':tok})
existing=[r[0] for r in conn.execute("SELECT handle FROM people ORDER BY random() LIMIT 4000")]
def rnd(n): return ''.join(random.choice(string.ascii_lowercase) for _ in range(n))
def users(n_exist,n_new):
    u=[]
    for h in random.sample(existing,n_exist):
        pid=conn.execute('SELECT ig_id FROM people WHERE handle=?',(h,)).fetchone()
        u.append({'handle':h,'ig_id':pid[0] if pid and pid[0] else str(random.randint(10**9,10**10)),'name':h,'is_private':False,'is_verified':False,'pic_url':None})
    for _ in range(n_new):
        h='zz_'+rnd(10); u.append({'handle':h,'ig_id':str(random.randint(10**11,10**12)),'name':h,'is_private':False,'is_verified':False,'pic_url':None})
    return u
def page(job,cursor,nxtc,n_new,n_exist):
    return server.ext_list_page(conn,dict(q),{'lane_id':LANE,'account':acct,'job_id':job['id'],'lease_token':job['lease_token'],'requested_cursor':cursor,
        'seed':job['seed'],'ig_id':job.get('ig_id'),'direction':job['direction'],'users':users(n_exist,n_new),'next_cursor':nxtc,'done':False,'has_more':True,
        'requested_count':job.get('page_size') or 200,'http_status':200,'captured_at':db.now(),'total':5000,'total_source':'cached'})
T={k:[] for k in ('heartbeat','next','acquire','release','list_page')}
def timed(k,f,*a):
    t=time.perf_counter(); r=f(*a); T[k].append((time.perf_counter()-t)*1000); return r
N=int(sys.argv[1]) if len(sys.argv)>1 else 25
r=timed('heartbeat',hb)
print('hb state', {k:r.get(k) for k in ('paused','stages','cooldown_until')})
r=timed('next',nxt)
job=r.get('job'); print('job',job and {k:job[k] for k in ('id','seed','direction','cursor','page_size')}, {k:r.get(k) for k in ('paused','stages')})
if not job: sys.exit('no job')
cur=job.get('cursor')
for i in range(N):
    timed('heartbeat',hb)
    if i:
        r=timed('next',nxt); job=r.get('job')
        if not job: print('no job',r); break
        cur=job.get('cursor')
    g=timed('acquire',acq,job)
    if not g.get('granted'):
        print('acquire',g); time.sleep(2.1); g=timed('acquire',acq,job)
        if not g.get('granted'): print('still not granted',g); break
    timed('release',rel,g['token'])
    nc='x%d_%s'%(i,rnd(6))
    res=timed('list_page',page,job,cur,nc,100,100)
    cur=nc
    if res.get('stale') or res.get('duplicate'): print('page res',res); break
    time.sleep(2.1)   # permit spacing
for k,v in T.items():
    if v: print('%-10s n=%2d  median %7.1f ms  p90 %7.1f ms  max %7.1f ms'%(k,len(v),S.median(v),sorted(v)[int(len(v)*.9)-1 if len(v)>1 else 0],max(v)))
