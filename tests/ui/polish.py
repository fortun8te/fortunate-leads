"""Run through browser-harness against an isolated browser and ?mock=1.

See README.md. These checks never access a real Instagram account or database.
"""
import base64
import json
import os
import time
from pathlib import Path

base = os.environ.get('UI_BASE_URL', 'http://127.0.0.1:8879')
out = Path(os.environ.get('UI_OUTPUT', '../ui-checks'))
out.mkdir(parents=True, exist_ok=True)
checks = []


def settle():
    # Let mock API responses and debounced input complete.
    time.sleep(.45)


def check(name, expression):
    result = js(expression)
    checks.append({'check': name, 'passed': bool(result), 'actual': result})
    if not result:
        raise AssertionError(name)


def click_node(role, name):
    nodes = cdp('Accessibility.getFullAXTree')['nodes']
    node = next(n for n in nodes if not n.get('ignored') and n.get('role', {}).get('value') == role and n.get('name', {}).get('value') == name)
    box = cdp('DOM.getBoxModel', backendNodeId=node['backendDOMNodeId'])['model']['content']
    click_at_xy(sum(box[0::2]) / 4, sum(box[1::2]) / 4)
    settle()


def key(key, code=None, text=None):
    args = {'key': key}
    if code:
        args['code'] = code
    if text:
        args['text'] = text
    cdp('Input.dispatchKeyEvent', type='keyDown', **args)
    cdp('Input.dispatchKeyEvent', type='keyUp', key=key, **({'code': code} if code else {}))
    settle()


def shot(name):
    (out / f'{name}.png').write_bytes(base64.b64decode(cdp('Page.captureScreenshot', format='png')['data']))


