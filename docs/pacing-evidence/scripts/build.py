import sqlite3, json, datetime as dt
import os
SNAP=os.environ.get("PACING_SNAPSHOT","/Users/michael/Projects/Fortunate-Leads/work/oct4-reports/data/snapshot/leads.sqlite")
c=sqlite3.connect("file:%s?mode=ro"%SNAP,uri=True); c.row_factory=sqlite3.Row
NAMES={'ln_beq5hsrckv2w':'fortun8te','ln_k37u7uev7wbw':'dihfluencer','ln_sqrscja8tx6z':'turtles'}
def ts(s): return dt.datetime.fromisoformat(s).timestamp()
rows=[]
# list pages from pages table (full history from 24 Sep)
for r in c.execute("select p.job_id, p.cursor, p.at, p.lane, p.users, j.direction, j.page_size, j.seed from pages p left join jobs j on j.id=p.job_id where p.at is not null"):
    rows.append(dict(t=ts(r['at']), at=r['at'], lane=NAMES.get(r['lane'],r['lane']), kind='list', outcome='page', users=r['users'], job=r['job_id'], direction=r['direction'], psize=r['page_size'], seed=r['seed']))
for r in c.execute("select * from collector_events where outcome!='page'"):
    rows.append(dict(t=ts(r['at']), at=r['at'], lane=NAMES.get(r['lane'],r['lane']), kind=r['kind'], outcome=r['outcome']+':'+(r['reason'] or ''), users=r['returned_count'], job=r['job_id'], direction=r['direction'], psize=r['requested_count'], seed=None, http=r['http_status'], route=r['route']))
# page events for requested_count/direction on those pages (Sep27+)
ev={}
for r in c.execute("select * from collector_events where outcome='page'"):
    ev[(r['job_id'],r['at'])]=r
for x in rows:
    if x['kind']=='list' and x['outcome']=='page':
        e=ev.get((x['job'],x['at']))
        if e:
            x['req']=e['requested_count']; x['saved']=e['saved_entries']; x['newl']=e['new_links']; x['dir']=e['direction']
rows.sort(key=lambda x:x['t'])
json.dump(rows, open('timeline.json','w'))
print(len(rows))
