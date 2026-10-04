import json, datetime as dt
rows=json.load(open('timeline.json'))
def fmt(t): return dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime('%H:%M:%S')
t=[r for r in rows if r['lane']=='turtles' and r['at']>='2026-10-03T12:00' and r['at']<'2026-10-04T03:00']
prev=None
for r in t:
    g=r['t']-prev if prev else 0
    prev=r['t']
    if g>45 or r['outcome']!='page':
        print(fmt(r['t']), 'gap %.0f'%g, r['kind'], r['outcome'], r.get('users'), r.get('seed'), r.get('direction') or r.get('dir'), r.get('job'))
