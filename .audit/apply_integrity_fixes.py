"""One-shot exact-context patch application on the isolated audit branch."""
from pathlib import Path
root = Path.cwd()

def replace(path, old, new):
    p = root / path
    text = p.read_text()
    assert text.count(old) == 1, f'Unexpected source context: {path}'
    p.write_text(text.replace(old, new))

replace('server/db.py',
    'CREATE INDEX IF NOT EXISTS ai_scoring_events_at ON ai_scoring_events(scored_at);',
    'CREATE INDEX IF NOT EXISTS ai_scoring_events_at ON ai_scoring_events(scored_at);\nCREATE INDEX IF NOT EXISTS ai_scoring_events_person ON ai_scoring_events(person_id);')
replace('server/db.py',
    "columns = ('url', 'final_url', 'title', 'summary', 'signals', 'error', 'model', 'at')",
    """columns = tuple(key for key in
                                        ('url', 'final_url', 'title', 'summary', 'signals', 'error', 'model', 'at', 'content_hash')
                                        if key in dropped.keys())""")
replace('server/db.py',
    """        note_mentions.merge(conn, keep, drop)
        conn.execute('DELETE FROM people WHERE id=?', (drop,))""",
    """        note_mentions.merge(conn, keep, drop)
        # people.id may be reused after deleting the highest rowid. Move audit
        # history and invalidate input-bound caches before that identity vanishes.
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('processing_review_events', 'ai_scoring_events'):
            if table in tables:
                conn.execute(f'UPDATE {table} SET person_id=? WHERE person_id=?', (keep, drop))
        if 'processing_ai_history' in tables:
            conn.execute('''UPDATE OR IGNORE processing_ai_history
                SET person_id=?,verdict=json_set(verdict,'$.person_id',?) WHERE person_id=?''',
                (keep, keep, drop))
            # A colliding archived result is already retained on the survivor.
            conn.execute('DELETE FROM processing_ai_history WHERE person_id=?', (drop,))
            conn.execute('''DELETE FROM processing_ai_history AS h WHERE person_id=? AND id NOT IN (
                SELECT recent.id FROM processing_ai_history recent
                WHERE recent.person_id=h.person_id AND recent.model=h.model
                ORDER BY recent.id DESC LIMIT 2)''', (keep,))
        for table in ('external_attempts', 'web_research', 'owner_note_reads'):
            if table in tables:
                conn.execute(f'DELETE FROM {table} WHERE person_id=?', (drop,))
        conn.execute('DELETE FROM people WHERE id=?', (drop,))""")
replace('server/server.py',
    """    if not (isinstance(p.get('website'), str) and re.match(r'https?://[^\\s]+$', p['website'].strip(), re.I)):
        p.pop('website', None)  # javascript:, data:, bare text: never stored, never rendered as a link
    else:
        p['website'] = p['website'].strip()""",
    """    if ('website' in p and isinstance(p.get('bio'), str)
            and (p['website'] is None or isinstance(p['website'], str) and not p['website'].strip())):
        # A complete read can explicitly remove a link. Omitted fields and
        # partial reads remain unknown; unsafe URLs never replace saved links.
        p['website'] = ''
    elif not (isinstance(p.get('website'), str) and re.match(r'https?://[^\\s]+$', p['website'].strip(), re.I)):
        p.pop('website', None)  # javascript:, data:, bare text: never stored, never rendered as a link
    else:
        p['website'] = p['website'].strip()""")
