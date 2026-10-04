import json, datetime as dt, statistics as S, collections
rows=json.load(open('timeline.json'))
OFF=2*3600
def day(t,off): return dt.datetime.fromtimestamp(t+off,dt.timezone.utc).strftime('%Y-%m-%d')
def table(off,label):
    d=collections.defaultdict(lambda: dict(pages=0,entries=0,bios=0,other=0,fail=0,t=[],hours=collections.Counter(),gaps=[]))
    lanes=collections.defaultdict(list)
    for r in sorted(rows,key=lambda x:x['t']): lanes[r['lane']].append(r)
    out=[]
    for lane,rs in lanes.items():
        for r in rs:
            k=(lane,day(r['t'],off)); e=d[k]
            if r['kind']=='list' and r['outcome']=='page':
                e['pages']+=1; e['entries']+=r['users'] or 0; e['hours'][int((r['t']+off)//3600)]+=1
            elif r['kind']=='profile' and r['outcome'].split(':')[0] in('profile','not_found'): e['bios']+=1
            else: e['other']+=1
            e['t'].append(r['t'])
        # gaps
    for (lane,dd),e in sorted(d.items(), key=lambda x:(x[0][1],x[0][0])):
        ts=sorted(e['t']); 
        # active time: sum of gaps <=600
        act=sum(b-a for a,b in zip(ts,ts[1:]) if b-a<=600)
        pg=[b-a for a,b in zip(ts,ts[1:]) if b-a<=60]
        mx=max(e['hours'].values()) if e['hours'] else 0
        out.append((dd,lane,e['pages'],e['entries'],e['bios'],e['other'],round(act/3600,2),mx,round(S.median(pg),1) if pg else None))
    print('===',label)
    print('day lane pages entries bios other active_h maxhour med_gap')
    for o in out: print(*o)
table(0,'UTC'); table(OFF,'Amsterdam (UTC+2)')
