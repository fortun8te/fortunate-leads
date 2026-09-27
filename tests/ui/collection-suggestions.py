"""Suggested targets are read-only until Add is clicked, mock only."""
exec(open('tests/ui/helpers.py').read())
new_tab(base+'/?mock=1#/accounts'); wait_for_load(); settle()
for _ in range(20):
    if js('!!document.querySelector("[data-suggested-handle]")'): break
    time.sleep(.3)
check('Saved-data suggestions visible', 'document.querySelectorAll("[data-suggested-handle]").length > 0')
js('window.suggestionPosts=[];const oldFetch=window.fetch;window.fetch=(u,o)=>{if(String(u)==="/api/scraper/seeds" && o?.method==="POST")suggestionPosts.push(JSON.parse(o.body));return oldFetch(u,o)}')
check('No automatic queue writes', 'suggestionPosts.length === 0')
label=js('document.querySelector("[data-suggested-handle]").getAttribute("aria-label")')
click_node('button',label)
check('Explicit Add queues one target', 'suggestionPosts.length===1 && suggestionPosts[0].handles.length===1 && suggestionPosts[0].directions.length>0')
for width in [1440,390]:
    cdp('Emulation.setDeviceMetricsOverride',width=width,height=1100,deviceScaleFactor=1,mobile=width<500);settle()
    js('document.querySelector("#view-accounts").scrollTop=0;document.querySelector("#view-accounts .scr").scrollTop=0');settle()
    check(f'{width}: no overflow', 'document.documentElement.scrollWidth<=innerWidth')
    shot(f'collection-final-{width}')
print(json.dumps({'passed':len(checks),'output':str(out)}))
