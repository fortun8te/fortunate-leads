"""Shared contracts and pure request helpers; owns no running resources."""
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import db
import owner_relationships
from pathlib import Path

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class AppConfig:
    """Paths and listener settings for one independent workspace."""
    db: str
    port: int = 8777
    root: Path = Path(__file__).resolve().parents[2]
    saved_data_only: bool = False

    @property
    def web(self):
        return self.root / 'web'

    @property
    def is_primary_workspace(self):
        return Path(self.db).resolve() == (self.root / 'data' / 'leads.sqlite').resolve()

    @property
    def host_operations_allowed(self):
        return self.is_primary_workspace and not self.saved_data_only

    def __getitem__(self, key):
        if key in ('db', 'port'):
            return getattr(self, key)
        raise KeyError(key)

    @classmethod
    def from_mapping(cls, values, root=None):
        return cls(str(Path(values['db']).resolve()), int(values['port']),
                   Path(root).resolve() if root else cls.__dataclass_fields__['root'].default)

    def with_port(self, port):
        return replace(self, port=port)


EXT_ORIGIN = 'chrome-extension://fgdbghllamedgihmdcolaggnbhnakjnf'

STATUSES = ('interested', 'contacted', 'talking', 'spoke_before', 'client', 'no')

POSITIVE = ('interested', 'talking', 'client')

POSITIVE_SQL = "('interested','talking','client')"

LEGACY_STATUS = {'good': 'interested'}

def status_in(v):
    return LEGACY_STATUS.get(v, v)

PIC_HOSTS = ('.cdninstagram.com', '.fbcdn.net')

PIC_MAX = 2 * 1024 * 1024

READ_PRIORITY = 10000

LEASE_MIN = 10

PROFILE_MAX_ATTEMPTS = 8

QUALIFY_MAX_ATTEMPTS = 5

PLAN_BATCH = 200

class Bad(Exception):
    pass

class NotFound(Bad):
    pass

class Conflict(Exception):

    def __init__(self, current):
        super().__init__('This record changed. Review the latest note before retrying.')
        self.current = current

def text_or_none(v):
    return v if isinstance(v, str) else None

def utc(s):
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

def iso(d):
    return d.isoformat(timespec='microseconds')

OWNER_HANDLE = owner_relationships.OWNER

def workspace_cooldown(conn, now):
    """A persisted Instagram warning pauses collection across all browser accounts."""
    raw = db.get_setting(conn, 'cooldown')
    value = clean_iso(raw)
    if raw is not None and raw != '' and (value is None):
        raise Bad('Stored safety hold is invalid. Collection remains stopped until it is repaired.')
    until = utc(value) if value else None
    return value if until and until > now else None

FOLLOWING_TRIAL_PAGE_LIMIT = 4

def count_or_none(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, float) and v == v and (abs(v) < 1000000000000.0):
        v = int(v)
    if isinstance(v, str) and re.fullmatch('\\s*\\d[\\d,]*\\s*', v):
        v = int(v.replace(',', ''))
    return v if isinstance(v, int) and 0 <= v < 10 ** 12 else None

def metric_int(value, low, high):
    return value if type(value) is int and low <= value <= high else None

def clean_iso(v):
    try:
        return iso(utc(v)) if isinstance(v, str) and v else None
    except ValueError:
        return None

def clean_rate(r):
    if not isinstance(r, dict):
        return None
    out = {}
    for k in ('pages_hour', 'people_hour'):
        v = r.get(k)
        out[k] = round(float(v), 1) if isinstance(v, (int, float)) and (not isinstance(v, bool)) and (v >= 0) else None
    v = r.get('last_hit_at')
    try:
        out['last_hit_at'] = iso(utc(v)) if isinstance(v, str) and v else None
    except ValueError:
        out['last_hit_at'] = None
    return out

LISTS_READ_THROUGH = '(SELECT count(DISTINCT e.seed) FROM current_edges e WHERE e.person_id=p.id)'

LISTS = f"(CASE WHEN (SELECT value FROM settings WHERE key='map_person_degree_v1')='true' AND (SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_degree_%')=6 THEN coalesce(d.degree,0) ELSE {LISTS_READ_THROUGH} END)"

PEOPLE_FROM = 'FROM people p LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id LEFT JOIN map_person_degree d ON d.person_id=p.id'

LEAD_SQL = f"SELECT p.*, v.tier, v.score, v.content_fit, v.role, v.reason, m.status, m.note, coalesce(m.updated_at, '') AS mark_rev, {LISTS} AS lists {PEOPLE_FROM}"

NOT_ME = "p.handle NOT IN (SELECT handle FROM seeds WHERE is_me=1) AND p.handle!='fortun8te' COLLATE NOCASE"

TAG_ORDER = "CASE t.source WHEN 'manual' THEN 0 WHEN 'rule' THEN 1 ELSE 2 END, CASE t.grp WHEN 'role' THEN 0 WHEN 'niche' THEN 1 WHEN 'signal' THEN 2 WHEN 'size' THEN 3 ELSE 4 END, t.tag"

MAP_TAGS = 4

JUDGE_BAD = {'Too big', 'Other market', 'Scout: No', 'Not reachable', 'Creator', 'Coach', 'Agency', 'Personal', 'SaaS', 'Freelancer'}

JUDGE_GOOD = {'Scout: Strong', 'Scout: Possible', 'AI: Top fit', 'AI: Decision maker', 'Founder', 'Brand', 'Store', 'Shopify', 'Shop Link', 'DTC'}

def judge(tagset):
    return 'bad' if tagset & JUDGE_BAD else 'good' if tagset & JUDGE_GOOD else None

def chunks(ids, n=900):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]

def csv(q, key):
    return [x.strip() for x in q.get(key, [''])[0].split(',') if x.strip()]

def qint(q, key):
    v = q.get(key, [''])[0].strip()
    if not v:
        return None
    try:
        n = int(v)
    except ValueError:
        raise Bad(f'{key} must be a whole number') from None
    if not -2 ** 63 <= n < 2 ** 63:
        raise Bad(f'{key} is too large')
    return n

SORTS = {'follow_up': '(SELECT f.due_on FROM followups f WHERE f.person_id=p.id AND f.completed_at IS NULL) IS NULL, (SELECT f.due_on FROM followups f WHERE f.person_id=p.id AND f.completed_at IS NULL)', 'recent': 'p.updated_at DESC', 'followers': 'p.followers IS NULL, p.followers DESC', 'connected': 'lists DESC, p.followers IS NULL, p.followers DESC', 'fit': "CASE WHEN v.tier='unread' THEN 1 ELSE 0 END, v.content_fit IS NULL, v.content_fit DESC, lists DESC, v.score IS NULL, v.score DESC", 'score': 'v.score IS NULL, v.score DESC, p.followers DESC'}

@contextmanager
def read_snapshot(conn):
    """Keep the revision, totals and related records in one SQLite read snapshot.

    SAVEPOINT also preserves a caller's existing transaction, including test fixtures.
    """
    conn.execute('SAVEPOINT lead_read')
    try:
        yield
    finally:
        conn.execute('RELEASE SAVEPOINT lead_read')

KEEP = object()

def data_rev(conn):
    """Durable revision advances on every edit affecting leads, facets or the map (triggers in db.init)."""
    return db.get_setting(conn, 'lead_data_rev', 0)

SEED_LINKS_TOP = 50

RATE_WINDOW = timedelta(hours=6)

OBSERVED_RATE_WINDOW = timedelta(minutes=1)

SNOWBALL_MAX = 50

class WorkerDelay:

    def __init__(self, seconds):
        self.seconds = max(1, min(60, float(seconds)))
