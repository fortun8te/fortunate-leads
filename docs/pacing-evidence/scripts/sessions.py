import json, statistics as st, datetime as dt
rows=json.load(open('timeline.json'))
def pct(a,p):
    a=sorted(a); 
    if not a: return None
    k=(len(a)-1)*p/100; f=int(k); c=min(f+1,len(a)-1); return a[f]+(a[c]-a[f])*(k-f)
def fmt(t): return dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime('%m-%d %H:%M:%S')
by={}
for r in rows: by.setdefault(r['lane'],[]).append(r)
for lane,rs in by.items():
    rs.sort(key=lambda x:x['t'])
    sess=[]; cur=[rs[0]]
    for r in rs[1:]:
        if r['t']-cur[-1]['t']>600: sess.append(cur); cur=[r]
        else: cur.append(r)
    sess.append(cur)
    print('=====',lane,len(rs),'events', len(sess),'sessions')
    for s in sess:
        if len(s)<5: continue
        dur=s[-1]['t']-s[0]['t']
        lp=[x for x in s if x['kind']=='list' and x['outcome']=='page']
        pr=[x for x in s if x['kind']=='profile']
        gaps=[b['t']-a['t'] for a,b in zip(s,s[1:])]
        lg=[b['t']-a['t'] for a,b in zip(lp,lp[1:])]
        u=[x['users'] or 0 for x in lp]
        print(fmt(s[0]['t']),'->',fmt(s[-1]['t']), 'dur %.0fmin'%(dur/60), 'n=%d list=%d prof=%d'%(len(s),len(lp),len(pr)),
              'gap med %.1f p10 %.1f p90 %.1f'%(pct(gaps,50),pct(gaps,10),pct(gaps,90)),
              'users/pg med %.0f'%pct(u,50) if u else '', 'pages/h %.0f'%(len(lp)/(dur/3600)) if dur>0 else '')
