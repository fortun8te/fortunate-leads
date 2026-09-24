"""User-defined tag rules: "tag people whose <field> matches <keywords or /regex/>".

Rule tags are stored in `tags` with source='rule'. They are applied to everyone when a rule is created and re-synced for
the people touched by every list page / profile ingest (and by the qualify batch), so they never go stale.
"""
import re
import time
from functools import lru_cache

try:  # the stdlib regex parser: exact structure instead of guessing from the pattern text
    from re import _parser as sre_parse  # Python 3.11+
except ImportError:  # pragma: no cover
    import sre_parse  # Python 3.9 / 3.10

FIELDS = ('bio', 'name', 'handle', 'category', 'website', 'any')
GROUPS = ('role', 'niche', 'signal', 'size', 'source')
TEXT_FIELDS = ('bio', 'name', 'category')   # keyword = whole word; handle / website = substring (no spaces in them)
MAX_REGEX = 200          # characters inside /.../
MAX_KEYWORDS = 1000      # characters of the comma-separated keyword list
MAX_WORDS = 100
TEXT_MAX = 160           # longest field value a rule ever scans (IG bios are 150 chars, names 30)
MAX_QUANT = 6            # quantifiers of any kind (? * + {m} {m,n}) per regex
MAX_WIDE = 2             # of which unbounded or wider than {0,3}
PROBE_MS = 5             # worst-case probe strings together must match within this
SCAN_BUDGET = 20         # s: a full scan slower than this is abandoned (nothing is written)
PREVIEW_BUDGET = 5       # s: the live hit count while typing a rule
PROBES = ('a' * TEXT_MAX, ' ' * TEXT_MAX, 'ab' * (TEXT_MAX // 2), '0' * TEXT_MAX, ('founder @brand shop now ' * 8)[:TEXT_MAX],
          ''.join(chr(33 + i % 94) for i in range(TEXT_MAX)))


class TooSlow(ValueError):
    pass


def _check_tree(tree):
    """Reject the shapes that make backtracking blow up: nested/alternating quantified groups, backrefs, too many repeats."""
    count = {'all': 0, 'wide': 0}
    repeat_ops = {sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT} | ({sre_parse.POSSESSIVE_REPEAT} if hasattr(sre_parse, 'POSSESSIVE_REPEAT') else set())

    def has_repeat_or_branch(sub):
        for op, av in sub:
            if op in repeat_ops or op is sre_parse.BRANCH:
                return True
            if any(has_repeat_or_branch(x) for x in _children(op, av)):
                return True
        return False

    def walk(sub):
        for op, av in sub:
            if op in (sre_parse.GROUPREF, getattr(sre_parse, 'GROUPREF_EXISTS', None)):
                raise ValueError('regex backreferences are not allowed')
            if op in repeat_ops:
                lo, hi, body = av
                count['all'] += 1
                count['wide'] += hi > 3            # MAXREPEAT (unbounded) is a huge int
                if hi > 1:
                    if has_repeat_or_branch(body):
                        raise ValueError('regex too expensive: a repeated group may not contain | or another quantifier')
            for child in _children(op, av):
                walk(child)
    walk(tree)
    if count['all'] > MAX_QUANT or count['wide'] > MAX_WIDE:
        raise ValueError(f'regex too expensive: at most {MAX_QUANT} quantifiers, {MAX_WIDE} of them * + or wider than {{0,3}}')


def _children(op, av):
    if op in (sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT) or op is getattr(sre_parse, 'POSSESSIVE_REPEAT', None):
        return [av[2]]
    if op is sre_parse.SUBPATTERN:
        return [av[-1]]
    if op is sre_parse.BRANCH:
        return av[1]
    if op in (sre_parse.ASSERT, sre_parse.ASSERT_NOT):
        return [av[1]]
    if op is getattr(sre_parse, 'ATOMIC_GROUP', None):
        return [av]
    return []


def _probe(*rxs):
    t = time.perf_counter()
    for rx in rxs:
        for s in PROBES:
            rx.search(s)
            if (time.perf_counter() - t) * 1000 > PROBE_MS:
                raise TooSlow('regex too slow on long text')


def _keyword(w):
    return re.escape(w).replace(r'\*', r'\w*').replace(r'\ ', r'\s+')


@lru_cache(maxsize=512)
def compile_match(field, match):
    """-> (word_rx, substring_rx). Raises ValueError with a message fit for a 400."""
    if field not in FIELDS:
        raise ValueError('field must be one of ' + '|'.join(FIELDS))
    if not isinstance(match, str) or not match.strip():
        raise ValueError('match required')
    m = match.strip()
    if len(m) >= 3 and m.startswith('/') and m.endswith('/'):
        pat = m[1:-1]
        if len(pat) > MAX_REGEX:
            raise ValueError(f'regex longer than {MAX_REGEX} characters')
        try:
            _check_tree(sre_parse.parse(pat, re.I))
            rx = re.compile(pat, re.I)
        except (re.error, OverflowError, RecursionError) as e:
            raise ValueError(f'bad regex: {e}') from None
        _probe(rx)
        return rx, rx
    if len(m) > MAX_KEYWORDS:
        raise ValueError(f'keywords longer than {MAX_KEYWORDS} characters')
    words = list(dict.fromkeys(w.strip() for w in m.split(',') if w.strip() and w.strip() != '*'))
    if not words or len(words) > MAX_WORDS:
        raise ValueError(f'1-{MAX_WORDS} comma-separated keywords required')
    if any(w.count('*') > 1 for w in words):
        raise ValueError('one * per keyword')
    alt = '|'.join(_keyword(w) for w in sorted(words, key=len, reverse=True))
    word_rx, sub_rx = re.compile(r'(?<!\w)(?:' + alt + r')(?!\w)', re.I), re.compile(alt, re.I)
    _probe(word_rx, sub_rx)
    return word_rx, sub_rx


def matches(rule, person):
    word_rx, sub_rx = compile_match(rule['field'], rule['match'])
    for f in (FIELDS[:-1] if rule['field'] == 'any' else (rule['field'],)):
        v = str(person[f] or '')[:TEXT_MAX]  # dict or sqlite3.Row
        if v and (word_rx if f in TEXT_FIELDS else sub_rx).search(v):
            return True
    return False


def load(conn):
    out = []
    for r in conn.execute('SELECT id, tag, grp, field, match FROM tag_rules ORDER BY id'):
        try:
            compile_match(r['field'], r['match'])
        except ValueError:  # stored before a limit tightened: skip rather than break ingest
            continue
        out.append(dict(r))
    return out


PERSON_COLS = 'id, handle, name, bio, website, category'


def _chunks(ids, n=900):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def matching_ids(conn, rule, budget=None):
    """Read-only pass over everyone (no write lock held). TooSlow when it takes longer than the budget."""
    budget = SCAN_BUDGET if budget is None else budget
    t, out = time.monotonic(), []
    for i, p in enumerate(conn.execute(f'SELECT {PERSON_COLS} FROM people')):
        if matches(rule, p):
            out.append(p['id'])
        if i % 1000 == 999 and time.monotonic() - t > budget:
            raise TooSlow(f'rule took longer than {budget} s over everyone')
    return out


def write_rule_tags(conn, rule, pids):
    before = conn.total_changes
    conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'rule')", [(p, rule['tag'], rule['grp']) for p in pids])
    return conn.total_changes - before


