"""Durable external-review attempts keyed by the full local review identity.

No prompts, notes or responses are stored. A returned answer (including None)
ends automatic attempts for that input. Only an abandoned claim can retry once.
Caller owns transactions; claim uses one atomic conditional UPSERT. Check local
input freshness under the same write transaction before claiming.
"""
import time

LEASE_SECONDS = 180  # exceeds the broad runtime + citation verification deadline
MAX_ATTEMPTS = 2


def ensure(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS external_attempts(
        person_id INTEGER PRIMARY KEY, input_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('running','complete','unverified')),
        attempts INTEGER NOT NULL, claimed_at REAL NOT NULL, lease_until REAL NOT NULL,
        completed_at REAL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS external_attempts_lease ON external_attempts(state,lease_until)')


def eligible_sql(person='p.id', fingerprint='l.input_hash', now=':external_now'):
    """For queries joining people p and CURRENT local_reviews l; bind :external_now.

    Caller must separately exclude local_queue and validate current local input.
    Correlated primary-key lookup avoids scanning historical external attempts.
    """
    return (f'NOT EXISTS (SELECT 1 FROM external_attempts ea WHERE ea.person_id={person} '
            f'AND ea.input_hash={fingerprint} AND (ea.state!=\'running\' '
            f'OR ea.attempts>={MAX_ATTEMPTS} OR ea.lease_until>{now}))')


def eligible(conn, person_id, input_hash, now=None):
    now = time.time() if now is None else now
    row = conn.execute('SELECT input_hash,state,attempts,lease_until FROM external_attempts WHERE person_id=?',
                       (person_id,)).fetchone()
    return (not row or row[0] != input_hash or
            (row[1] == 'running' and row[2] < MAX_ATTEMPTS and row[3] <= now))


def claim(conn, person_id, input_hash, now=None):
    """Commit this successful claim before inference. Returns a claim token or None.

    Completing needs that token to prevent an old, slow worker overwriting the
    retry that took its expired lease. A changed input starts a new attempt count.
    """
    if not isinstance(input_hash, str) or not input_hash:
        raise ValueError('input_hash is required')
    now = time.time() if now is None else now
    result = conn.execute('''INSERT INTO external_attempts
        (person_id,input_hash,state,attempts,claimed_at,lease_until,completed_at)
        VALUES(?,?,'running',1,?,?,NULL)
        ON CONFLICT(person_id) DO UPDATE SET
          input_hash=excluded.input_hash,state='running',
          attempts=CASE WHEN input_hash=excluded.input_hash THEN attempts+1 ELSE 1 END,
          claimed_at=excluded.claimed_at,lease_until=excluded.lease_until,completed_at=NULL
        WHERE input_hash!=excluded.input_hash OR
          (state='running' AND attempts<? AND lease_until<=excluded.claimed_at)''',
        (person_id,input_hash,now,now+LEASE_SECONDS,MAX_ATTEMPTS))
    return now if result.rowcount else None


def complete(conn, person_id, input_hash, verdict_or_none, claim_token=None, now=None):
    """Finish only the matching input and lease; None means unverified, not bad fit.

    Pass the token returned by claim whenever completion can race crash recovery.
    Without a token, only a first attempt can finish (legacy synchronous callers).
    """
    now = time.time() if now is None else now
    guard = 'claimed_at=?' if claim_token is not None else 'attempts=1'
    args = ["complete" if verdict_or_none is not None else "unverified", now, person_id, input_hash]
    if claim_token is not None:
        args.append(claim_token)
    return bool(conn.execute(f'''UPDATE external_attempts SET state=?,completed_at=?
        WHERE person_id=? AND input_hash=? AND state='running' AND {guard}''', args).rowcount)


def retry(conn, person_id, now=None):
    """Explicit owner retry only. Never clear an unexpired running lease."""
    now = time.time() if now is None else now
    return bool(conn.execute("DELETE FROM external_attempts WHERE person_id=? AND (state!='running' OR lease_until<=?)",
                             (person_id,now)).rowcount)
