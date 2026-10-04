#!/usr/bin/env python3
"""Photo store audit: sizes, damaged and orphaned files, duplicates, and what is missing and fetchable.

    python3 ops/photo_audit.py --db data/leads.sqlite                 # read only; add --hash for duplicates
    python3 ops/photo_audit.py --db data/leads.sqlite --hash --thumb-sample 200 --json audit.json

Read only and offline: it reads the database (mode=ro), lists data/pfp and reads files, and sends no
network request. Nothing is changed or deleted. The server already refetches photos that are missing
or damaged (backend/photos.py repair_pfp_cache + pfp_step, one photo at a time, never while a Meta
network hold is active); this tool says how many that is and how many still have a usable address.

Instagram's picture addresses carry an expiry (the `oe=` hex timestamp). An expired address cannot be
fetched again until the profile is read again, so those photos cannot be refetched from the saved URL.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'server'))
import dbsafe  # noqa: E402

PIC_MAX = 2 * 1024 * 1024
NAME = re.compile(r'(\d+)\.jpg$')
TEMP = re.compile(r'\.\d+\..*\.tmp$')


def valid_pic_file(path_or_head_tail, size=None):
    """Same rule as backend/photos.py PhotoService.valid_pic_file (a test keeps the two in step)."""
    if isinstance(path_or_head_tail, tuple):
        head, tail = path_or_head_tail
    else:
        try:
            size = os.stat(path_or_head_tail).st_size
            if size < 12 or size > PIC_MAX:
                return False
            with open(path_or_head_tail, 'rb') as f:
                head = f.read(12)
                f.seek(-12, os.SEEK_END)
                tail = f.read(12)
        except OSError:
            return False
    if size < 12 or size > PIC_MAX:
        return False
    return ((head[:3] == b'\xff\xd8\xff' and tail[-2:] == b'\xff\xd9')
            or (head[:8] == b'\x89PNG\r\n\x1a\n' and tail[4:8] == b'IEND')
            or (head[:4] == b'RIFF' and head[8:12] == b'WEBP' and int.from_bytes(head[4:8], 'little') == size - 8))


def url_expiry(url):
    """Unix time an Instagram picture address stops working (its oe= hex timestamp), or None."""
    try:
        value = parse_qs(urlparse(url).query).get('oe', [None])[0]
        return int(value, 16) if value else None
    except (ValueError, TypeError):
        return None


def percentile(values, q):
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def scan_directory(pfp, *, hash_files=False):
    """name -> {size, blocks, valid, kind, sha}. One pass; reads head and tail (or the whole file to hash)."""
    files, other = {}, []
    with os.scandir(pfp) as entries:
        for entry in entries:
            if entry.is_symlink() or not entry.is_file():
                other.append(entry.name)
                continue
            info = entry.stat(follow_symlinks=False)
            row = {'size': info.st_size, 'blocks': info.st_blocks * 512, 'mtime': info.st_mtime}
            if TEMP.search(entry.name):
                row['kind'] = 'temp'
            elif NAME.fullmatch(entry.name):
                row['kind'] = 'photo'
            else:
                row['kind'] = 'other'
            if row['kind'] == 'photo':
                try:
                    with open(entry.path, 'rb') as f:
                        if hash_files:
                            data = f.read(PIC_MAX + 1)
                            row['sha'] = hashlib.sha256(data).hexdigest()
                            head, tail = data[:12], data[-12:]
                        else:
                            head = f.read(12)
                            f.seek(max(row['size'] - 12, 0))
                            tail = f.read(12)
                    row['valid'] = valid_pic_file((head, tail), row['size'])
                    row['format'] = ('jpeg' if head[:3] == b'\xff\xd8\xff' else 'png' if head[:4] == b'\x89PNG'
                                     else 'webp' if head[8:12] == b'WEBP' else 'unknown')
                except OSError:
                    row['valid'] = False
            files[entry.name] = row
    return files, other


def thumbnail_sample(pfp, names, count, edge=128):
    """Resize `count` random photos with macOS sips into a temp folder and report the size ratio."""
    sips = shutil.which('sips') or '/usr/bin/sips'
    if not os.path.exists(sips) or not names:
        return None
    picks = random.Random(7).sample(names, min(count, len(names)))
    original = thumb = 0
    done = 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='fl-thumbs-') as tmp:
        for name in picks:
            out = Path(tmp) / name
            result = subprocess.run([sips, '-Z', str(edge), '--setProperty', 'formatOptions', '70',
                                     str(Path(pfp) / name), '--out', str(out)], capture_output=True)
            if result.returncode == 0 and out.exists():
                original += (Path(pfp) / name).stat().st_size
                thumb += out.stat().st_size
                done += 1
    if not done:
        return None
    return {'edge_px': edge, 'sampled': done, 'original_bytes': original, 'thumbnail_bytes': thumb,
            'ratio': round(thumb / original, 3), 'seconds_per_photo': round((time.monotonic() - started) / done, 3)}


def audit(db_path, pfp, *, hash_files=False, thumb_sample=0, now=None):
    now = now or time.time()
    started = time.monotonic()
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    conn.execute('PRAGMA query_only=ON')
    people = {r[0]: r for r in conn.execute(
        'SELECT id, pic_file, pic_url, pic_refresh, pic_attempts, pic_retry_at, handle FROM people')}
    conn.close()
    files, other = scan_directory(pfp, hash_files=hash_files)
    photos = {n: r for n, r in files.items() if r['kind'] == 'photo'}
    temp = {n: r for n, r in files.items() if r['kind'] == 'temp'}
    report = {'people': len(people), 'directory': str(pfp), 'files': len(photos),
              'bytes': sum(r['size'] for r in photos.values()),
              'allocated_bytes': sum(r['blocks'] for r in photos.values()),
              'temp_files': {'count': len(temp), 'bytes': sum(r['size'] for r in temp.values())},
              'other_entries': other[:20], 'other_count': len(other)}
    report['slack_bytes'] = report['allocated_bytes'] - report['bytes']
    sizes = [r['size'] for r in photos.values()]
    report['size_bytes'] = {'median': int(statistics.median(sizes)) if sizes else 0, 'p95': percentile(sizes, 0.95),
                            'max': max(sizes, default=0)}
    report['formats'] = dict(Counter(r.get('format') for r in photos.values()))

    states = Counter()
    refetch = {'fetchable': [], 'expired': [], 'no_url': []}
    invalid, orphan, missing = [], [], []
    for pid, (_, pic_file, pic_url, pic_refresh, attempts, retry_at, handle) in people.items():
        name = f'{pid}.jpg'
        present = photos.get(name)
        if pic_file:
            if present and pic_file == name and present['valid']:
                states['ok'] += 1
            elif present and not present['valid']:
                states['damaged_file'] += 1
                invalid.append(pid)
            else:
                states['missing_file'] += 1
                missing.append(pid)
        else:
            if present and present['valid']:
                states['file_without_database_link'] += 1
                orphan.append(pid)
            if pic_url:
                expiry = url_expiry(pic_url)
                bucket = 'expired' if expiry is not None and expiry < now else 'fetchable'
                refetch[bucket].append(pid)
                states['no_photo_with_address'] += 1
            else:
                refetch['no_url'].append(pid)
                states['no_photo_no_address'] += 1
    unowned = [n for n in photos if int(NAME.fullmatch(n).group(1)) not in people]
    report['states'] = dict(states)
    report['refetch'] = {k: len(v) for k, v in refetch.items()}
    report['refetch']['already_retrying'] = sum(1 for pid in refetch['fetchable'] if people[pid][4])
    report['files_for_unknown_people'] = {'count': len(unowned), 'bytes': sum(photos[n]['size'] for n in unowned)}
    report['damaged_on_disk'] = sum(1 for r in photos.values() if not r['valid'])
    if hash_files:
        groups = {}
        for name, row in photos.items():
            if row.get('valid') and row.get('sha'):
                groups.setdefault(row['sha'], []).append(name)
        dupes = {sha: names for sha, names in groups.items() if len(names) > 1}
        extra = [photos[n] for names in dupes.values() for n in names[1:]]
        report['duplicates'] = {'groups': len(dupes), 'extra_files': len(extra),
                                'extra_bytes': sum(r['size'] for r in extra),
                                'extra_allocated_bytes': sum(r['blocks'] for r in extra),
                                'largest_group': max((len(n) for n in dupes.values()), default=0)}
        report['_duplicate_groups'] = {sha: sorted(names) for sha, names in list(dupes.items())[:50]}
    else:
        report['duplicates'] = 'not computed (add --hash)'
    if thumb_sample:
        report['thumbnails'] = thumbnail_sample(pfp, [n for n, r in photos.items() if r['valid']], thumb_sample)
        if report['thumbnails']:
            t = report['thumbnails']
            report['thumbnails']['projected_total_bytes'] = int(report['bytes'] * t['ratio'])
    report['seconds'] = round(time.monotonic() - started, 1)
    report['_ids'] = {'damaged': invalid[:5000], 'missing_file': missing[:5000],
                      'fetchable': refetch['fetchable'][:5000], 'expired': refetch['expired'][:5000]}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, default=REPO / 'data' / 'leads.sqlite')
    parser.add_argument('--pfp', type=Path, help='photo folder (default: pfp next to the database)')
    parser.add_argument('--hash', action='store_true', help='read every photo and find exact duplicates')
    parser.add_argument('--thumb-sample', type=int, default=0, metavar='N',
                        help='resize N random photos with sips to measure what thumbnails would save')
    parser.add_argument('--json', type=Path, help='write the full report here (includes id lists)')
    args = parser.parse_args(argv)
    pfp = args.pfp or args.db.resolve().parent / 'pfp'
    report = audit(args.db, pfp, hash_files=args.hash, thumb_sample=args.thumb_sample)
    h = dbsafe.human_bytes
    print(f"people {report['people']:,}   photo files {report['files']:,}   in {report['seconds']}s")
    print(f"size {h(report['bytes'])}  on disk {h(report['allocated_bytes'])}  (block slack {h(report['slack_bytes'])})  "
          f"median {h(report['size_bytes']['median'])}  p95 {h(report['size_bytes']['p95'])}  max {h(report['size_bytes']['max'])}")
    print('formats', report['formats'])
    for key, value in sorted(report['states'].items()):
        print(f'  {key:<30}{value:>9,}')
    r = report['refetch']
    print(f"no photo: {r['fetchable']:,} have a usable address, {r['expired']:,} address expired, {r['no_url']:,} no address")
    print(f"damaged files {report['damaged_on_disk']:,}   temp files {report['temp_files']['count']:,} "
          f"({h(report['temp_files']['bytes'])})   files for unknown people {report['files_for_unknown_people']['count']:,} "
          f"({h(report['files_for_unknown_people']['bytes'])})")
    d = report['duplicates']
    if isinstance(d, dict):
        print(f"duplicates: {d['groups']:,} groups, {d['extra_files']:,} extra files, {h(d['extra_bytes'])} "
              f"({h(d['extra_allocated_bytes'])} on disk)")
    else:
        print('duplicates:', d)
    if report.get('thumbnails'):
        t = report['thumbnails']
        print(f"thumbnails at {t['edge_px']}px: {t['ratio']:.0%} of the original size "
              f"({h(t['projected_total_bytes'])} for all), {t['seconds_per_photo']}s each to make")
    if args.json:
        dbsafe.write_json_atomic(args.json, report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
