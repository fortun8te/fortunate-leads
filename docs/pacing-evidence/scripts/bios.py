import json, datetime as dt, statistics as S, collections
rows=json.load(open('timeline.json'))
def fmt(t): return dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime('%m-%d %H:%M:%S')
pr=[r for r in rows if r['kind']=='profile' and (r['outcome'].startswith('profile') or r['outcome'].startswith('not_found') or r['outcome'].startswith('other'))]
byl=collections.defaultdict(list)
for r in pr: byl[r['lane']].append(r)
for lane,rs in byl.items():
    rs.sort(key=lambda x:x['t'])
    print('==',lane,len(rs))
    sess=[[rs[0]]]
    for a,b in zip(rs,rs[1:]):
        if b['t']-a['t']>900: sess.append([b])
        else: sess[-1].append(b)
    for s in sess:
        if len(s)<3: continue
        g=[b['t']-a['t'] for a,b in zip(s,s[1:])]
        print(fmt(s[0]['t']),'->',fmt(s[-1]['t']),'n=%d'%len(s),'gap med %.0f p10 %.0f p90 %.0f min %.0f'%(S.median(g),sorted(g)[int(len(g)*.1)],sorted(g)[int(len(g)*.9)],min(g)),
              'per hour %.0f'%(len(s)/max((s[-1]['t']-s[0]['t'])/3600,0.01)), collections.Counter(x['outcome'].split(':')[0] for x in s))
# per-day bios per lane (UTC)
c=collections.Counter()
for r in pr:
    if r['outcome'].startswith('profile'): c[(r['at'][:10],r['lane'])]+=1
print(sorted(c.items()))
# hourly max bios
h=collections.Counter()
for r in pr:
    h[(r['lane'],r['at'][:13])]+=1
print(sorted(h.items(), key=lambda x:-x[1])[:8])