replace('server/biofetch.py', 'import json\n', 'import json\nimport math\n')
p = root / 'server/biofetch.py'
s = p.read_text()
start = s.index('def usage_pct(')
end = s.index('\n\ndef pick(', start)
s = s[:start] + '''def usage_pct(headers):
    """Highest valid percentage in Meta usage headers; tolerate unknown shapes."""
    top = 0
    for k, v in headers.items():
        if k.lower() not in ('x-business-use-case-usage', 'x-app-usage'):
            continue
        try:
            data = json.loads(v)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        items = ([item for group in data.values() if isinstance(group, list) for item in group]
                 if k.lower() == 'x-business-use-case-usage' else [data])
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in ('call_count', 'total_cputime', 'total_time'):
                value = item.get(field)
                if type(value) in (int, float) and math.isfinite(value):
                    top = max(top, value)
    return top
''' + s[end:]
p.write_text(s)
replace('server/biofetch.py',
    'SELECT p.handle FROM people p LEFT JOIN verdicts v',
    'SELECT p.id,p.ig_id,p.handle,p.first_seen,p.updated_at,p.bio_at,p.bd_at FROM people p LEFT JOIN verdicts v')
replace('server/biofetch.py',
    """    status, body, headers = FETCH[0](GRAPH + urllib.parse.quote(s['ig_user_id']) + '?' + q)
    ts = db.now()""",
    """    status, body, headers = FETCH[0](GRAPH + urllib.parse.quote(s['ig_user_id']) + '?' + q)
    # Do not hold a write lock over network I/O. Revalidate the original row
    # under the same write transaction as ingestion, not by mutable handle alone.
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    current = conn.execute('SELECT * FROM people WHERE id=?', (row['id'],)).fetchone()
    fresh = current is not None and all(current[key] == row[key] for key in row.keys())
    st = state(conn)  # settings/state may have changed while the request was in flight
    ts = db.now()""")
replace('server/biofetch.py',
    """    err = body.get('error') if isinstance(body, dict) else None
    bd = body.get('business_discovery') if isinstance(body, dict) else None
    if status == 200 and isinstance(bd, dict):
""",
    """    err = body.get('error') if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else None
    bd = body.get('business_discovery') if isinstance(body, dict) else None
    matches = (isinstance(bd, dict) and
               ('username' not in bd or isinstance(bd['username'], str) and db.norm_handle(bd['username']) == h))
    if status == 200 and matches and not fresh:
        st['discarded'] = st.get('discarded', 0) + 1
        if usage_pct(headers) >= 75:
            _cool(conn, st, 'Meta usage above 75 %', now)
    elif status == 200 and matches:
""")
replace('server/biofetch.py',
    """    elif err and err.get('code') == 190:
        s['on'] = False
        db.set_setting(conn, 'biofetch', s)
        st['last_error'] = 'Token expired or invalid: switched off'
    elif err and err.get('code') in NOT_FOUND:
        conn.execute('UPDATE people SET bd_at=? WHERE handle=?', (ts, h))
        st['misses'] = st.get('misses', 0) + 1""",
    """    elif err and err.get('code') == 190:
        current_settings = settings(conn)
        if all(current_settings[key] == s[key] for key in ('token', 'ig_user_id')):
            current_settings['on'] = False
            db.set_setting(conn, 'biofetch', current_settings)
            st['last_error'] = 'Token expired or invalid: switched off'
    elif err and err.get('code') in NOT_FOUND:
        if fresh:
            conn.execute('UPDATE people SET bd_at=? WHERE id=?', (ts, row['id']))
            st['misses'] = st.get('misses', 0) + 1
        else:
            st['discarded'] = st.get('discarded', 0) + 1""")
p = root / 'extension/test/core.test.mjs'
s = p.read_text()
old = 'const hit = new Date(2026, 8, 27, 1, 43).getTime();'
assert s.count(old) == 4
p.write_text(s.replace(old, 'const hit = new Date(2026, 8, 26, 1, 43).getTime(); // before the UTC policy cutoff in every timezone'))
replace('server/tests/test_server.py',
    "self.assertEqual(set(m['nodes'][0]), {'id', 'kind', 'label', 'tier', 'score', 'pic', 'degree', 'followers', 'status', 'lists',",
    "self.assertEqual(set(m['nodes'][0]), {'id', 'kind', 'label', 'name', 'tier', 'score', 'pic', 'degree', 'followers', 'status', 'lists',")
