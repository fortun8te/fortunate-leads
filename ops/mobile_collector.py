#!/usr/bin/env python3
"""Explicit optional mobile trial. Default action is read-only preflight."""
import argparse
import fcntl
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db
import mobile_collector as mobile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'configure', 'disable', 'queue', 'run'), nargs='?', default='check')
    parser.add_argument('--lane', required=True)
    parser.add_argument('--viewer-id', required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8777')
    parser.add_argument('--settings', type=Path, help='Existing private instagrapi settings file; never a browser cookie export')
    parser.add_argument('--db', type=Path, help='App database, only used by explicit configure action')
    parser.add_argument('--enable', action='store_true', help='Explicitly assign the paused alternate to mobile collection')
    parser.add_argument('--seed')
    parser.add_argument('--direction', choices=('followers', 'following'), default='followers')
    parser.add_argument('--count', type=int, choices=(25, 50, 100, 200), default=200)
    parser.add_argument('--max-pages', type=int, choices=(1, 2, 3, 4), default=4)
    parser.add_argument('--outbox', type=Path, help='Optional private outcome file; defaults to a separate directory per lane')
    args = parser.parse_args()
    args.outbox = args.outbox or mobile.default_outbox(args.lane)
    if args.action in ('configure', 'disable'):
        if not args.db or not args.db.is_file():
            parser.error('configure requires --db pointing to the existing migrated app database')
        conn = db.connect(str(args.db))
        try:
            result = mobile.configure_lane(conn, args.lane, args.viewer_id, enabled=args.enable, disable=args.action == 'disable')
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        print(json.dumps(result))
        return
    api = mobile.LocalAPI(args.lane, args.viewer_id, args.server)
    if args.action == 'check':
        print(json.dumps(api.call('/api/mobile/state')))
        return
    if args.action == 'queue':
        if not args.seed:
            parser.error('queue requires --seed')
        print(json.dumps(api.call('/api/mobile/queue', {'seed': args.seed, 'direction': args.direction, 'count': args.count})))
        return
    if not args.settings or not args.settings.is_file():
        parser.error('run requires an explicit existing --settings file; this tool never logs in')
    # Check server enablement/identity before importing the optional dependency.
    state = api.call('/api/mobile/state')
    if state.get('backend_cursor_isolated') is not True:
        raise mobile.Stopped('Server has not enabled isolated mobile collection')
    client = mobile.load_client(args.settings)
    args.outbox.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.outbox.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise mobile.Stopped('Another mobile trial owns this outbox') from None
        requests = 0
        for index in range(args.max_pages):
            if index:
                time.sleep(12)
            result = mobile.run_once(api, client, args.outbox)
            requests += result.get('upstream_requests', 0)
            print(json.dumps(dict(result, trial_upstream_requests=requests)), flush=True)
            if result['state'] != 'saved' or result.get('replayed') and not result.get('upstream_requests'):
                break


if __name__ == '__main__':
    try:
        main()
    except (mobile.Stopped, ValueError) as exc:
        print('Mobile collector stopped: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Session settings and HTTP response bodies must never appear in errors.
        print('Mobile collector stopped. Check local API availability, optional dependency and private settings file.', file=sys.stderr)
        sys.exit(1)
