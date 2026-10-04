"""Settings storage ownership; writes remain in the caller transaction."""

import json


# budget: per account per day (list pages, profile reads; profile 0 = no daily number). bio_min: prefilter floor for planned
# bio reads (an explicit read ignores it).
DEFAULTS = {'paused': False, 'budget': {'list': 3000, 'profile': 300}, 'qualify': False, 'qualify_auto': False, 'llm_workers': 8,
            'llm_min': 40, 'bio_min': 25, 'main_list_share': 0}


def get_setting(conn, key, default=None):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    # a copy: callers mutate dicts (budget.update) and must never change the shared defaults
    return json.loads(row[0] if row else json.dumps(DEFAULTS.get(key, default)))


def set_setting(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))
