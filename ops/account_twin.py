#!/usr/bin/env python3
"""Run the opt-in second-profile test on @trashtheturtles, one step at a time. See docs/SECOND_PROFILE_TEST.md.

Nothing here sends an Instagram request. It reads and changes the local workspace's test state; the extension in each
Chrome profile does the collecting. Every command that changes state prints what it will do and needs --yes.

  status                         where the test is now (add --json for the raw answer)
  baseline-start --first LANE    step 1: measure the first profile alone for an hour
  baseline-finish --yes          accept the hour as the baseline
  enable --first LANE --second LANE --yes
                                 step 2: switch the second profile on, capped at the baseline
  advance --yes                  step 3+: allow 25% more, only after a clean full step that used its cap
  watch                          print one line a minute; exits with 2 the moment the test stops itself
  disable --yes [--reason TEXT]  end the test now (the second profile is paused)
  reset --yes                    clear a finished or stopped test (never resumes collection)
  report [--out FILE]            the numbers that answer "did it double?"

The speed of each profile is set where it always is (the popup and the speed control). This test has no speed
setting of its own; it only caps the two profiles' combined requests at what one profile may do.
"""
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = 'http://127.0.0.1:8777'
PATH = '/api/account-twin'
STOPPED = 2


def base_url(value):
    try:
        parsed = urlsplit(value.rstrip('/'))
        valid = (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost') and parsed.port
                 and not parsed.path and not parsed.query and not parsed.fragment and not parsed.username)
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError('base URL must be http://127.0.0.1:PORT or http://localhost:PORT')
    return value.rstrip('/')


