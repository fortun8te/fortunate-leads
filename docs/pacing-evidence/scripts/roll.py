import json, datetime as dt, bisect
rows=json.load(open('timeline.json'))
def fmt(t): return dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime('%m-%d %H:%M')
req=[r for r in rows if r['kind']=='list' and r['outcome']=='page']
def rolling(rs,w,step=60):
    ts=sorted(r['t'] for r in rs)
    best=[]
    if not ts: return best
    t=ts[0]; end=ts[-1]
    out=[]
    while t<=end:
        lo=bisect.bisect_left(ts,t); hi=bisect.bisect_left(ts,t+w)
        out.append((hi-lo,t))
        t+=step
    return out
for lane in ['ALL','fortun8te','dihfluencer','turtles']:
    rs=[r for r in req if lane=='ALL' or r['lane']==lane]
    for w,name in ((3600,'1h'),(7200,'2h'),(3*3600,'3h'),(6*3600,'6h'),(24*3600,'24h')):
        o=rolling(rs,w,300 if w>=3*3600 else 60)
        if not o: continue
        n,t=max(o)
        users=sum(r['users'] or 0 for r in rs if t<=r['t']<t+w)
        print(lane,name,'max pages',n,'starting',fmt(t),'entries',users)