def apply_rule(conn, rule):
    """Tag everyone the rule matches: scan first, then one short write. Returns the number of tags added."""
    return write_rule_tags(conn, rule, matching_ids(conn, rule))


def sync(conn, pids, rules=None):
    """Make the rule tags of these people exactly what the current rules say (manual/auto tags are never touched)."""
    rules = load(conn) if rules is None else rules
    pids = [p for p in dict.fromkeys(pids) if p is not None]
    if not pids:
        return 0
    changed = 0
    for chunk in _chunks(pids):
        marks = ','.join('?' * len(chunk))
        have = {}
        for t in conn.execute(f"SELECT person_id, tag FROM tags WHERE source='rule' AND person_id IN ({marks})", chunk):
            have.setdefault(t['person_id'], set()).add(t['tag'])
        if not rules and not have:
            continue
        want = {}
        if rules:
            for p in conn.execute(f'SELECT {PERSON_COLS} FROM people WHERE id IN ({marks})', chunk):
                for r in rules:
                    if r['tag'] not in want.get(p['id'], {}) and matches(r, p):
                        want.setdefault(p['id'], {})[r['tag']] = r['grp']
        stale = [(pid, t) for pid, tags in have.items() for t in tags if t not in want.get(pid, {})]
        fresh = [(pid, t, g) for pid, tags in want.items() for t, g in tags.items() if t not in have.get(pid, set())]
        before = conn.total_changes
        conn.executemany("DELETE FROM tags WHERE person_id=? AND tag=? AND source='rule'", stale)
        conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'rule')", fresh)
        changed += conn.total_changes - before
    return changed
