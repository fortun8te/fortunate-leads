"""Map-first desktop/phone browser check. Run through browser-harness on a mock preview."""
import json
import os
import time

new_tab(os.environ.get('UI_BASE_URL', 'http://127.0.0.1:8904') + '/?mock=1#/map')
wait_for_load()
checks = []
for width in [1440, 390]:
    cdp('Emulation.setDeviceMetricsOverride', width=width, height=900, deviceScaleFactor=1, mobile=False)
    cdp('Page.reload', ignoreCache=True)
    wait_for_load()
    for _ in range(30):
        try:
            if js('M.loaded && M.nodes.length > 0'):
                break
        except RuntimeError:
            pass
        time.sleep(.1)
    assert js("document.querySelector('#connections-panel').hidden && M.shown"), 'Map must be the direct entry'
    assert js('M.sim.alpha() === 0'), 'Default avatar map must not drift'
    assert js("document.documentElement.scrollWidth <= innerWidth"), 'No horizontal page overflow'
    assert js("getComputedStyle(document.querySelector('.qbar')).display === 'none'"), 'Only one search toolbar'
    assert js("!!document.querySelector('#map-q').getClientRects().length"), 'Map search is visible'
    js("document.querySelector('#connections-toggle').click()")
    assert js("!document.querySelector('#connections-panel').hidden && !M.shown"), 'Comparison stops the map'
    js("document.querySelector('#connections-toggle').click()")
    assert js("document.querySelector('#connections-panel').hidden && M.shown"), 'Back returns to map'
    if width > 1000:
        point = js("(()=>{const n=M.leads.find(n=>n.x*M.k+M.x>100&&n.x*M.k+M.x<M.w-100&&n.y*M.k+M.y>100&&n.y*M.k+M.y<M.h-100);const r=document.querySelector('#canvas').getBoundingClientRect();return {x:r.x+n.x*M.k+M.x,y:r.y+n.y*M.k+M.y,k:M.k};})()")
        click_at_xy(point['x'], point['y'])
        time.sleep(.2)
        assert js("M.focus && !document.querySelector('#detail').hidden && getComputedStyle(document.querySelector('#detail')).display !== 'none'"), 'Selection opens the useful side panel'
        assert abs(js('M.k') - point['k']) < .00001, 'Selection preserves zoom'
    checks.append({'width': width, 'map_first': True, 'stable': True, 'single_toolbar': True})
cdp('Emulation.clearDeviceMetricsOverride')
print(json.dumps(checks))
