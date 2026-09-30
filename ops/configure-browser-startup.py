#!/usr/bin/env python3
"""Explicitly bind already verified account lanes to existing Chrome profiles."""
import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'server'))
import browser_startup


def configure(repo, bindings, chrome_root, *, enabled=True):
    repo = Path(repo)
    profiles = []
    with sqlite3.connect('file:' + str(repo / 'data/leads.sqlite') + '?mode=ro', uri=True) as conn:
        for binding in bindings:
            directory, separator, lane = binding.partition('=')
            if (not separator or not re.fullmatch(r'Default|Profile [1-9][0-9]*', directory)
                    or not browser_startup.accounts.LANE_RX.fullmatch(lane)):
                raise ValueError('Use PROFILE_DIRECTORY=VERIFIED_LANE_ID for each profile.')
            if not (Path(chrome_root) / directory / 'Preferences').is_file():
                raise ValueError('Chrome profile does not already exist: ' + directory)
            row = conn.execute('SELECT ig_id, collection_backend FROM accounts WHERE lane_id=?', (lane,)).fetchone()
            if not row or not row[0] or row[1] != 'chrome':
                raise ValueError('This Chrome account has not reported a logged-in identity: ' + lane)
            profiles.append({'directory': directory, 'lane_id': lane, 'ig_id': str(row[0])})
    path = repo / 'data/browser-startup.json'
    temporary = path.with_suffix('.json.tmp')
    payload = {'enabled': enabled, 'profiles': profiles}
    # Validate before replacing an existing working configuration.
    import tempfile
    with tempfile.TemporaryDirectory() as scratch:
        validation = Path(scratch) / 'data'
        validation.mkdir()
        (validation / path.name).write_text(json.dumps(payload), encoding='utf-8')
        browser_startup.read_config(scratch)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, indent=2)
        stream.write('\n')
    temporary.replace(path)
    return len(profiles)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', action='append', default=[])
    parser.add_argument('--disable', action='store_true')
    args = parser.parse_args()
    if not args.profile and not args.disable:
        parser.error('Provide each verified --profile binding, or --disable.')
    try:
        count = configure(REPO, args.profile, Path.home() / 'Library/Application Support/Google/Chrome', enabled=not args.disable)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, str(exc) + '\n')
    print('Background profile startup disabled.' if args.disable else f'Background startup enabled for {count} saved profiles. Login is still checked by the extension.')


if __name__ == '__main__':
    main()
