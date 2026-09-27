"""Logged-in follower/following pagination, one bot account per dedicated sticky egress.

This is the tier that needs accounts, so it is the one that is scarce. Policy:
  * each bot account is bound 1:1 to its own egress (Registry.bind_account); never Tor, never the
    home connection, never shared (two accounts on one IP get linked, one flag hits both);
  * each account has its own Pacer (list_pacer + warm-up budget) and its own cooldowns. A limit on
    one account does NOT stop the others, because they no longer share an IP;
  * challenge / checkpoint = that account is held until a human clears it (no auto-resume);
  * the main account is disabled unless `enabled` AND `allow_main` are both set, then gets the tiny
    main_account_pacer, following lists only, and a hold on the first push-back;
  * the cursor is saved after every page, so nothing is ever fetched twice;
  * every returned user goes straight into the SeenStore -> enrichment pool (logged-out tier).

Live logins are out of scope here: the SessionClient is injected. `MobileSessionClient` shows the
request shape for a session file Michael creates himself on the account's own device/IP.
"""
import random
import sqlite3
import threading
import time

from scale import signals
from scale.pacing import list_pacer, main_account_pacer, warmup_budget

DAY = 86400.0


class Account:
    def __init__(self, id, handle=None, egress_id=None, is_main=False, enabled=True, allow_main=False,
                 created_at=None, daily_budget=3000, directions=('following', 'followers')):
        self.id, self.handle, self.egress_id = str(id), handle, egress_id
        self.is_main, self.allow_main = bool(is_main), bool(allow_main)
        self.enabled = bool(enabled) and (not self.is_main or self.allow_main)
        self.created_at, self.daily_budget = created_at, int(daily_budget)
        self.directions = ('following',) if self.is_main else tuple(directions)
        self.hold = None            # 'challenge' | 'login' | 'main_pushback'

    @classmethod
    def from_dict(cls, d):
        keys = ('id', 'handle', 'egress_id', 'is_main', 'enabled', 'allow_main', 'created_at', 'daily_budget',
                'directions')
        return cls(**{k: d[k] for k in keys if k in d})


# ---- request shapes ------------------------------------------------------------
def web_list_url(user_id, direction, cursor=None, count=None):
    """What the extension sends today (www, same-origin in the logged-in tab)."""
    count = count or (50 if direction == 'following' else 25)
    url = 'https://www.instagram.com/api/v1/friendships/%s/%s/?count=%d' % (user_id, direction, count)
    if cursor:
        url += '&max_id=' + cursor
    if direction == 'followers':
        url += '&search_surface=follow_list_page'
    return url


def mobile_list_url(user_id, direction, rank_token, cursor=None, count=100):
    """Mobile API shape (instagrapi user_followers_v1_chunk). Page sizes of 50-100 are reported for
    this surface vs ~15-25 for web followers; to be confirmed with a soak, not assumed."""
    url = 'https://i.instagram.com/api/v1/friendships/%s/%s/?count=%d&rank_token=%s' % (
        user_id, direction, count, rank_token)
    if direction == 'followers':
        url += '&search_surface=follow_list_page'
    if cursor:
        url += '&max_id=' + cursor
    return url


class MobileSessionClient:
    """Adapter: (account, egress, transport, session headers) -> page JSON. The session headers
    (Authorization: Bearer IGT:2:..., X-IG-Device-ID, etc.) come from a session file the owner
    created on that account's own device/IP; this code never logs in."""

    def __init__(self, transport, egress, session_headers, rank_token):
        self.transport, self.egress, self.headers, self.rank_token = transport, egress, session_headers, rank_token

    def fetch(self, user_id, direction, cursor):
        r = self.transport.send(self.egress, 'GET', mobile_list_url(user_id, direction, self.rank_token, cursor),
                                self.headers, timeout=25)
        return r


# ---- cursor persistence -----------------------------------------------------------
CURSOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS list_work(
  seed TEXT NOT NULL COLLATE NOCASE, user_id TEXT, direction TEXT NOT NULL,
  cursor TEXT, pages INTEGER NOT NULL DEFAULT 0, users INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'queued',        -- queued | running | done | limited | error | private
  account TEXT, priority INTEGER NOT NULL DEFAULT 0, updated_at REAL,
  PRIMARY KEY(seed, direction));
"""


class ListQueue:
    def __init__(self, conn=None, path=':memory:'):
        self.conn = conn or sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(CURSOR_SCHEMA)
        self.lock = threading.Lock()

    def add(self, seed, user_id, direction, priority=0):
        with self.lock:
            self.conn.execute('INSERT OR IGNORE INTO list_work(seed,user_id,direction,priority,updated_at) '
                              'VALUES(?,?,?,?,?)', (seed.lower(), user_id, direction, priority, time.time()))

    def next_for(self, account):
        """The account's own running list first (sticky), then the best queued one it may take."""
        dirs = tuple(account.directions)
        marks = ','.join('?' * len(dirs))
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM list_work WHERE state='running' AND account=? AND direction IN (%s) "
                "ORDER BY updated_at LIMIT 1" % marks, (account.id,) + dirs).fetchone()
            if row is None:
                # following before followers (smaller, less limited), then priority
                row = self.conn.execute(
                    "SELECT * FROM list_work WHERE state='queued' AND direction IN (%s) "
                    "ORDER BY direction='followers', priority DESC, updated_at LIMIT 1" % marks, dirs).fetchone()
                if row is None:
                    return None
                self.conn.execute("UPDATE list_work SET state='running', account=?, updated_at=? "
                                  "WHERE seed=? AND direction=?", (account.id, time.time(), row['seed'], row['direction']))
            return dict(row)

    def save_page(self, seed, direction, next_cursor, n_users, done, limited=False):
        state = 'limited' if limited else 'done' if done else 'running'
        with self.lock:
            self.conn.execute('UPDATE list_work SET cursor=?, pages=pages+1, users=users+?, state=?, updated_at=? '
                              'WHERE seed=? AND direction=?',
                              (next_cursor, n_users, state, time.time(), seed.lower(), direction))

    def release(self, seed, direction):
        """Account stopped (cooldown/hold): list goes back, cursor kept, any healthy account resumes it."""
        with self.lock:
            self.conn.execute("UPDATE list_work SET state='queued', account=NULL WHERE seed=? AND direction=? "
                              "AND state='running'", (seed.lower(), direction))

    def finish(self, seed, direction, state):
        with self.lock:
            self.conn.execute('UPDATE list_work SET state=?, updated_at=? WHERE seed=? AND direction=?',
                              (state, time.time(), seed.lower(), direction))


