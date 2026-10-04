import json, datetime as dt, collections, sqlite3
rows=json.load(open('timeline.json'))
c=sqlite3.connect("file:"+__import__("os").environ.get("PACING_SNAPSHOT","/Users/michael/Projects/Fortunate-Leads/work/oct4-reports/data/snapshot/leads.sqlite")+"?mode=ro",uri=True)
jobs={r[0]:r for r in c.execute("select id, seed, direction from jobs")}
def fmt(t): return dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime('%m-%d %H:%M:%S')
pagesbyjob=collections.defaultdict(list)
for r in rows:
    if r['kind']=='list' and r['outcome']=='page': pagesbyjob[r['job']].append(r)
red=[r for r in rows if r['outcome'].startswith('other:list_html_home_redirect')]
# group by job
byjob=collections.defaultdict(list)
for r in red: byjob[(r['lane'],r['job'])].append(r)
for (lane,job),rs in sorted(byjob.items(), key=lambda x:x[1][0]['t']):
    pages=pagesbyjob.get(job,[])
    first=rs[0]
    before=[p for p in pages if p['t']<first['t']]
    last=max((p['t'] for p in before), default=None)
    print(fmt(first['t']), lane, 'job',job, jobs.get(job,(0,'?','?'))[1:], 'redirects',len(rs), 'pages_before(all lanes)',len(before), 'last page %s'%(fmt(last) if last else None), 'after>%d'%len([p for p in pages if p['t']>first['t']]))
