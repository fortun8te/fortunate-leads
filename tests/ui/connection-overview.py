"""Browser regression for comparison-only controls. Run with browser-harness on a mock preview."""
import json
import os
import time

new_tab(os.environ.get('UI_BASE_URL', 'http://127.0.0.1:8891') + '/?mock=1#/map')
cdp('Page.bringToFront')
wait_for_load()
for _ in range(25):
    if js("typeof M !== 'undefined' && document.querySelector('#connection-target') !== null"):
        break
    time.sleep(.2)
assert js('M.loaded === false'), 'Initial comparison must not fetch map data'


def click_compare():
    # Exercise the installed toggle listener; assert actual computed visibility below.
    js("document.querySelector('#connections-toggle').click()")
    time.sleep(.1)


visible = "el => !!el && getComputedStyle(el).display !== 'none' && el.getClientRects().length > 0"
checks = []
for width in [1280, 390]:
    zoom = cdp('Page.getLayoutMetrics')['cssVisualViewport'].get('zoom', 1)
    cdp('Emulation.setDeviceMetricsOverride', width=round(width*zoom), height=round(844*zoom), deviceScaleFactor=1, mobile=False)
    time.sleep(.2)
    assert js("!document.querySelector('#connections-panel').hidden"), 'Comparison starts open'
    original = js('JSON.stringify(S.f)')
    assert js(f"['.qbar','#filters','#scrim','#save-view'].every(s => !({visible})(document.querySelector(s)))"), 'Unrelated overview controls must be hidden'
    assert js("document.documentElement.scrollWidth <= innerWidth"), 'Comparison must fit the phone viewport'
    assert js(f"({visible})(document.querySelector('#connection-target'))"), 'Target remains visible'
    assert js("!document.querySelector('#map-q') && !document.querySelector('#map-labels')"), 'Duplicate controls are removed'
    assert js('M.shown === false'), 'Hidden map remains stopped'
    click_compare()
    assert js("document.querySelector('#connections-panel').hidden"), 'Explore map opens the overview'
    assert js(f"({visible})(document.querySelector('.qbar'))"), 'Overview toolbar returns'
    assert js('M.shown === true'), 'Map starts only when explored'
    assert js('JSON.stringify(S.f)') == original, 'Existing filters remain unchanged'
    click_compare()
    assert js("!document.querySelector('#connections-panel').hidden && !M.shown"), 'Returning to comparison stops the map'
    checks.append({'width': js('innerWidth'), 'comparison_controls': 'pass', 'filter_preservation': 'pass'})
cdp('Emulation.clearDeviceMetricsOverride')
print(json.dumps(checks))
