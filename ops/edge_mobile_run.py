#!/usr/bin/env python3
"""Bounded mobile edge benchmark. Requires an already enabled server experiment."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import mobile_collector as mobile
import edge_mobile_transport as transport


def save(path, value):
    """Durable private outbox, including the pre-send uncertainty marker."""
    import os
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def run_once(api, client, outbox):
    path = Path(outbox)
    viewer = mobile.session_identity(client)
    if viewer != api.viewer_id:
        raise mobile.Stopped('Saved session does not match the benchmark viewer')
    pending = json.loads(path.read_text()) if path.exists() else None
    if pending:
        if pending.get('lane') != api.lane or pending.get('viewer') != viewer:
            raise mobile.Stopped('Benchmark outbox belongs to another identity')
        if pending.get('phase') == 'inflight':
            raise mobile.Stopped('An interrupted HTTP attempt needs review; no new request made')
        if pending.get('phase') == 'result':
            body = pending['body']
            ack = api.call('/api/benchmark/result', body)
            if ack.get('ack') is not True:
                raise mobile.Stopped('Benchmark result was not acknowledged; outbox retained')
            path.unlink()
            return {'state': 'stopped' if ack.get('stopped') or (body['terminal_warning'] and ack.get('stop_scope') not in ('target', 'arm')) else 'saved', 'replayed': True,
                    'upstream_requests': 0, 'status': body['status'], 'returned_count': body['returned_count'],
                    'duration_ms': body['duration_ms'], 'raw_returned_count': body.get('raw_returned_count'),
                    'failure_reason': body.get('failure_reason'), 'reason_flags': body.get('reason_flags', []),
                    'stop_scope': ack.get('stop_scope')}
        if pending.get('phase') != 'reserved':
            raise mobile.Stopped('Invalid benchmark outbox')
        task = pending['task']
    else:
        state = api.call('/api/benchmark/next?transport=mobile')
        if state.get('enabled') is not True or state.get('stopped'):
            return {'state': 'stopped', 'upstream_requests': 0}
        task = state.get('task')
        if not task:
            return {'state': 'waiting' if state.get('wait_ms') else 'idle',
                    'wait_ms': state.get('wait_ms', 0), 'upstream_requests': 0}
        if task.get('lane') != api.lane or task.get('transport') != 'mobile' or not task.get('task_id'):
            raise mobile.Stopped('Unbound mobile benchmark task')
        # Validate everything local before reserving shared upstream capacity.
        transport.construct_request(client, task, viewer)
        pending = {'phase': 'reserved', 'lane': api.lane, 'viewer': viewer, 'task': task,
                   'request_id': str(uuid.uuid4())}
        save(path, pending)
    permit = api.call('/api/benchmark/permit', {'transport': 'mobile', 'task_id': task['task_id'],
                     'request_id': pending['request_id'], 'fingerprints': {'device': transport.device_fingerprint(client), 'session': transport.session_fingerprint(client)}})
    if permit.get('granted') is not True:
        return {'state': 'stopped' if permit.get('stopped') or permit.get('enabled') is False else 'waiting',
                'wait_ms': permit.get('wait_ms', 0), 'upstream_requests': 0}
    if not permit.get('token') or permit.get('request_id', pending['request_id']) != pending['request_id']:
        raise mobile.Stopped('Invalid benchmark permit')
    pending.update(phase='inflight', token=permit['token'])
    save(path, pending)
    try:
        expiry = datetime.fromisoformat(permit['expires_at'].replace('Z', '+00:00'))
        fresh = expiry.tzinfo is not None and (expiry - datetime.now(timezone.utc)).total_seconds() >= 55
    except (KeyError, ValueError, TypeError, AttributeError):
        fresh = False
    if fresh:
        result = transport.one_request(client, task, viewer)
    else:
        result = {'rows': [], 'next_cursor': None, 'has_more': None, 'status': 'permit_expired',
                  'terminal_warning': True, 'actual_http_requests': 0, 'http_status': 0,
                  'transport_completed': True, 'requested_count': task.get('page_size'),
                  'returned_count': 0, 'duration_ms': 0}
    body = dict(result, task_id=task['task_id'], request_id=pending['request_id'], token=permit['token'], transport='mobile')
    pending.update(phase='result', body=body)
    save(path, pending)
    delivered = run_once(api, client, path)
    return dict(delivered, replayed=False, upstream_requests=result['actual_http_requests'], attempted_requests=int(fresh))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', required=True, type=Path)
    parser.add_argument('--lane', required=True)
    parser.add_argument('--viewer-id', required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8777')
    parser.add_argument('--outbox', type=Path)
    parser.add_argument('--max-requests', type=int, choices=range(1, 101), default=1,
                        help='Actual HTTP attempt limit; defaults to one warmup request')
    parser.add_argument('--max-seconds', type=int, default=120, help='Bound total waiting, default 120 seconds')
    args = parser.parse_args()
    if not 1 <= args.max_seconds <= 3600:
        parser.error('--max-seconds must be between 1 and 3600')
    outbox = args.outbox or mobile.default_outbox(args.lane).with_name('edge-benchmark-outbox.json')
    api = mobile.LocalAPI(args.lane, args.viewer_id, args.server)
    client = mobile.load_client(args.settings)
    outbox.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with outbox.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise mobile.Stopped('Another runner owns this benchmark outbox') from None
        attempts, deadline = 0, time.monotonic() + args.max_seconds
        while attempts < args.max_requests and time.monotonic() < deadline:
            result = run_once(api, client, outbox)
            attempts += result.get('attempted_requests', result.get('upstream_requests') or 0)
            print(json.dumps(dict(result, run_http_attempts=attempts)), flush=True)
            if result['state'] in ('stopped', 'idle') or result.get('replayed'):
                break
            if result['state'] == 'waiting':
                wait = result.get('wait_ms')
                if not isinstance(wait, (int, float)) or wait <= 0:
                    break
                remaining = deadline - time.monotonic()
                if wait / 1000 >= remaining:
                    break
                time.sleep(wait / 1000)


if __name__ == '__main__':
    try:
        main()
    except (mobile.Stopped, ValueError) as exc:
        print('Benchmark stopped: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('Benchmark stopped. Check local API, saved settings, and retained outbox.', file=sys.stderr)
        sys.exit(1)