# ---- scheduler ------------------------------------------------------------------------
class AccountScheduler:
    def __init__(self, accounts, registry, queue, store, clients, metrics=None, rng=None, clock=time.time):
        """clients: account id -> object with fetch(user_id, direction, cursor) -> Response."""
        self.rng, self.clock = rng or random.Random(), clock
        self.registry, self.queue, self.store, self.clients, self.metrics = registry, queue, store, clients, metrics
        self.accounts = {}
        self.pacers = {}
        for a in accounts:
            if not a.enabled:
                continue          # main account off by default; disabled accounts never scheduled
            e = registry.for_account(a.id) or (registry.bind_account(a.id, a.egress_id) if a.egress_id else None)
            if e is None:
                raise ValueError('account %s has no dedicated egress; refusing to schedule it' % a.id)
            self.accounts[a.id] = a
            if a.is_main:
                self.pacers[a.id] = main_account_pacer(rng=self.rng)
            else:
                budget = a.daily_budget
                if a.created_at:
                    budget = warmup_budget((clock() - a.created_at) / DAY, target=a.daily_budget)
                self.pacers[a.id] = list_pacer(daily_budget=budget or 1, rng=self.rng)
                if budget == 0:
                    a.hold = 'warming_up'

    def _m(self, ident, name):
        if self.metrics:
            self.metrics.inc('account', ident, name, now=self.clock())

    def ready(self):
        now = self.clock()
        out = []
        for a in self.accounts.values():
            if a.hold:
                continue
            e = self.registry.for_account(a.id)
            if not self.registry.ready_for_work(e):
                continue            # exit not verified as non-home: fail closed
            if self.pacers[a.id].ready_at(now) <= now:
                out.append(a)
        return out

    def step(self, account):
        now = self.clock()
        pacer = self.pacers[account.id]
        job = self.queue.next_for(account)
        if job is None:
            return 'idle'
        if not pacer.try_acquire(now):
            return 'wait'
        self._m(account.id, 'requests')
        try:
            r = self.clients[account.id].fetch(job['user_id'], job['direction'], job['cursor'])
            code, reason = signals.classify(r.status, r.text, r.url, kind='list')
        except OSError:
            code, reason, r = signals.NETWORK, 'transport', None
        if code == signals.OK:
            j = signals.parse_body(r.text) or {}
            users = signals.users_of(j) or []
            mapped = [{'handle': u.get('username'), 'ig_id': str(u.get('pk') or u.get('pk_id') or u.get('id') or '') or None,
                       'is_private': u.get('is_private'), 'is_verified': u.get('is_verified')}
                      for u in users if isinstance(u, dict) and u.get('username')]
            limited = bool(j.get('should_limit_list_of_followers'))
            nxt = j.get('next_max_id')
            nxt = str(nxt) if nxt not in (None, '') and not limited else None
            done = limited or not nxt or j.get('has_more') is False or not mapped
            if nxt and nxt == job['cursor']:
                done = True    # repeated cursor: keep what came back, mark partial
            new = self.store.add_discovered(mapped, source='%s/%s' % (job['seed'], job['direction']), now=now)
            self.queue.save_page(job['seed'], job['direction'], None if done else nxt, len(mapped), done, limited)
            pacer.success(now)
            self._m(account.id, 'pages')
            self._m(account.id, 'users')
            if self.metrics:
                self.metrics.inc('pipeline', 'lists', 'users_seen', now=now, n=len(mapped))
                self.metrics.inc('pipeline', 'lists', 'users_new', now=now, n=len(new))
            return 'page'
        if code == signals.CHALLENGE:
            account.hold = 'challenge'
            self.queue.release(job['seed'], job['direction'])
            self._m(account.id, 'challenge')
            return 'hold'
        if code in signals.PUSHBACK:
            retry = None
            if r is not None and str(r.header('retry-after', '')).isdigit():
                retry = now + int(r.header('retry-after'))
            pacer.pushback(now, retry_at=retry)
            self.queue.release(job['seed'], job['direction'])     # another healthy account continues it
            self._m(account.id, 'pushback_' + code)
            if account.is_main:
                account.hold = 'main_pushback'                  # never push the main account twice
            if code == signals.LOGIN_WALL:
                account.hold = 'login'
            return 'cooldown'
        if code == signals.PRIVATE:
            self.queue.finish(job['seed'], job['direction'], 'private')
            return 'private'
        if code == signals.NOT_FOUND:
            self.queue.finish(job['seed'], job['direction'], 'error')
            return 'not_found'
        pacer.soft_error(now, delay=120 if code != signals.NETWORK else 30)
        self._m(account.id, 'error_' + str(reason)[:30])
        return 'error'

    def snapshot(self):
        now = self.clock()
        return {aid: dict(self.pacers[aid].snapshot(now), hold=a.hold,
                          egress=getattr(self.registry.for_account(aid), 'id', None), is_main=a.is_main)
                for aid, a in self.accounts.items()}
