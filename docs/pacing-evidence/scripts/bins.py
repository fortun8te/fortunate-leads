import json, datetime as dt, sys
rows=json.load(open('timeline.json'))
start=dt.datetime.fromisoformat(sys.argv[1]).replace(tzinfo=dt.timezone.utc).timestamp()
end=dt.datetime.fromisoformat(sys.argv[2]).replace(tzinfo=dt.timezone.utc).timestamp()
w=int(sys.argv[3])*60
lanes=['fortun8te','dihfluencer','turtles']
import collections
b=collections.defaultdict(lambda: collections.Counter())
for r in rows:
    if start<=r['t']<end:
        k=int((r['t']-start)//w)
        tag = 'L' if r['kind']=='list' and r['outcome']=='page' else ('P' if r['kind']=='profile' and r['outcome'].startswith('profile') else 'x')
        b[k][(r['lane'],tag)]+=1
print('time   '+'  '.join('%-14s'%l for l in lanes))
for k in range(int((end-start)//w)):
    t=dt.datetime.fromtimestamp(start+k*w,dt.timezone.utc).strftime('%H:%M')
    cells=[]
    for l in lanes:
        cells.append('%3dL %2dP %2dx'%(b[k][(l,'L')],b[k][(l,'P')],b[k][(l,'x')]) if sum(b[k][(l,t_)] for t_ in 'LPx') else '      -       ')
    print(t,'  '.join('%-14s'%c for c in cells))
