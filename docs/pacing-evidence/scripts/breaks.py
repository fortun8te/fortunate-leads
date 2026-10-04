import json, datetime as dt, statistics as S
rows=json.load(open('timeline.json'))
def fmt(t): return dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime('%m-%d %H:%M')
by={}
for r in rows:
    if r['kind']=='list' and r['outcome']=='page': by.setdefault(r['lane'],[]).append(r)
for lane,rs in by.items():
    rs.sort(key=lambda x:x['t'])
    # sessions: gaps>600 split
    sess=[[rs[0]]]
    for a,b in zip(rs,rs[1:]):
        if b['t']-a['t']>600: sess.append([b])
        else: sess[-1].append(b)
    print('==',lane)
    for s in sess:
        if len(s)<60: continue
        gaps=[b['t']-a['t'] for a,b in zip(s,s[1:])]
        brk=[(i,g) for i,g in enumerate(gaps) if 60<g<=600]
        base=[g for g in gaps if g<=60]
        bl=[g for i,g in brk]
        between=[b[0]-a[0] for a,b in zip(brk,brk[1:])]
        print(fmt(s[0]['t']),'n=%d'%len(s),'normal gap med %.1f mean %.1f p5 %.1f p95 %.1f'%(S.median(base),S.mean(base),sorted(base)[int(len(base)*.05)],sorted(base)[int(len(base)*.95)]),
              '| breaks %d len med %.0f min %.0f max %.0f'%(len(brk), S.median(bl) if bl else 0, min(bl) if bl else 0, max(bl) if bl else 0),
              '| pages between breaks med %s range %s'%(S.median(between) if between else None, (min(between),max(between)) if between else None))
