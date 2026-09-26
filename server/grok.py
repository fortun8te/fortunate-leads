"""Bulk AI on Michael's SuperGrok plan through Hermes (profile `leadqual`, provider xai-oauth).
Hermes calls the xAI API directly: ~4k tokens of overhead per call against ~17.5k for the Grok Build CLI, and half the latency.
One call scores up to BATCH people; Hermes records every call's tokens in the profile's state.db (the 7-day usage view)."""
import os
import subprocess
import time
from pathlib import Path

BIN = Path.home() / '.local' / 'bin' / 'hermesme'
PROFILE = 'leadqual'
STATE_DB = Path.home() / '.hermes-me' / 'profiles' / PROFILE / 'state.db'
MODEL = 'grok-4.7'
EFFORT = 'low'      # lowest Grok 4.7 accepts; needs the Hermes allowlist fix (upstream #121796) or it silently runs at high
BATCH = 25          # 25/25 parsed on real leads; 50 per call came back empty
TIMEOUT = 420


class Failed(Exception):
    pass


def available():
    return not os.environ.get('FL_NO_ORSLOT') and BIN.exists()


def chat(messages, timeout=TIMEOUT):
    """messages = [system, user] -> (text, model). Raises Failed on any CLI, quota or parse problem."""
    prompt = '\n\n'.join(m['content'] for m in messages) + '\n\nReply with ONLY the JSON object.'
    try:
        out = subprocess.run([str(BIN), '-p', PROFILE, '--provider', 'xai-oauth', '-m', MODEL, '--reasoning', EFFORT,
                              '-t', 'clarify', '--ignore-rules', '-z', prompt.replace('\0', '')],
                             capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd='/tmp')
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Failed(str(e)[:200]) from None
    text = out.stdout.strip()
    if out.returncode or not text:
        raise Failed((out.stderr or text or 'empty reply')[-200:])
    return text, MODEL


def usage(days=7):
    """{'calls', 'tokens_in', 'tokens_out', 'today'} over the last `days`, from Hermes' own session records."""
    import sqlite3
    since, midnight = time.time() - days * 86400, time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    try:
        con = sqlite3.connect(f'file:{STATE_DB}?mode=ro', uri=True, timeout=2)
        row = con.execute('SELECT count(*), coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0), '
                          'coalesce(sum(started_at>=?),0) FROM sessions WHERE started_at>=?', (midnight, since)).fetchone()
        con.close()
    except sqlite3.Error:
        return {'calls': 0, 'tokens_in': 0, 'tokens_out': 0, 'today': 0}
    return dict(zip(('calls', 'tokens_in', 'tokens_out', 'today'), row))