def request(base, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {'Accept': 'application/json'}
    if body is not None:
        headers.update({'Origin': base, 'Content-Type': 'application/json'})
    try:
        with urlopen(Request(base + PATH, data=data, headers=headers), timeout=15) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            detail = json.load(error).get('error', error.reason)
        except (ValueError, OSError, AttributeError):
            detail = error.reason
        raise RuntimeError(str(detail)) from error
    except URLError as error:
        raise RuntimeError(f'Cannot reach Fortunate Leads at {base}: {error.reason}') from error
    return result


def number(value, unit=''):
    return '-' if value is None else f'{value:g}{unit}'


def describe(status):
    lines = [f"state        {status['state']}"]
    if status.get('lanes'):
        lines.append('profiles     ' + ', '.join(status['lanes']))
    base = status.get('baseline')
    if base:
        lines.append(f"baseline     {number(base.get('requests_per_hour'))} requests/h, "
                     f"{number(base.get('list_pages_per_hour'))} list pages/h over {number(base.get('minutes'))} min")
    cur = status.get('current')
    if cur:
        label = cur.get('label', 'baseline')
        cap = f" of cap {cur['cap_per_hour']}" if 'cap_per_hour' in cur else ''
        lines.append(f"now          {label}: {number(cur.get('minutes'))} min, {cur['requests']} requests = "
                     f"{number(cur['requests_per_hour'])}/h{cap}, {number(cur['list_pages_per_hour'])} list pages/h, "
                     f"{cur['people']} people, {cur['bios']} bios")
        if cur.get('by_lane'):
            lines.append('per profile  ' + ', '.join(f'{lane} {n}' for lane, n in cur['by_lane'].items()))
        if 'vs_baseline' in cur:
            lines.append(f"vs baseline  {number(cur['vs_baseline'])}x the list pages per hour (the proof metric)")
            lines.append(f"next step    in {number(cur['minutes_left'])} min, if it used at least 80% of its cap and nothing went wrong")
    for step in status.get('history', []):
        lines.append(f"done         {step['label']}: cap {step['cap_per_hour']}, {number(step['requests_per_hour'])}/h, "
                     f"{number(step['list_pages_per_hour'])} list pages/h over {number(step['minutes'])} min")
    trip = status.get('trip')
    if trip:
        lines.append(f"STOPPED      {trip['code']} on {trip['lane']} after {number(trip.get('after_minutes'))} min: {trip['reason']}")
        lines.append('             Collection is paused. Check the account in its Chrome profile before anything else.')
    limits = status.get('limits') or {}
    if limits:
        lines.append(f"ceiling      {limits['ceiling_per_hour']} requests/h (what one profile may do at the current pace)")
    pre = status.get('preflight')
    if pre:
        lines.append('ready        ' + ('yes' if pre['ok'] else 'no'))
        for blocker in pre['blockers']:
            lines.append(f"  - {blocker['text']}" + (f" ({blocker['lane_id']})" if blocker.get('lane_id') else ''))
    return '\n'.join(lines)


def act(base, body, args):
    if not args.yes:
        print('This would send:\n' + json.dumps(body, indent=2) + '\nRun again with --yes to do it.')
        return 1
    print(describe(request(base, dict(body, reviewed=True))))
    return 0


def report(status):
    base = status.get('baseline') or {}
    rows = [dict(label='baseline (one profile)', **{k: base.get(k) for k in ('minutes', 'requests', 'requests_per_hour', 'list_pages_per_hour')})] \
        if base else []
    rows += [dict(label=h['label'], cap=h['cap_per_hour'], **{k: h.get(k) for k in ('minutes', 'requests', 'requests_per_hour', 'list_pages_per_hour')})
             for h in status.get('history', [])]
    cur = status.get('current')
    if cur and 'cap_per_hour' in cur:
        rows.append(dict(label=cur['label'] + ' (running)', cap=cur['cap_per_hour'],
                         **{k: cur.get(k) for k in ('minutes', 'requests', 'requests_per_hour', 'list_pages_per_hour')}))
    base_pages = base.get('list_pages_per_hour')
    for row in rows:
        row['x_baseline'] = round(row['list_pages_per_hour'] / base_pages, 2) if base_pages and row.get('list_pages_per_hour') is not None else None
    return {'state': status['state'], 'ig_id': status.get('ig_id'), 'trip': status.get('trip'), 'rows': rows,
            'how_to_read': 'The test proves doubling only if a step with zero adverse events, run for its full time and using at '
                           'least 80% of its cap, shows list pages per hour near 2x the baseline. The cap never goes above what one '
                           'profile may do, so a ratio near 2x needs a baseline near half of that.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base-url', type=base_url, default=DEFAULT_BASE_URL)
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('status',):
        sub.add_parser(name).add_argument('--json', action='store_true')
    sub.add_parser('baseline-start').add_argument('--first', required=True, help='lane id of the original profile')
    for name in ('baseline-finish', 'advance', 'reset'):
        sub.add_parser(name)
    enable = sub.add_parser('enable')
    enable.add_argument('--first', required=True)
    enable.add_argument('--second', required=True)
    disable = sub.add_parser('disable')
    disable.add_argument('--reason', default='')
    watch = sub.add_parser('watch')
    watch.add_argument('--every', type=int, default=60)
    rep = sub.add_parser('report')
    rep.add_argument('--out', type=Path)
    for name, p in sub.choices.items():
        if name in ('baseline-start', 'baseline-finish', 'enable', 'advance', 'disable', 'reset'):
            p.add_argument('--yes', action='store_true', help='confirm that you reviewed the plan')
    args = parser.parse_args(argv)
    try:
        if args.cmd == 'status':
            status = request(args.base_url)
            print(json.dumps(status, indent=2) if args.json else describe(status))
            return 0
        if args.cmd == 'baseline-start':
            return act(args.base_url, {'action': 'start_baseline', 'lane_id': args.first}, args)
        if args.cmd == 'baseline-finish':
            return act(args.base_url, {'action': 'finish_baseline'}, args)
        if args.cmd == 'enable':
            return act(args.base_url, {'action': 'enable', 'lane_ids': [args.first, args.second]}, args)
        if args.cmd == 'advance':
            return act(args.base_url, {'action': 'advance'}, args)
        if args.cmd == 'disable':
            return act(args.base_url, {'action': 'disable', 'reason': args.reason}, args)
        if args.cmd == 'reset':
            return act(args.base_url, {'action': 'reset'}, args)
        if args.cmd == 'watch':
            while True:
                status = request(args.base_url)
                cur = status.get('current') or {}
                print(time.strftime('%H:%M'), status['state'], cur.get('label', ''), f"{number(cur.get('requests_per_hour'))}/h",
                      f"{number(cur.get('list_pages_per_hour'))} pages/h", flush=True)
                if status['state'] == 'tripped':
                    print(describe(status))
                    return STOPPED
                if status['state'] in ('off', 'finished'):
                    return 0
                time.sleep(max(10, args.every))
        if args.cmd == 'report':
            out = report(request(args.base_url))
            text = json.dumps(out, indent=2)
            if args.out:
                args.out.write_text(text + '\n', encoding='utf-8')
            print(text)
            return 0
    except (RuntimeError, OSError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
