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
    cdp('DOM.scrollIntoViewIfNeeded', backendNodeId=node['backendDOMNodeId'])
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


