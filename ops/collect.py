#!/usr/bin/env python3
"""Agent-friendly client for the Fortunate Leads durable Instagram list queue."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from db import norm_handle  # noqa: E402 - use exactly the server's input contract

DEFAULT_BASE_URL = 'http://127.0.0.1:8777'


def base_url(value: str) -> str:
    try:
        parsed = urlsplit(value.rstrip('/'))
        valid = (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')
                 and parsed.port and not parsed.path and not parsed.query and not parsed.fragment
                 and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError('base URL must be http://127.0.0.1:PORT or http://localhost:PORT')
    return value.rstrip('/')


def request(base: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode('utf-8') if body is not None else None
    headers = {'Accept': 'application/json'}
    if body is not None:
        headers.update({'Origin': base, 'Content-Type': 'application/json'})
    try:
        with urlopen(Request(base + path, data=data, headers=headers), timeout=10) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get('ok') is False:
            raise RuntimeError(f'{path}: {result.get("error", "invalid response") if isinstance(result, dict) else "invalid response"}')
        return result
    except HTTPError as error:
        try:
            detail = json.load(error).get('error', error.reason)
        except (ValueError, OSError, AttributeError):
            detail = error.reason
        raise RuntimeError(f'{path}: HTTP {error.code}: {detail}') from error
    except URLError as error:
        raise RuntimeError(f'Cannot reach Fortunate Leads at {base}: {error.reason}') from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RuntimeError(f'{path}: server did not return JSON') from error


def profiles_from(args: argparse.Namespace) -> list[str]:
    raw = list(args.profiles)
    if args.file:
        raw.extend(args.file.read_text(encoding='utf-8').splitlines())
    profiles = []
    for item in raw:
        item = item.strip()
        if not item or item.startswith('#'):
            continue
        handle = norm_handle(item)
        if not handle or '~' in handle:
            raise ValueError(f'Invalid Instagram profile: {item}')
        profiles.append(handle)
    profiles = list(dict.fromkeys(profiles))
    if not profiles:
        raise ValueError('Provide at least one Instagram profile or --file')
    return profiles


def status(base: str, details: bool = False) -> dict:
    state = request(base, '/api/scraper')
    ext = state.get('ext') or {}
    lists = state.get('lists') or []
    accounts = state.get('accounts') or []
    list_states = {name: sum(1 for row in lists if row.get('state') == name)
                   for name in sorted({row.get('state') for row in lists if isinstance(row.get('state'), str)})}
    account_states = {name: sum(1 for row in accounts if row.get('status') == name)
                      for name in sorted({row.get('status') for row in accounts if isinstance(row.get('status'), str)})}
    progress = state.get('progress') or {}
    list_progress = progress.get('lists') or {}
    result = {
        'workspace': 'online', 'exporter': 'online' if ext.get('online') else 'offline',
        'exporter_state': ext.get('state'), 'paused': state.get('paused'),
        'cooldown_until': ext.get('cooldown_until'),
        'queue': state.get('queue'),
        'accounts': {'total': len(accounts), 'online': sum(bool(row.get('online')) for row in accounts),
                     'states': account_states},
        'list_states': list_states,
        'lists_total': len(lists),
        'list_progress': {key: list_progress.get(key) for key in ('left', 'per_hour', 'eta_h')},
    }
    if details:
        result.update(last_error=ext.get('last_error'), lists=lists,
                      account_details=accounts, progress=progress)
    return result


def queue(base: str, args: argparse.Namespace) -> dict:
    if args.new_only:
        raise ValueError('--new-only is unsupported by this API; no profiles were queued')
    profiles = profiles_from(args)
    directions = ['followers'] if args.followers else ['following'] if args.following else ['followers', 'following']
    current = request(base, '/api/scraper')
    existing = {(row.get('seed'), row.get('direction')) for row in current.get('lists') or []}
    existing_count = sum((handle, direction) in existing for handle in profiles for direction in directions)
    missing = {direction: [handle for handle in profiles if args.refresh or (handle, direction) not in existing]
               for direction in directions}
    result = {
        'profiles': profiles, 'directions': directions,
        'already_present': existing_count,
        'to_submit': {direction: handles for direction, handles in missing.items() if handles},
        'queued_lists': 0, 'dry_run': args.dry_run, 'refresh': args.refresh,
        'note': ('Queued means scheduled, not collected. Active list jobs are kept by the server.'
                 if args.refresh else 'Queued means scheduled, not collected. Existing list rows were left untouched.'),
    }
    if not args.dry_run:
        for direction, handles in missing.items():
            if handles:
                reply = request(base, '/api/scraper/seeds', {'handles': handles, 'directions': [direction], 'refresh': args.refresh})
                result['queued_lists'] += reply.get('queued', 0)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Check or queue Instagram lists in Fortunate Leads.')
    parser.add_argument('--base-url', type=base_url, default=DEFAULT_BASE_URL)
    sub = parser.add_subparsers(dest='action', required=True)
    status_parser = sub.add_parser('status', help='Read a compact collector and queue summary.')
    status_parser.add_argument('--details', action='store_true', help='Include all list rows and accounts')
    queued = sub.add_parser('queue', help='Queue missing lists for handles or profile URLs.')
    queued.add_argument('profiles', nargs='*')
    queued.add_argument('--file', type=Path, help='UTF-8 file with one profile per line')
    direction = queued.add_mutually_exclusive_group()
    direction.add_argument('--followers', action='store_true')
    direction.add_argument('--following', action='store_true')
    direction.add_argument('--both', action='store_true', help='Default: followers and following')
    queued.add_argument('--dry-run', action='store_true', help='Read current lists, show what would be queued')
    queued.add_argument('--refresh', action='store_true', help='Request a full rerun of existing lists; the server keeps active jobs')
    queued.add_argument('--new-only', action='store_true', help='Unsupported; fails without queuing')
    args = parser.parse_args(argv)
    try:
        output = status(args.base_url, args.details) if args.action == 'status' else queue(args.base_url, args)
        print(json.dumps(output, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