new_tab('about:blank')
cdp('Page.addScriptToEvaluateOnNewDocument', source='window.uiErrors=[]; window.addEventListener("error", e=>uiErrors.push(e.message || "Resource failed")); window.addEventListener("unhandledrejection", e=>uiErrors.push(String(e.reason)));')
cdp('Emulation.setDeviceMetricsOverride', width=1440, height=1000, deviceScaleFactor=1, mobile=False)
goto_url(base + '/?mock=1')
wait_for_load()
settle()
try:
    check('Sample leads and all stage controls load', 'document.querySelectorAll(".row").length > 0 && document.querySelectorAll(".fl-ctl-pill").length === 3')
    click_node('button', 'Dark') if js('document.documentElement.dataset.theme === "light"') else None
    routes = [('Leads', 'work'), ('Map', 'work'), ('Qualification', 'qual'), ('Tags', 'tags'), ('Scraper', 'scraper'), ('Accounts', 'accounts'), ('Settings', 'settings')]
    for width, height in [(1440, 1000), (390, 844)]:
        cdp('Emulation.setDeviceMetricsOverride', width=width, height=height, deviceScaleFactor=1, mobile=width < 500)
        cdp('Emulation.setTouchEmulationEnabled', enabled=width < 500)
        for theme in ['dark', 'light']:
            if js('document.documentElement.dataset.theme') != theme:
                click_node('button', 'Light' if theme == 'light' else 'Dark')
            for label, view in routes:
                click_node('link', label)
                check(f'{label} {theme} {width}: visible', f'document.querySelector("#view-{view}").classList.contains("on")')
                check(f'{label} {theme} {width}: no horizontal page overflow', 'document.documentElement.scrollWidth <= innerWidth')
                check(f'{label} {theme} {width}: named active navigation', f'document.querySelector(".tabs [aria-current=page]").getAttribute("aria-label") === {json.dumps(label)}')
                check(f'{label} {theme} {width}: no uncaught errors', 'window.uiErrors.length === 0')
                shot(f'{label.lower()}-{theme}-{width}')

    cdp('Emulation.setDeviceMetricsOverride', width=1440, height=1000, deviceScaleFactor=1, mobile=False)
    cdp('Emulation.setTouchEmulationEnabled', enabled=False)
    click_node('button', 'Dark')
    click_node('link', 'Leads')
    # Keyboard entry, empty state and clear control.
    key('/', 'Slash', '/')
    cdp('Input.insertText', text='no-such-lead-polish-check')
    settle()
    check('Search shows an actionable empty state', 'document.querySelector(".empty b")?.textContent === "No matches" && !document.querySelector("#clear-filters").hidden')
    shot('empty-dark-1440')
    click_node('button', 'Clear')
    check('Clear restores results', 'document.querySelectorAll(".row").length > 0 && document.querySelector("#clear-filters").hidden')

    # Start from the first native lead button, using real key events for activation.
    js('document.querySelector(".lead-open").focus()')
    first = js('document.activeElement.closest(".row").dataset.personId')
    key('Enter', 'Enter', '\r')
    check('Enter opens detail and focuses close after loading', '!document.querySelector("#detail").hidden && document.activeElement.id === "d-close"')
    shot('detail-dark-1440')
    js('document.querySelector("#detail").scrollTop = 500')
    check('Detail identity stays visible while scrolling', 'Math.abs(document.querySelector(".d-head").getBoundingClientRect().top - document.querySelector("#detail").getBoundingClientRect().top) < 2')
    key('Escape', 'Escape')
    check('Escape returns to the same lead', f'document.querySelector("#detail").hidden && document.activeElement.classList.contains("lead-open") && document.activeElement.closest(".row").dataset.personId === {json.dumps(first)}')

    js('document.querySelector(".row [data-ck]").focus()')
    key(' ', 'Space', ' ')
    check('Space selects once and keeps checkbox focus', 'document.activeElement.matches("[data-ck]") && document.activeElement.getAttribute("aria-checked") === "true" && document.querySelector("#detail").hidden')
    key(' ', 'Space', ' ')
    check('Space deselects once', 'document.activeElement.getAttribute("aria-checked") === "false"')
    js('document.querySelectorAll(".lead-open")[2].focus()')
    key('x', 'KeyX', 'x')
    check('Row shortcut acts on the keyboard-focused lead', 'document.activeElement.closest(".row").querySelector("[data-ck]").getAttribute("aria-checked") === "true" && document.querySelectorAll(".row.picked").length === 1')
    key('x', 'KeyX', 'x')

    # Density and virtualization must agree after scrolling far down.
    key('d', 'KeyD', 'd')
    check('Compact rows retain 48px geometry', 'document.querySelector(".row").getBoundingClientRect().height === 48')
    js('document.querySelector("#scroll").scrollTop = 2400')
    settle()
    check('A virtualized row covers the scrolled viewport top edge', '[...document.querySelectorAll(".row")].some(r=>{const b=r.getBoundingClientRect(),s=document.querySelector("#scroll").getBoundingClientRect();return b.top <= s.top+1 && b.bottom > s.top})')
    key('d', 'KeyD', 'd')
    check('Comfortable rows retain 64px geometry', 'document.querySelector(".row").getBoundingClientRect().height === 64')
    js('document.querySelector("#scroll").scrollTop=0')
    settle()

    # Per-stage controls retain state and don't affect the adjacent stage.
    js('document.querySelector("[data-stage=lists]").focus()')
    key('Enter', 'Enter', '\r')
    check('List pause leaves bios running', 'document.querySelector("[data-stage=lists]").dataset.action === "resume" && document.querySelector("[data-stage=bios]").dataset.action === "pause"')
    check('Async stage action retains keyboard focus', 'document.activeElement.dataset.stage === "lists" && !document.activeElement.disabled')
    click_node('button', 'Resume collect lists')
    check('List resume works', 'document.querySelector("[data-stage=lists]").dataset.action === "pause"')

    for width in [1280, 1024, 768, 390]:
        cdp('Emulation.setDeviceMetricsOverride', width=width, height=900, deviceScaleFactor=1, mobile=width < 500)
        settle()
        js('document.querySelector(".lead-open").focus()')
        key('Enter', 'Enter', '\r')
        check(f'Detail {width}: contained', 'document.querySelector("#detail").getBoundingClientRect().right <= innerWidth && document.documentElement.scrollWidth <= innerWidth')
        key('Escape', 'Escape')
        click_node('button', 'Filters')
        check(f'Filters {width}: expanded state matches', 'document.querySelector("#filters-btn").getAttribute("aria-expanded") === String(innerWidth <= 900 ? document.querySelector("#filters").classList.contains("show") : !document.querySelector("#view-work").classList.contains("work-noside"))')
        click_node('button', 'Filters') if width > 900 else key('Escape', 'Escape')

    click_node('link', 'Settings')
    js('document.querySelector(".skip-link").focus()')
    key('Enter', 'Enter', '\r')
    check('Skip link preserves the current page', 'location.hash === "#/settings" && document.activeElement.id === "main-content"')
    cdp('Emulation.setEmulatedMedia', features=[{'name': 'prefers-reduced-motion', 'value': 'reduce'}])
    check('Reduced motion removes pulse animation', 'getComputedStyle(document.querySelector(".dot")).animationName === "none"')
    check('No uncaught errors after interaction checks', 'window.uiErrors.length === 0')
finally:
    (out / 'checks.json').write_text(json.dumps(checks, indent=2))
    print(json.dumps({'passed': sum(c['passed'] for c in checks), 'total': len(checks), 'output': str(out)}))
