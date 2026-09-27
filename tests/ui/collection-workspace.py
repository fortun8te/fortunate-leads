"""Shared collection targets stay usable across Accounts and Collection, mock only."""
exec(open('tests/ui/helpers.py').read())
new_tab(base+'/?mock=1#/accounts'); wait_for_load(); settle(); time.sleep(1)
for _ in range(20):
    if js('!!document.querySelector("a.fl-ctl-status-link")'): break
    time.sleep(.3)
check('Accounts shows existing targets and one form', 'document.querySelectorAll("#seed-in").length===1 && !!document.querySelector("#acc-targets #seed-in") && !!document.querySelector("#acc-targets #lists-body tr")')
check('No controls dropdown', '!document.querySelector(".fl-ctl-details") && Array.from(document.querySelectorAll("a.fl-ctl-status-link")).some(a=>a.hash==="#/accounts")')
check('Startup names local models', 'document.querySelector("#acc-start").textContent === "Start local models"')
click_node('button','Add profiles')
check('Add action focuses real queue form', 'document.activeElement.id === "seed-in"')
cdp('Input.insertText', text='pending_handle'); settle()
click_node('link','Detailed activity')
check('Existing input is preserved on Collection', 'document.querySelector("#view-scraper #seed-in").value === "pending_handle"')
click_node('link','Accounts')
check('Existing input moves back without duplication', 'document.querySelectorAll("#seed-in").length === 1 && document.querySelector("#acc-targets #seed-in").value === "pending_handle"')
for width in [1440,390]:
    cdp('Emulation.setDeviceMetricsOverride',width=width,height=1000,deviceScaleFactor=1,mobile=width<500);settle()
    check(f'{width}: no page overflow', 'document.documentElement.scrollWidth <= innerWidth')
    js('document.querySelector("#view-accounts").scrollTop=0'); settle()
    shot(f'collection-workspace-{width}')
print(json.dumps({'passed':len(checks),'output':str(out)}))
