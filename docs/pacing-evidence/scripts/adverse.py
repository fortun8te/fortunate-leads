import json, datetime as dt, bisect
rows=json.load(open('timeline.json'))
rows.sort(key=lambda r:r['t'])
def fmt(t): return dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime('%m-%d %H:%M:%S')
def ams(t): return (dt.datetime.fromtimestamp(t,dt.timezone.utc)+dt.timedelta(hours=2)).strftime('%H:%M')
adv=[r for r in rows if r['outcome'].split(':')[0] in ('rate_limit','other') and not r['outcome'].startswith('other:list_html')]
adv+= [r for r in rows if r['outcome'].startswith('other:list_html')]
adv.sort(key=lambda r:r['t'])
def stats(r,w):
    lo=r['t']-w
    out={}
    for lane in ('fortun8te','dihfluencer','turtles','ALL'):
        rs=[x for x in rows if lo<=x['t']<r['t'] and (lane=='ALL' or x['lane']==lane) and x['outcome']!='rate_limit']
        l=sum(1 for x in rs if x['kind']=='list'); p=sum(1 for x in rs if x['kind']=='profile')
        out[lane]=(l,p)
    return out
for r in adv:
    if r['outcome'].startswith('other:list_html') : continue
    print(fmt(r['t']),'(Ams %s)'%ams(r['t']), r['lane'], r['kind'], r['outcome'], 'job',r['job'])
    for w,name in ((300,'5m'),(3600,'1h'),(86400,'24h')):
        s=stats(r,w)
        print('   ',name, {k:v for k,v in s.items()})
