import json, os, datetime as dt, collections, statistics as S, math, html
rows=json.load(open('timeline.json'))
FONT="font-family=\"-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif\""
BG='#ffffff'; INK='#1f2933'; MUTE='#6b7280'; GRID='#e5e7eb'
COL={'fortun8te':'#3b5b8c','dihfluencer':'#b7791f','turtles':'#2f8f83'}
RED='#b3261e'
def esc(s): return html.escape(str(s))
def T(x,y,s,size=11,anchor='start',fill=INK,weight='normal',extra=''):
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" fill="{fill}" font-weight="{weight}" {FONT} {extra}>{esc(s)}</text>'
def svg(w,h,body,title,desc):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-labelledby="t d">'
            f'<title id="t">{esc(title)}</title><desc id="d">{esc(desc)}</desc>'
            f'<rect width="{w}" height="{h}" fill="{BG}"/>'+body+'</svg>')
def ts(s): return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()
def lst(r): return r['kind']=='list' and r['outcome']=='page'

# ---------- chart 1: hourly list requests per account, with adverse markers
def chart1():
    t0=ts('2026-09-24T18:00'); t1=ts('2026-10-04T14:00')
    W=1040; L=140; R=20; Wp=W-L-R
    hrs=int((t1-t0)//3600)+1
    px=Wp/hrs
    lanes=['fortun8te','dihfluencer','turtles']
    rowh=92; top=70; gap=16
    H=top+3*(rowh+gap)+100
    b=[]
    b.append(T(L,24,'List requests per hour, by account',15,weight='600'))
    b.append(T(L,42,'Persisted list pages per clock hour (UTC). Dashed line: 250 an hour, the highest sustained rate seen on every account.',11,fill=MUTE))
    cnt=collections.defaultdict(collections.Counter)
    for r in rows:
        if lst(r) and t0<=r['t']<t1: cnt[r['lane']][int((r['t']-t0)//3600)]+=1
    ymax=300
    for i,lane in enumerate(lanes):
        y0=top+i*(rowh+gap)
        b.append(f'<rect x="{L}" y="{y0}" width="{Wp}" height="{rowh}" fill="#fafafa" stroke="{GRID}"/>')
        for v in (0,100,200,300):
            yy=y0+rowh-v/ymax*rowh
            b.append(f'<line x1="{L}" x2="{L+Wp}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="{GRID}"/>')
            b.append(T(L-6,yy+4,str(v),10,'end',MUTE))
        yy=y0+rowh-250/ymax*rowh
        b.append(f'<line x1="{L}" x2="{L+Wp}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="{MUTE}" stroke-dasharray="4 3"/>')
        b.append(T(L-40,y0+rowh/2+4,lane,11,'end',COL[lane],'600'))
        for h,n in cnt[lane].items():
            hh=n/ymax*rowh
            b.append(f'<rect x="{L+h*px:.1f}" y="{y0+rowh-hh:.1f}" width="{max(px-0.6,1.2):.1f}" height="{hh:.1f}" fill="{COL[lane]}"/>')
    # day ticks (Amsterdam midnight = 22:00 UTC)
    d=dt.datetime(2026,9,24,22,0,tzinfo=dt.timezone.utc)
    ybot=top+3*(rowh+gap)-gap
    while d.timestamp()<t1:
        x=L+(d.timestamp()-t0)/3600*px
        loc=(d+dt.timedelta(hours=2))
        b.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{ybot+4}" stroke="{GRID}"/>')
        b.append(T(x+2,ybot+16,loc.strftime('%d %b'),10,'start',MUTE))
        d+=dt.timedelta(days=1)
    b.append(T(L,ybot+34,'Day boundaries are Amsterdam midnight (22:00 UTC), which is when the extension resets its daily counters.',10,fill=MUTE))
    # markers
    laneY={l:top+i*(rowh+gap) for i,l in enumerate(lanes)}
    def mark(t,lane,shape,color):
        x=L+(t-t0)/3600*px; y=laneY[lane]+(20 if shape=='dot' else 8)
        if shape=='tri': return f'<path d="M{x:.1f},{y-5:.1f} L{x+5:.1f},{y+4:.1f} L{x-5:.1f},{y+4:.1f} Z" fill="{color}"/>'
        if shape=='dia': return f'<path d="M{x:.1f},{y-6:.1f} L{x+6:.1f},{y:.1f} L{x:.1f},{y+6:.1f} L{x-6:.1f},{y:.1f} Z" fill="{color}"/>'
        if shape=='x': return f'<path d="M{x-5:.1f},{y-5:.1f} L{x+5:.1f},{y+5:.1f} M{x+5:.1f},{y-5:.1f} L{x-5:.1f},{y+5:.1f}" stroke="{color}" stroke-width="2.5"/>'
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{color}"/>'
    for r in rows:
        if r['outcome'].startswith('rate_limit'): b.append(mark(r['t'],r['lane'],'tri',RED))
    # inferred 429s from retry_not_before (turtles 09:44:11 and 10:02:08 on 27 Sep)
    for tt in ('2026-09-27T09:44:11','2026-09-27T10:02:08'): b.append(mark(ts(tt),'turtles','tri','#d97b73'))
    for r in rows:
        if r['outcome'].startswith('other:list_html'): b.append(mark(r['t'],r['lane'],'dot','#6d28d9'))
    b.append(mark(ts('2026-09-28T01:24:25'),'dihfluencer','dia',RED))
    b.append(mark(ts('2026-09-30T22:24:32'),'dihfluencer','x',RED))
    # legend
    ly=H-44; lx=24
    items=[('tri',RED,'HTTP 429 on a bio read'),('tri','#d97b73','429 inferred from retry times'),('dot','#6d28d9','followers list returned home page'),('dia',RED,'soft block (mobile benchmark)'),('x',RED,'scraping warning page first seen')]
    for shape,color,label in items:
        x=lx; y=ly
        if shape=='tri': b.append(f'<path d="M{x+5},{y-5} L{x+10},{y+4} L{x},{y+4} Z" fill="{color}"/>')
        elif shape=='dia': b.append(f'<path d="M{x+5},{y-6} L{x+11},{y} L{x+5},{y+6} L{x-1},{y} Z" fill="{color}"/>')
        elif shape=='x': b.append(f'<path d="M{x},{y-5} L{x+10},{y+5} M{x+10},{y-5} L{x},{y+5}" stroke="{color}" stroke-width="2.5"/>')
        else: b.append(f'<circle cx="{x+5}" cy="{y}" r="3.2" fill="{color}"/>')
        b.append(T(x+16,y+4,label,10,fill=MUTE))
        lx+=16+len(label)*5.6+22
        if label.startswith('followers'): lx=24; ly+=20
    return svg(W,H,''.join(b),'List requests per hour by account','Hourly persisted list pages for three accounts from 24 September to 4 October 2026 with adverse events marked.')

# ---------- chart 2: gap regimes
def gaps(a,bnd,lanes=None):
    out=[]
    ls=collections.defaultdict(list)
    for r in rows:
        if lst(r) and a<=r['t']<bnd and (lanes is None or r['lane'] in lanes): ls[r['lane']].append(r)
    for lane,rs in ls.items():
        rs.sort(key=lambda x:x['t'])
        for x,y in zip(rs,rs[1:]):
            g=y['t']-x['t']
            if g<=60: out.append(g)
    return out
def chart2():
    A=gaps(ts('2026-09-24'),ts('2026-10-02'), lanes=['fortun8te','dihfluencer','turtles'])
    Bg=gaps(ts('2026-10-03T12:00'),ts('2026-10-05'))
    W=1040; H=360; L=70; R=30; top=64; bot=60
    xmax=40; px=(W-L-R)/xmax
    bins=lambda g:[sum(1 for v in g if i<=v<i+1)/len(g) for i in range(xmax)]
    ba,bb=bins(A),bins(Bg)
    ymax=max(max(ba),max(bb))*1.1
    ph=H-top-bot
    b=[T(L,24,'Gap between list pages: before and after 1 October',15,weight='600'),
       T(L,42,f'Share of gaps by length, one-second bins. Left: 24 Sep to 1 Oct (n={len(A):,}). Right-hand curve: 3 to 4 Oct (n={len(Bg):,}). Gaps over 60 s (scheduled breaks) excluded.',11,fill=MUTE)]
    # configured band 7..12
    b.append(f'<rect x="{L+7*px:.1f}" y="{top}" width="{5*px:.1f}" height="{ph}" fill="#eef2f7"/>')
    b.append(T(L+9.5*px,top+14,'configured gap',10,'middle',MUTE)); b.append(T(L+9.5*px,top+26,'7 to 12 s',10,'middle',MUTE))
    for v in range(0,int(ymax*100)+1,2):
        y=top+ph-(v/100)/ymax*ph
        b.append(f'<line x1="{L}" x2="{W-R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>'); b.append(T(L-6,y+4,f'{v}%',10,'end',MUTE))
    for i in range(0,xmax+1,5):
        b.append(T(L+i*px,H-bot+16,f'{i} s',10,'middle',MUTE))
    b.append(f'<line x1="{L}" x2="{W-R}" y1="{top+ph}" y2="{top+ph}" stroke="{MUTE}"/>')
    for i in range(xmax):
        for series,color,off in ((ba,'#3b5b8c',0),(bb,'#b3261e',0.5)):
            h=series[i]/ymax*ph
            b.append(f'<rect x="{L+i*px+off*px*0.5:.1f}" y="{top+ph-h:.1f}" width="{px*0.46:.1f}" height="{h:.1f}" fill="{color}" opacity="0.9"/>')
    ma,mb=S.median(A),S.median(Bg)
    b.append(T(L+ma*px+4,top+ph-ba[int(ma)]/ymax*ph-26,f'median {ma:.1f} s',11,fill='#3b5b8c',weight='600'))
    b.append(T(L+27*px,top+40,f'median {mb:.1f} s',11,fill=RED,weight='600'))
    b.append(T(L+27*px,top+54,'the 15 s idle poll sits on top of',10,fill=RED))
    b.append(T(L+27*px,top+67,'the configured gap',10,fill=RED))
    ly=H-24
    b.append(f'<rect x="{L}" y="{ly-9}" width="12" height="12" fill="#3b5b8c"/>'); b.append(T(L+18,ly+1,'24 Sep to 1 Oct, three accounts',11,fill=MUTE))
    b.append(f'<rect x="{L+250}" y="{ly-9}" width="12" height="12" fill="{RED}"/>'); b.append(T(L+268,ly+1,'3 to 4 Oct, @trashtheturtles, lists only',11,fill=MUTE))
    return svg(W,H,''.join(b),'Gap between list pages','Histogram comparing page gaps before and after 1 October 2026.')

# ---------- chart 3: 3 Oct day
def chart3():
    t0=ts('2026-10-03T12:00'); t1=t0+24*3600
    W=1040; L=60; R=24; Wp=W-L-R; top=76; ph=120
    bins=144; bw=Wp/bins
    c=collections.Counter()
    for r in rows:
        if lst(r) and r['lane']=='turtles' and t0<=r['t']<t1: c[int((r['t']-t0)//600)]+=1
    ymax=60
    H=top+ph+190
    b=[T(L,24,'3 October to 4 October: where the 24 hours went (@trashtheturtles)',15,weight='600'),
       T(L,42,'List pages per 10 minutes. Window starts 12:00 UTC (14:00 Amsterdam) on 3 October.',11,fill=MUTE)]
    for v in (0,20,40,60):
        y=top+ph-v/ymax*ph
        b.append(f'<line x1="{L}" x2="{L+Wp}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>'); b.append(T(L-6,y+4,str(v),10,'end',MUTE))
    for k,n in c.items():
        h=n/ymax*ph
        b.append(f'<rect x="{L+k*bw:.1f}" y="{top+ph-h:.1f}" width="{bw-0.8:.1f}" height="{h:.1f}" fill="{COL["turtles"]}"/>')
    for hh in range(0,25,2):
        x=L+hh*6*bw
        b.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top+ph}" y2="{top+ph+4}" stroke="{MUTE}"/>')
        tt=dt.datetime.fromtimestamp(t0+hh*3600,dt.timezone.utc)
        b.append(T(x,top+ph+16,tt.strftime('%H'),10,'middle',MUTE))
    b.append(T(L+Wp,top+ph+30,'UTC hour',10,'end',MUTE))
    # state band
    by=top+ph+44; bh=26
    def seg(a,bb,color,label=None,tc='#fff'):
        x=L+(ts(a)-t0)/(24*3600)*Wp; w=(ts(bb)-ts(a))/(24*3600)*Wp
        s=f'<rect x="{x:.1f}" y="{by}" width="{w:.1f}" height="{bh}" fill="{color}"/>'
        if label and w>46: s+=T(x+w/2,by+17,label,10,'middle',tc)
        return s
    segs=[('2026-10-03T12:00','2026-10-03T12:14','#d1d5db','paused'),
          ('2026-10-03T12:14','2026-10-03T14:52',COL['turtles'],'run 1'),
          ('2026-10-03T14:52','2026-10-03T17:14','#9ca3af','budget reached'),
          ('2026-10-03T17:14','2026-10-03T20:18',COL['turtles'],'run 2'),
          ('2026-10-03T20:18','2026-10-04T01:05','#9ca3af','redirect pause'),
          ('2026-10-04T01:05','2026-10-04T01:32',COL['turtles'],''),
          ('2026-10-04T01:32','2026-10-04T12:00',RED,'stopped: request not confirmed')]
    for a,bb,color,label in segs: b.append(seg(a,bb,color,label))
    b.append(T(L,by-8,'State of the account',10,fill=MUTE))
    # annotation list
    notes=[('Run 1, 12:14 to 14:52 (158 min)','335 following pages of about 195 people, median gap 21.9 s. Stopped by the daily list budget (400, raised from 200 at 13:42): 2 + 335 pages + about 64 seed profile loads = 401 counted requests.'),
           ('Gap, 14:52 to 17:14 (142 min)','Self-imposed. The budget was spent. This account\'s list budget reads 0 (unlimited) in the database now, so it was lifted before run 2.'),
           ('Run 2, 17:14 to 20:18 (183 min)','493 pages, almost all followers lists at about 24 people each (one fifth of the new people per request), median gap 19.1 s. Ends on a followers redirect to the home page at page 101 of one list.'),
           ('Gap, 20:18 to 01:05 (287 min)','Self-imposed. A followers home redirect revokes the account selection and pauses both stages (server.py:924). The same list was retried at 22:22 after the 2 h route wait and redirected again. Collection was restarted by hand at 01:04:54.'),
           ('Run 3, 01:05 to 01:32 (27 min)','72 following pages, median gap 18.6 s with the 11.65 s poll patch.'),
           ('Stop, 01:32 to the snapshot at 16:21 UTC','No requests for 14 h 49 min. The last request had no confirmed end, so the server paused everything at 01:42 (accounts.py:1040). Cause unknown. Still paused at snapshot time.')]
    y=by+bh+22
    for h,txt in notes:
        b.append(T(L,y,h,11,weight='600')); y+=14
        # wrap
        words=txt.split(); line=''
        for w in words:
            if len(line)+len(w)>170: b.append(T(L,y,line,10.5,fill=MUTE)); y+=13; line=w
            else: line=(line+' '+w).strip()
        if line: b.append(T(L,y,line,10.5,fill=MUTE)); y+=17
    H=int(y+10)
    return svg(W,H,''.join(b),'Where the 24 hours of 3 to 4 October went','Pages per ten minutes for @trashtheturtles with runs, stops and the reasons for each.')

# ---------- chart 4: cadence anatomy
def chart4():
    W=1040; H=300; L=210; R=30; top=70
    xmax=24; px=(W-L-R)/xmax
    bars=[('3 Oct run 1 (15 s poll)',[('request and processing',3.4,'#6b7280'),('spacing wait',3.5,'#c4cdd8'),('idle poll',15.0,RED)]),
          ('4 Oct run (11.65 s poll, live patch)',[('request and processing',3.4,'#6b7280'),('spacing wait',3.5,'#c4cdd8'),('idle poll',11.65,'#d97b73')]),
          ('Proposed: wait for the list clock',[('request and processing',3.4,'#6b7280'),('list gap (7 to 12 s)',9.5,'#3b5b8c')])]
    obs=[21.9,18.6,None]
    b=[T(L-170,24,'Where one page gap goes (200-person following pages)',15,weight='600'),
       T(L-170,42,'Seconds between two list requests. Observed medians in bold; the third bar is a model built from the same constants.',11,fill=MUTE)]
    y=top
    for (name,segs),o in zip(bars,obs):
        b.append(T(L-10,y+22,name,11,'end'))
        x=L
        for label,val,color in segs:
            w=val*px
            b.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="34" fill="{color}"/>')
            if w>40: b.append(T(x+w/2,y+21,f'{val:g}',11,'middle','#fff' if color not in('#c4cdd8',) else INK))
            x+=w
        tot=sum(v for _,v,_ in segs)
        b.append(T(x+8,y+21,(f'{o:.1f} s observed' if o else f'{tot:.1f} s modeled'),11,fill=INK,weight='600'))
        y+=58
    # axis
    for i in range(0,xmax+1,4):
        b.append(f'<line x1="{L+i*px:.1f}" x2="{L+i*px:.1f}" y1="{top-8}" y2="{y-14}" stroke="{GRID}"/>')
        b.append(T(L+i*px,y,f'{i} s',10,'middle',MUTE))
    # legend
    ly=H-34; lx=L-170
    for label,color in (('request and processing','#6b7280'),('spacing wait (2 to 5 s)','#c4cdd8'),('idle poll','#c0392b'),('list gap (7 to 12 s)','#3b5b8c')):
        b.append(f'<rect x="{lx}" y="{ly-9}" width="12" height="12" fill="{color}"/>'); b.append(T(lx+18,ly+1,label,10.5,fill=MUTE)); lx+=len(label)*6+46
    return svg(W,H,''.join(b),'Where one page gap goes','Stacked bars for observed and proposed gap composition between list pages.')

# ---------- chart 5: envelope
def chart5():
    W=1040; H=430; L=250; R=40; top=76
    items=[('List requests per hour',284,250,250,'fortun8te, 26 Sep',400),
           ('List requests per day, collector account',900,900,1200,'turtles, 3 to 4 Oct',1800),
           ('List requests per day, main account (old)',1760,None,None,'fortun8te, 26 Sep',1800),
           ('Bio page reads per hour',69,60,60,'turtles, 1 Oct',120),
           ('Bio page reads per day',100,100,150,'turtles, 1 Oct (budget cap)',250)]
    b=[T(24,24,'What was run cleanly, and what is proposed',15,weight='600'),
       T(24,42,'Dark bar: highest clean value seen. Tick: proposed default. Open bar: ramp ceiling that needs the step-up experiment.',11,fill=MUTE)]
    y=top
    for name,seen,default,ramp,who,axmax in items:
        px=(W-L-R)/axmax
        b.append(T(L-10,y+14,name,11,'end',weight='600')); b.append(T(L-10,y+28,who,10,'end',fill=MUTE))
        b.append(f'<rect x="{L}" y="{y}" width="{(W-L-R)}" height="22" fill="#f3f4f6"/>')
        if ramp: b.append(f'<rect x="{L}" y="{y}" width="{ramp*px:.1f}" height="22" fill="none" stroke="{MUTE}" stroke-dasharray="4 3"/>')
        b.append(f'<rect x="{L}" y="{y+4}" width="{seen*px:.1f}" height="14" fill="#3b5b8c"/>')
        b.append(T(L+seen*px+6,y+16,f'{seen:,}',11,fill=INK,weight='600'))
        if default: 
            b.append(f'<line x1="{L+default*px:.1f}" x2="{L+default*px:.1f}" y1="{y-4}" y2="{y+26}" stroke="{RED}" stroke-width="2.5"/>')
            b.append(T(L+default*px,y-8,f'default {default:,}',10,'middle',RED))
        if ramp and ramp!=default: b.append(T(L+ramp*px,y+40,f'ramp ceiling {ramp:,}',10,'middle',MUTE))
        y+=66
    b.append(T(24,H-14,'Sample sizes: one day above 1,700, two days near 700 to 900, one 2-hour bio session at 69 an hour. The data does not show where Instagram starts to push back.',10.5,fill=MUTE))
    return svg(W,H,''.join(b),'Clean envelope against proposed defaults','Bars comparing the highest clean request counts with proposed defaults.')

open(os.environ.get('PACING_OUT','..')+'/pacing-1-hourly.svg','w').write(chart1())
open(os.environ.get('PACING_OUT','..')+'/pacing-2-gaps.svg','w').write(chart2())
open(os.environ.get('PACING_OUT','..')+'/pacing-3-oct3.svg','w').write(chart3())
open(os.environ.get('PACING_OUT','..')+'/pacing-4-cadence.svg','w').write(chart4())
open(os.environ.get('PACING_OUT','..')+'/pacing-5-envelope.svg','w').write(chart5())
print('ok')
