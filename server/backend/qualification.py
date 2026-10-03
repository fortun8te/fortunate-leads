"""Qualification orchestration with application-owned worker resources."""

from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sys
import threading
import time
import traceback

import control
import db
import deepscout
import engine_controls
import external_harness
import external_queue
import laya
import local_model
import local_qualification
import owner
import owner_notes
import processing_modes
import processing_progress
import processing_state
import qualify
import resource_budget
import rules
import websearch

from .executor import DaemonExecutor
from .common import NOT_ME, POSITIVE, POSITIVE_SQL, QUALIFY_MAX_ATTEMPTS, WorkerDelay, iso
from .evidence import edges_of, me_handle, network_context, with_owner

LAYA_BATCH = 64
LAYA_REBUILD_BATCH = 5000
FEWSHOT_MAX = 8
FEWSHOT_CHANGE = 5
FEWSHOT_RERUN = 35
FEWSHOT_TAG_MAX = getattr(qualify, 'FEWSHOT_TAG_MAX', 6)

def external_candidates_sql():
    return (f"FROM local_reviews l JOIN people p ON p.id=l.person_id JOIN verdicts v ON v.person_id=p.id WHERE {NOT_ME} AND coalesce(p.bio,'')!='' "
            "AND l.status='needs_research' AND v.model LIKE 'local:%' AND v.updated_at=p.updated_at "
            "AND NOT EXISTS(SELECT 1 FROM local_queue q WHERE q.person_id=p.id) "
            "AND NOT EXISTS(SELECT 1 FROM marks m LEFT JOIN owner_note_reads n ON n.person_id=m.person_id "
            "WHERE m.person_id=p.id AND trim(coalesce(m.note,''))!='' AND coalesce(n.state,'pending')!='ready') "
            f"AND {external_queue.eligible_sql(now='?')} AND (coalesce(v.prefilter,0)+coalesce(v.score,0))/2>=? ")


def laya_hash(*fields):
    """Invalidate when any field supplied to Laya changes."""
    payload = [laya.cache_signature(), fields]
    return 'profile:' + hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:24]


def _expire(skip):
    t = datetime.now().timestamp()
    for k in [k for k, until in list(skip.items()) if until <= t]:
        skip.pop(k, None)


class QualificationService:
    """Own the scoring policy and pools for one configured database."""

    def __init__(self, config, algorithms=qualify):
        self.config = config
        self.algorithms = algorithms
        self._resource_lock = threading.RLock()
        self._research_pool = None
        self._research_slots = threading.BoundedSemaphore(max(1, websearch.PARALLEL))
        self._llm_pool = None
        self._closed = False

    @property
    def fewshot_tag_max(self):
        return getattr(self.algorithms, 'FEWSHOT_TAG_MAX', 6)

    @property
    def research_pool(self):
        with self._resource_lock:
            if self._closed:
                raise RuntimeError('Qualification service is closed')
            if self._research_pool is None:
                self._research_pool = DaemonExecutor(
                    max_workers=max(1, websearch.PARALLEL), thread_name_prefix='research')
            return self._research_pool

    @property
    def llm_pool(self):
        with self._resource_lock:
            if self._closed:
                raise RuntimeError('Qualification service is closed')
            if self._llm_pool is None:
                self._llm_pool = LLMPool(self)
            return self._llm_pool

    def external_active(self):
        with self._resource_lock:
            pool = self._llm_pool
        return pool is not None and not pool.idle()

    def shutdown(self, wait=True):
        with self._resource_lock:
            self._closed = True
            llm_pool, research_pool = self._llm_pool, self._research_pool
        if llm_pool is not None:
            llm_pool.shutdown(wait=wait)
        if research_pool is not None:
            research_pool.shutdown(wait=wait, cancel_futures=True)

    def _current(self, conn, ticket):
        return not self._closed and processing_modes.result_current(conn, ticket)

    def _commit_current(self, conn, ticket):
        with self._resource_lock:
            if not self._current(conn, ticket):
                conn.rollback()
                return False
            conn.commit()
            return True

    def _finish_claims(self, conn, items):
        """A completed provider attempt stays terminal even when its result is discarded."""
        if conn.in_transaction:
            conn.rollback()
        conn.execute('BEGIN IMMEDIATE')
        for item in items:
            review = item.get('local_review')
            if review:
                external_queue.complete(conn, item['person']['id'], review['input_hash'], None,
                                        claim_token=item['claim_token'])
        conn.commit()

    def _research_map(self, people):
        """Share a fixed number of submissions across concurrent model batches."""
        pending = deque()
        try:
            for person in people:
                if len(pending) >= max(1, websearch.PARALLEL):
                    yield pending.popleft().result()
                self._research_slots.acquire()
                try:
                    with self._resource_lock:
                        future = self.research_pool.submit(self.safe_research_lookup, person)
                except BaseException:
                    self._research_slots.release()
                    raise
                future.add_done_callback(lambda _: self._research_slots.release())
                pending.append(future)
            while pending:
                yield pending.popleft().result()
        finally:
            for future in pending:
                future.cancel()

    def refresh_network(self, conn, pids, prior=None, me=None, nets=None):
        """Refresh connection claims and reblend already known fit without classifying anyone.

        Content hashes allow queued changes to reblend without a prior snapshot.
        `prior` can validate older graph-dependent verdict hashes before migration.
        Changed content and unknown content fit remain for an enabled qualification pass.
        """
        pids = list(dict.fromkeys(pids))
        if not pids:
            return 0
        me = me if me is not None else me_handle(conn)
        if nets is None or not all(pid in nets for pid in pids):
            nets = network_context(conn, pids, me)
        changed = 0
        for pid in pids:
            row = conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
            if not row:
                continue
            p = with_owner(conn, dict(row))
            edges = edges_of(conn, pid)
            # Automatic source tags describe observed connections. Manual knowledge
            # is independent evidence and is never removed by a list refresh.
            source = [(tag, grp) for tag, grp in self.algorithms.rule_tags(p, edges, me) if grp == 'source']
            old_source = {(r['tag'], r['grp']) for r in conn.execute(
                "SELECT tag, grp FROM tags WHERE person_id=? AND source='auto' AND grp='source'", (pid,))}
            if old_source != set(source):
                conn.execute("DELETE FROM tags WHERE person_id=? AND source='auto' AND grp='source'", (pid,))
                conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')", [(pid, tag, grp) for tag, grp in source])
                changed += 1
            old = conn.execute('SELECT * FROM verdicts WHERE person_id=?', (pid,)).fetchone()
            before = (prior or {}).get(pid)
            if not old:
                continue
            if old['content_fit'] is None:
                if old['score'] is not None:
                    conn.execute("UPDATE verdicts SET score=NULL,tier='unread',prefilter=NULL WHERE person_id=?", (pid,))
                    changed += 1
                continue
            hashed = self.algorithms.input_hash(p, edges, nets[pid])
            valid = old['input_hash'] == hashed
            if not valid and before:
                previous = before['person']
                same_content = self.algorithms.input_hash(previous, before['edges'], before['net']) == hashed
                valid = same_content and old['updated_at'] == previous['updated_at'] and (
                    old['model'] == 'rules' or old['input_hash'] == self.algorithms.legacy_input_hash(
                        previous, before['edges'], before['net']))
            if not valid:
                continue
            score = owner.owner_recommendation(p, {'score': self.algorithms.blend(old['content_fit'], nets[pid])})['score']
            _, lfit = self.laya_row(conn, pid)
            pre = self.algorithms.prefilter(p, sorted({e['seed'] for e in edges}), nets[pid], lfit)
            if (score, pre, hashed, p['updated_at']) != (old['score'], old['prefilter'], old['input_hash'], old['updated_at']):
                conn.execute('UPDATE verdicts SET score=?, tier=?, prefilter=?, input_hash=?, updated_at=? WHERE person_id=?',
                             (score, self.algorithms._tier(score, bool((p.get('bio') or '').strip())), pre, hashed, p['updated_at'], pid))
                changed += 1
        return changed


    def drain_network_dirty(self, conn, limit=200):
        """Acknowledge only the exact queued revision that was refreshed."""
        rows = conn.execute('SELECT person_id,change_id FROM network_dirty ORDER BY change_id,person_id LIMIT ?',
                            (limit,)).fetchall()
        if not rows:
            return 0
        self.refresh_network(conn, [r['person_id'] for r in rows])
        conn.executemany('DELETE FROM network_dirty WHERE person_id=? AND change_id=?',
                         [(r['person_id'], r['change_id']) for r in rows])
        conn.commit()
        return len(rows)


    def laya_row(self, conn, pid):
        if not processing_modes.allows(conn, 'laya'):
            return None, None
        r = conn.execute('''SELECT l.answers, l.fit, l.input_hash, p.handle, p.name, p.bio,
                           p.category, p.website, p.followers
                           FROM laya l JOIN people p ON p.id=l.person_id WHERE l.person_id=?''', (pid,)).fetchone()
        if not r:
            return None, None
        fields = (r[k] for k in ('handle', 'name', 'bio', 'category', 'website', 'followers'))
        if r['input_hash'] != laya_hash(*fields):
            return None, None
        try:
            answers = json.loads(r['answers'] or '{}')
            if not laya.valid_answers(answers):
                return None, None
            return answers, laya.fit(answers)
        except (TypeError, ValueError):
            return None, None


    def requalify(self, conn, p, me, net=None):
        # Same rule as background_qualify: rule scoring is free and only Stop all holds it.
        if not db.get_setting(conn, 'local_laya') and all(control.stage_paused(conn, s) for s in ('lists', 'bios', 'ai')):
            return self.refresh_network(conn, [p['id']], me=me)
        with_owner(conn, p)
        # Older auto tags treated any follow or @mention as a personal acquaintance.
        conn.execute("DELETE FROM tags WHERE person_id=? AND tag='knows you' AND source='auto'", (p['id'],))
        edges = edges_of(conn, p['id'])
        auto = self.algorithms.rule_tags(p, edges, me)
        retained = [tuple(r) for r in conn.execute("SELECT tag,grp FROM tags WHERE person_id=? AND source!='auto'", (p['id'],))]
        rule_result = self.algorithms.rule_verdict(p, retained + auto, net)
        rule_result['input_hash'] = self.algorithms.input_hash(p, edges, net)
        rule_pre = self.algorithms.prefilter(p, sorted({e['seed'] for e in edges}), net)
        processing_state.save_rules(conn, p, rule_pre, rule_result, auto)
        _, lfit = self.laya_row(conn, p['id'])
        pre = self.algorithms.prefilter(p, sorted({e['seed'] for e in edges}), net, lfit)
        old = conn.execute('SELECT model, input_hash FROM verdicts WHERE person_id=?', (p['id'],)).fetchone()
        scout_current = bool(old and old['model'] == 'leadscout' and deepscout.fresh(conn, p))
        allowed_old = old and (old['model'] == 'rules' or
                (old['model'] or '').startswith('local:') and processing_modes.allows(conn, 'local_qualification') or
                              processing_modes.allows(conn, 'external'))
        keep_llm = allowed_old and old['input_hash'] and old['input_hash'] == self.algorithms.input_hash(p, edges, net) and (
            old['model'] != 'leadscout' or scout_current)
        if not keep_llm:  # the LLM's extra auto tags stay as long as its verdict does
            conn.execute("DELETE FROM tags WHERE person_id=? AND source='auto'", (p['id'],))
        auto = self.algorithms.rule_tags(p, edges, me)
        # Laya probabilities are uncalibrated and have no separate tag provenance.
        # Use the fit as a ranking hint only; rule and LLM tags keep their own evidence.
        conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')", [(p['id'], t, g) for t, g in auto])
        if processing_modes.allows(conn, 'external'):
            deepscout.retag(conn, p['id'])
        if keep_llm:
            self.refresh_network(conn, [p['id']], me=me, nets={p['id']: net} if net is not None else None)
            # The verdict is current for this profile revision even when the reblend changed nothing;
            # without this the batch would pick the same person up again on every pass.
            conn.execute('UPDATE verdicts SET updated_at=? WHERE person_id=? AND updated_at<?',
                         (p['updated_at'], p['id'], p['updated_at']))
            return
        v = rule_result
        conn.execute("INSERT OR REPLACE INTO verdicts(person_id, prefilter, score, tier, role, reason, model, input_hash, updated_at, content_fit) "
                     "VALUES(?,?,?,?,?,?,'rules',?,?,?)", (p['id'], pre, v['score'], v['tier'], v['role'], v['reason'], self.algorithms.input_hash(p, edges, net), p['updated_at'], v['content_fit']))
        if scout_current and processing_modes.allows(conn, 'external'):
            deepscout.reapply(conn, p, net)


    def qualify_batch(self, conn, limit=32):
        """Keep each rule write transaction short enough for concurrent extension and Laya writers."""
        rows = conn.execute('SELECT p.* FROM people p LEFT JOIN verdicts v ON v.person_id=p.id '
                            "WHERE v.person_id IS NULL OR (coalesce(v.model,'')!='error' AND v.updated_at < p.updated_at) "
                            "OR (v.model='error' AND (coalesce(v.input_hash,'')!=p.updated_at "
                            "OR (v.reason LIKE 'retry %' AND v.updated_at<=?))) LIMIT ?",
                            (db.now(), limit)).fetchall()
        me = me_handle(conn)
        # Reserve the writer before rule reads and per-person savepoints. A scraper
        # commit between a savepoint's first read and its first write otherwise
        # makes SQLite reject the upgrade with SQLITE_BUSY_SNAPSHOT.
        if rows and not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        # One bulk rule sync is much cheaper than querying rules and tags for every
        # person. If it fails, roll back the whole sync and isolate the bad person
        # below so the rest of the batch can still make progress.
        rule_sync_failed = False
        if rows:
            conn.execute('SAVEPOINT qualify_rules')
            try:
                rules.sync(conn, [r['id'] for r in rows])
                conn.execute('RELEASE SAVEPOINT qualify_rules')
            except Exception:
                traceback.print_exc()
                conn.execute('ROLLBACK TO SAVEPOINT qualify_rules')
                conn.execute('RELEASE SAVEPOINT qualify_rules')
                rule_sync_failed = True
        nets = network_context(conn, [r['id'] for r in rows], me) if rows else {}
        for r in rows:
            conn.execute('SAVEPOINT qualify_person')
            try:
                if rule_sync_failed:
                    rules.sync(conn, [r['id']])
                self.requalify(conn, dict(r), me, nets.get(r['id']))
                conn.execute('RELEASE SAVEPOINT qualify_person')
            except Exception:
                traceback.print_exc()
                conn.execute('ROLLBACK TO SAVEPOINT qualify_person')
                conn.execute('RELEASE SAVEPOINT qualify_person')
                previous = conn.execute('SELECT model,input_hash,reason FROM verdicts WHERE person_id=?', (r['id'],)).fetchone()
                attempt = 1
                if previous and previous['model'] == 'error' and previous['input_hash'] == r['updated_at']:
                    match = re.fullmatch(r'(?:retry|failed) (\d+)/\d+', previous['reason'] or '')
                    if match:
                        attempt = int(match[1]) + 1
                final = attempt >= QUALIFY_MAX_ATTEMPTS
                delay = min(10 * 3 ** (attempt - 1), 900)
                next_at = r['updated_at'] if final else iso(datetime.now(timezone.utc) + timedelta(seconds=delay))
                conn.execute("INSERT OR REPLACE INTO verdicts(person_id,tier,model,input_hash,reason,updated_at) "
                             "VALUES(?,'unread','error',?,?,?)",
                             (r['id'], r['updated_at'], f"{'failed' if final else 'retry'} {attempt}/{QUALIFY_MAX_ATTEMPTS}", next_at))
        conn.commit()
        return len(rows)


    def rebuild_laya_queue(self, conn, signature):
        """Reconcile one ID range and publish the queue only after the final range.

        Profile and Laya triggers maintain ranges already visited. A durable cursor
        lets a restart resume without holding the SQLite write lock for a full scan.
        """
        conn.execute('BEGIN IMMEDIATE')
        try:
            if db.get_setting(conn, 'laya_queue_signature') == signature:
                conn.commit()
                return True
            state = db.get_setting(conn, 'laya_queue_rebuild')
            cursor = state['cursor'] if isinstance(state, dict) and state.get('signature') == signature else None
            if cursor is None:
                ids = [r[0] for r in conn.execute('SELECT id FROM people ORDER BY id LIMIT ?', (LAYA_REBUILD_BATCH,))]
            else:
                ids = [r[0] for r in conn.execute('SELECT id FROM people WHERE id>? ORDER BY id LIMIT ?',
                                                   (cursor, LAYA_REBUILD_BATCH))]
            if ids:
                end = ids[-1]
                bound = ('person_id<=?' if cursor is None else 'person_id>? AND person_id<=?')
                args = (end,) if cursor is None else (cursor, end)
                conn.execute('DELETE FROM laya_queue WHERE ' + bound, args)
                person_bound = ('p.id<=?' if cursor is None else 'p.id>? AND p.id<=?')
                conn.execute(f"""INSERT INTO laya_queue(person_id,bio_blank,prefilter)
                    SELECT p.id,coalesce(p.bio,'')='',v.prefilter
                    FROM people p LEFT JOIN laya l ON l.person_id=p.id
                    LEFT JOIN verdicts v ON v.person_id=p.id
                    WHERE {person_bound} AND instr(p.handle,'~')=0 AND p.handle!='fortun8te' COLLATE NOCASE AND NOT EXISTS
                      (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
                      AND (l.person_id IS NULL OR l.input_hash IS NOT
                        laya_hash(p.handle,p.name,p.bio,p.category,p.website,p.followers))""", args)
                cursor = end
            if len(ids) < LAYA_REBUILD_BATCH:
                db.set_setting(conn, 'laya_queue_signature', signature)
                conn.execute("DELETE FROM settings WHERE key='laya_queue_rebuild'")
                complete = True
            else:
                db.set_setting(conn, 'laya_queue_rebuild', {'signature': signature, 'cursor': cursor})
                complete = False
            conn.commit()
            return complete
        except Exception:
            conn.rollback()
            raise


    def laya_allowed(self, conn):
        return not self._closed and processing_modes.begin_work(conn, 'laya') is not None


    def apply_local_review(self, conn, person, result, net, edges, note_context):
        verdict = local_qualification.reblend(result, person, net, note_context)
        if not verdict:
            return False
        old = conn.execute('SELECT model,input_hash,updated_at FROM verdicts WHERE person_id=?', (person['id'],)).fetchone()
        if (old and old['model'] not in ('rules', 'error') and not old['model'].startswith('local:')
                and processing_modes.allows(conn, 'external') and old['updated_at'] == person['updated_at']
                and old['input_hash'] == self.algorithms.input_hash(person, edges, net)):
            return False  # A current external review remains authoritative in RLEAI.
        processing_state.archive_verdict(conn, person['id'])
        _, lfit = self.laya_row(conn, person['id'])
        pre = self.algorithms.prefilter(person, sorted({e['seed'] for e in edges}), net, lfit)
        conn.execute('''INSERT OR REPLACE INTO verdicts
            (person_id,prefilter,score,tier,role,reason,model,input_hash,updated_at,prompt,evidence,content_fit)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (person['id'],pre,verdict['score'],verdict['tier'],
            verdict['role'],verdict['reason'],'local:'+local_model.MODEL,
            self.algorithms.input_hash(person,edges,net),person['updated_at'],result['prompt'],
            json.dumps(verdict.get('evidence') or []),verdict['content_fit']))
        conn.execute("DELETE FROM tags WHERE person_id=? AND source='auto'", (person['id'],))
        tags = self.algorithms.rule_tags(person,edges,me_handle(conn)) + (verdict.get('tags') or [])
        conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')", [(person['id'],t,g) for t,g in tags])
        return True


    def local_processing_step(self, conn):
        if self._closed:
            return False
        ticket = processing_modes.begin_work(conn, 'local_qualification')
        with engine_controls.work(conn, ticket) as admitted:
            if not admitted:
                return False
            return self._local_processing_step(conn)


    def _local_processing_step(self, conn):
        """One local call at a time; edits coalesce and notes get the first slot."""
        if self._closed or processing_modes.begin_work(conn, 'local_qualification') is None:
            return False
        if owner_notes.step(conn, allowed=lambda: not self._closed, result_guard=self._resource_lock):
            return True
        if self._closed:
            return False
        ticket = processing_modes.begin_work(conn, 'local_qualification')
        jobs = processing_state.next_pending(conn)
        if not jobs or ticket is None:
            return False
        job = jobs[0]
        row = conn.execute(f'SELECT * FROM people p WHERE id=? AND {NOT_ME}', (job['person_id'],)).fetchone()
        if not row:
            conn.execute('DELETE FROM local_queue WHERE person_id=? AND revision=?', (job['person_id'], job['revision']))
            conn.commit()
            return True
        person = with_owner(conn, dict(row))
        edges = edges_of(conn, person['id'])
        net = network_context(conn, [person['id']]).get(person['id'], {})
        context = owner_notes.local_context(conn, person['id'])
        tags = self.algorithms.rule_tags(person, edges, me_handle(conn)) + [tuple(r) for r in conn.execute(
            "SELECT tag,grp FROM tags WHERE person_id=? AND source!='auto'", (person['id'],))]
        cached = conn.execute('SELECT * FROM local_reviews WHERE person_id=?', (person['id'],)).fetchone()
        result = dict(cached) if cached else None
        if result:
            result['verdict'] = json.loads(result['verdict'] or 'null')
        needs_inference = bool(job['repair_attempt']) or not result or not local_qualification.current_result(result, person, context)
        if needs_inference:
            processing_progress.active(conn, person['id'])
        conn.commit()
        started = time.monotonic()
        try:
            if needs_inference:
                result = local_qualification.evaluate(person, tags, edges, net, note_context=context, repair=bool(job['repair_attempt']))
        except (local_model.Busy, local_model.Unavailable) as exc:
            if self._closed:
                return False
            delay = exc.retry_after if isinstance(exc, local_model.Busy) else 60
            processing_state.retry(conn, person['id'], job['revision'], str(exc), delay)
            processing_progress.active(conn)
            if not self._commit_current(conn, ticket):
                return False
            return WorkerDelay(delay)
        except ValueError as exc:
            result = local_qualification.failure_result(person, context, exc)
        if self._closed:
            return False
        conn.execute('BEGIN IMMEDIATE')
        if needs_inference:
            processing_progress.record(conn, person['id'], result, time.monotonic()-started, bool(job['repair_attempt']))
        latest = conn.execute('SELECT * FROM people WHERE id=?', (person['id'],)).fetchone()
        if not latest or not self._current(conn, ticket):
            conn.rollback()
            return False
        current = with_owner(conn, dict(latest))
        context = owner_notes.local_context(conn, person['id'])
        if not local_qualification.current_result(result, current, context):
            conn.rollback()
            return False
        if needs_inference and not job['repair_attempt'] and local_qualification.is_repairable(result):
            if processing_state.request_repair(conn, person['id'], job['revision'], result.get('error', 'unverified')):
                return self._commit_current(conn, ticket)
        if processing_state.put_review(conn, person['id'], result, revision=job['revision'],
                                        private_context_hash=(context or {}).get('snapshot')):
            edges = edges_of(conn, person['id'])
            net = network_context(conn, [person['id']]).get(person['id'], {})
            self.apply_local_review(conn, current, result, net, edges, context)
            verdict = conn.execute('SELECT score FROM verdicts WHERE person_id=?', (person['id'],)).fetchone()
            processing_state.record_review_event(conn, person['id'], local_model.MODEL, result['status'],
                                                score=verdict[0] if verdict else None, reason=result.get('error') or result.get('escalation_reason') or '')
        return self._commit_current(conn, ticket)


    def processing_maintenance(self, conn):
        """Bounded startup and mode-change work, independent of model availability."""
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        seeded = processing_state.seed_step(conn, policy=local_qualification.PROMPT_VERSION + ':' + local_model.MODEL_DIGEST)
        seeded += processing_state.refresh_rank_step(conn)
        queued = [r[0] for r in conn.execute('SELECT person_id FROM processing_rule_queue ORDER BY person_id LIMIT 32')]
        if not queued and not db.get_setting(conn, 'processing_refresh_complete', True):
            cursor = db.get_setting(conn, 'processing_refresh_cursor', 0)
            queued = [r[0] for r in conn.execute('SELECT id FROM people WHERE id>? ORDER BY id LIMIT 64', (cursor,))]
            if queued:
                db.set_setting(conn, 'processing_refresh_cursor', queued[-1])
            else:
                db.set_setting(conn, 'processing_refresh_complete', True)
        if queued:
            me = me_handle(conn)
            nets = network_context(conn, queued, me)
            for pid in queued:
                row = conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
                if row:
                    self.requalify(conn, dict(row), me, nets.get(pid))
                    if processing_modes.allows(conn, 'local_qualification'):
                        processing_state.enqueue(conn, pid)
                else:
                    conn.execute('DELETE FROM processing_rule_queue WHERE person_id=?', (pid,))
        conn.commit()
        return bool(seeded or queued)


    def laya_step(self, conn):
        """Score people without a (current) Laya answer; bios first, list-only people too. Silently idle when the sidecar is down."""
        if laya.runtime_status().get('busy') or not self.laya_allowed(conn) or not laya.available():
            return False
        caller_transaction = conn.in_transaction
        ticket = processing_modes.begin_work(conn, 'laya')
        conn.create_function('laya_hash', 6, laya_hash, deterministic=True)
        signature = laya.cache_signature()
        if not caller_transaction and db.get_setting(conn, 'laya_queue_signature') != signature:
            if not self.rebuild_laya_queue(conn, signature):
                return True  # next worker pass continues the bounded rebuild
        if caller_transaction:
            # A caller's uncommitted profile edits must be visible, but its transaction
            # must not be committed or held behind a queue rebuild during a model call.
            rows = conn.execute(f"""SELECT p.id,p.handle,p.name,p.bio,p.category,p.website,p.followers,l.input_hash AS lh
                FROM people p LEFT JOIN laya l ON l.person_id=p.id LEFT JOIN verdicts v ON v.person_id=p.id
                WHERE instr(p.handle,'~')=0 AND {NOT_ME}
                  AND (l.person_id IS NULL OR l.input_hash IS NOT
                    laya_hash(p.handle,p.name,p.bio,p.category,p.website,p.followers))
                ORDER BY coalesce(p.bio,'')='',v.prefilter DESC,p.id LIMIT ?""", (LAYA_BATCH,)).fetchall()
        else:
            rows = conn.execute(f"""SELECT p.id,p.handle,p.name,p.bio,p.category,p.website,p.followers,l.input_hash AS lh
                FROM laya_queue q JOIN people p ON p.id=q.person_id
                LEFT JOIN laya l ON l.person_id=p.id
                WHERE {NOT_ME}
                ORDER BY q.bio_blank,q.prefilter DESC,q.person_id LIMIT ?""", (LAYA_BATCH,)).fetchall()
        # A profile can change back to an already cached hash. Drop that one queue
        # entry rather than sending the same profile to the sidecar again.
        current = [r['id'] for r in rows if r['lh'] == laya_hash(*(r[k] for k in
                   ('handle','name','bio','category','website','followers')))]
        if current and not caller_transaction:
            conn.executemany('DELETE FROM laya_queue WHERE person_id=? AND EXISTS '
                             '(SELECT 1 FROM people p JOIN laya l ON l.person_id=p.id '
                             'WHERE p.id=laya_queue.person_id AND l.input_hash=? AND '
                             'l.input_hash=laya_hash(p.handle,p.name,p.bio,p.category,p.website,p.followers))',
                             [(r['id'], r['lh']) for r in rows if r['id'] in current])
            conn.commit()
            current_ids = set(current)
            rows = [r for r in rows if r['id'] not in current_ids]
        if not rows:
            return False
        if not self.laya_allowed(conn):
            return False
        try:
            with engine_controls.work(conn, ticket) as admitted:
                if not admitted:
                    return False
                with resource_budget.lease('laya'):
                    answers = laya.decide([dict(r) for r in rows])
        except resource_budget.Deferred:
            return WorkerDelay(resource_budget.state()['retry_after'])
        if not answers:
            return False
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        with self._resource_lock:
            if not self._current(conn, ticket):
                if not caller_transaction:
                    conn.rollback()
                return False
            if set(answers) != {r['id'] for r in rows} or not all(laya.valid_answers(a) for a in answers.values()):
                if not caller_transaction:
                    conn.rollback()
                return False
            ts = db.now()
            done = []
            for r in rows:
                current = conn.execute('SELECT handle,name,bio,category,website,followers FROM people WHERE id=?', (r['id'],)).fetchone()
                if current and laya_hash(*current) == laya_hash(*(r[k] for k in ('handle','name','bio','category','website','followers'))):
                    done.append(r)
            if not done:
                if not caller_transaction:
                    conn.rollback()
                return False
            conn.executemany('INSERT OR REPLACE INTO laya VALUES(?,?,?,?,?)',
                             [(r['id'], laya_hash(*(r[k] for k in ('handle','name','bio','category','website','followers'))), json.dumps(answers[r['id']]),
                               laya.fit(answers[r['id']]), ts) for r in done])
            conn.executemany('DELETE FROM laya_queue WHERE person_id=?', [(r['id'],) for r in done])
            # The qualify batch folds the new ranking signal into prefilter on its next pass.
            conn.executemany("UPDATE verdicts SET updated_at='' WHERE person_id=?", [(r['id'],) for r in done])
            if not caller_transaction:
                conn.commit()
            return True


    def feedback_example(self, conn, pid):
        """A small, identity-stable example from Michael's mark and manual tags only."""
        r = conn.execute(f"""SELECT p.id,p.handle,p.name,p.bio,m.status AS status FROM people p
            LEFT JOIN ({owner.feedback_marks_sql()}) m ON m.person_id=p.id WHERE p.id=?
            AND (m.status IN ('interested','talking','client','no') OR
                 (m.status IS NOT 'no' AND EXISTS(SELECT 1 FROM tags t WHERE t.person_id=p.id
                    AND t.source='manual' AND t.tag='client' COLLATE NOCASE)))
            AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0 AND p.handle!='fortun8te' COLLATE NOCASE
            AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)""", (pid,)).fetchone()
        if not r:
            return None
        tags = [re.sub(r'\s+', ' ', t[0]).strip()[:64] for t in conn.execute(
            "SELECT tag FROM tags WHERE person_id=? AND source='manual' ORDER BY tag LIMIT ?",
            (pid, self.fewshot_tag_max))]
        return {'person_id': r['id'], 'handle': r['handle'], 'name': (r['name'] or '')[:80],
                'bio': r['bio'][:200], 'label': 'no' if r['status'] == 'no' else 'good',
                'status': r['status'], 'feedback_source': 'status_mark' if r['status'] in (*POSITIVE, 'no') else 'manual_client_tag',
                'manual_tags': [t for t in tags if t and not (r['status'] == 'no' and t.casefold() == 'client')]}


    def fewshot(self, conn):
        """Use bounded owner examples for future calls; batch broad re-runs after several new marks."""
        n = conn.execute(f"SELECT count(*) FROM ({owner.feedback_marks_sql()}) m WHERE m.status IN ('interested','talking','client','no')").fetchone()[0]
        cur = db.get_setting(conn, 'fewshot') or {}
        prior = cur.get('examples') or []
        selected_n = cur.get('n', 0)
        count_due = bool(cur) and abs(n - selected_n) >= max(FEWSHOT_CHANGE, selected_n // 5)
        old_format = any('person_id' not in e or 'feedback_source' not in e for e in prior)
        latest_client = conn.execute(f"""SELECT p.id FROM ({owner.feedback_marks_sql()}) m JOIN people p ON p.id=m.person_id
            WHERE m.status='client' AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0
              AND p.handle!='fortun8te' COLLATE NOCASE
              AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
            ORDER BY m.updated_at DESC,p.id DESC LIMIT 1""").fetchone()
        tagged_client = conn.execute(f"""SELECT p.id FROM tags t JOIN people p ON p.id=t.person_id
            LEFT JOIN ({owner.feedback_marks_sql()}) m ON m.person_id=p.id
            WHERE t.source='manual' AND t.tag='client' COLLATE NOCASE AND m.status IS NOT 'no'
              AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0 AND p.handle!='fortun8te' COLLATE NOCASE
              AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
            ORDER BY CASE WHEN m.status IN ('interested','talking','client') THEN 1 ELSE 0 END,
                     p.updated_at DESC,p.id DESC LIMIT 1""").fetchone()
        prior_ids = {e.get('person_id') for e in prior}
        new_client = any(row and row[0] not in prior_ids for row in (latest_client, tagged_client))
        rebuild = not cur or count_due or old_format or new_client
        ex = []
        if not rebuild:
            ex = [e for e in (self.feedback_example(conn, p['person_id']) for p in prior) if e]
            if len(ex) != len(prior) or any(a['label'] != b['label'] for a, b in zip(ex, prior)):
                rebuild = True
        if rebuild:
            ex = []
            marked_ids = [r[0] for r in conn.execute(f"""SELECT p.id FROM ({owner.feedback_marks_sql()}) m JOIN people p ON p.id=m.person_id
                    WHERE m.status IN {POSITIVE_SQL} AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0
                      AND p.handle!='fortun8te' COLLATE NOCASE
                      AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
                    ORDER BY CASE m.status WHEN 'client' THEN 0 WHEN 'talking' THEN 1 ELSE 2 END,
                             m.updated_at DESC,p.id DESC LIMIT ?""", (FEWSHOT_MAX,))]
            tagged_ids = [r[0] for r in conn.execute(f"""SELECT p.id FROM tags t JOIN people p ON p.id=t.person_id
                LEFT JOIN ({owner.feedback_marks_sql()}) m ON m.person_id=p.id
                WHERE t.source='manual' AND t.tag='client' COLLATE NOCASE AND m.status IS NOT 'no'
                  AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0 AND p.handle!='fortun8te' COLLATE NOCASE
                  AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
                ORDER BY CASE WHEN m.status IN ('interested','talking','client') THEN 1 ELSE 0 END,
                         p.updated_at DESC,p.id DESC LIMIT ?""", (FEWSHOT_MAX,))]
            good_ids = marked_ids[:FEWSHOT_MAX]
            extra_tagged = [pid for pid in tagged_ids if pid not in good_ids]
            if extra_tagged and len(good_ids) == FEWSHOT_MAX:
                good_ids.pop()  # one slot for Michael's manual Client tag, even when marks fill the cap
            good_ids.extend(extra_tagged[:FEWSHOT_MAX - len(good_ids)])
            ex.extend(e for e in (self.feedback_example(conn, pid) for pid in good_ids) if e)
            no_ids = [r[0] for r in conn.execute(f"""SELECT p.id FROM ({owner.feedback_marks_sql()}) m JOIN people p ON p.id=m.person_id
                WHERE m.status='no' AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0
                  AND p.handle!='fortun8te' COLLATE NOCASE
                  AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
                ORDER BY m.updated_at DESC,p.id DESC LIMIT ?""", (FEWSHOT_MAX,))]
            ex.extend(e for e in (self.feedback_example(conn, pid) for pid in no_ids) if e)
        version = self.algorithms.prompt_version(ex) if hasattr(self.algorithms, 'prompt_version') else None
        if ex == prior and version == cur.get('version') and not count_due:
            return ex
        # A rubric/schema revision still invalidates warm verdicts. Owner note/tag
        # edits update future prompts without turning every old verdict into a job.
        # New examples apply to future reviews. Existing AI results retain their
        # provenance; relabeling an AI score as rules would defeat mode isolation.
        db.set_setting(conn, 'fewshot', {'n': n if rebuild else selected_n, 'examples': ex, 'version': version})
        conn.commit()
        return ex


    def llm_candidates(self, conn, limit, exclude):
        """External work is an escalation from a current local review."""
        held = list(exclude)[:900]
        return conn.execute('SELECT p.* ' + external_candidates_sql() +
                            f"AND p.id NOT IN ({','.join('?' * len(held))}) "
                            "ORDER BY coalesce(v.prefilter,0)+coalesce(v.score,0) DESC, p.id LIMIT ?",
                            (time.time(), db.get_setting(conn, 'llm_min'), *held, limit)).fetchall()


    def current_local_review(self, conn, person):
        """External work must wait for the latest local context, including saved notes."""
        if conn.execute('SELECT 1 FROM local_queue WHERE person_id=?', (person['id'],)).fetchone():
            return None
        if (person.get('note') or '').strip() and owner_notes.result(conn, person['id'])['state'] != 'ready':
            return None
        row = conn.execute('SELECT * FROM local_reviews WHERE person_id=?', (person['id'],)).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result['verdict'] = json.loads(result['verdict'] or 'null')
        except (ValueError, TypeError):
            return None
        return result if local_qualification.current_result(result, person,
            owner_notes.local_context(conn, person['id'])) else None


    def safe_research_lookup(self, person):
        try:
            return websearch.lookup(person)
        except Exception as exc:
            print(f'research failed for person {person.get("id")}: {exc}', file=sys.stderr)
            return {'results': [], 'site': ''}


    def research(self, conn, items):
        """Attach web research (SearXNG + their website) to each item's person before the model call.
        Cached per person; missing lookups run in parallel with no DB transaction open. Search down -> no research."""
        if self._closed or not items or not websearch.available():
            return
        websearch.ensure(conn)
        found, todo = {}, []
        for it in items:
            hit = websearch.cached(conn, it['person'])
            if hit is None:
                todo.append(it['person'])
            else:
                found[it['person']['id']] = hit
        conn.commit()
        if todo:
            for p, got in zip(todo, self._research_map(todo)):
                found[p['id']] = got
            with self._resource_lock:
                if self._closed:
                    return
                for p in todo:
                    websearch.store(conn, p, found[p['id']])
                conn.commit()
        for it in items:
            got = found.get(it['person']['id'])
            # A copy: the input hash and the stored profile never see the research fields.
            it['person'] = dict(it['person'], web_lines=websearch.lines(got), web_text=websearch.text(got),
                                web_site=(got or {}).get('site') or '')


    def run_llm(self, conn, rows, skip):
        """One model round for these people (no DB transaction is open during the call). -> number of verdicts written."""
        ticket = processing_modes.begin_work(conn, 'external')
        if self._closed or ticket is None:
            return 0
        rows = [with_owner(conn, dict(r)) for r in rows]
        me = me_handle(conn)
        nets = network_context(conn, [p['id'] for p in rows], me)
        items = []
        websearch.ensure(conn)
        for p in rows:
            edges = edges_of(conn, p['id'])
            fresh_auto = self.algorithms.rule_tags(p, edges, me)
            retained = [tuple(r) for r in conn.execute("SELECT tag, grp FROM tags WHERE person_id=? AND source!='auto'", (p['id'],))]
            items.append({'person': p, 'edges': edges, 'net': nets.get(p['id']),
                          'tags': retained + fresh_auto, 'fresh_auto': fresh_auto})
            hit = websearch.cached(conn, p)
            if hit is not None:
                items[-1]['person'] = dict(p, web_lines=websearch.lines(hit),
                    web_text=websearch.text(hit), web_site=hit.get('site') or '')
        examples = self.fewshot(conn)
        conn.commit()
        if self._closed or control.stage_paused(conn, 'ai'):
            return 0
        try:
            vs = []
            for item in items:
                if self._closed:
                    break
                conn.execute('BEGIN IMMEDIATE')
                latest = conn.execute('SELECT * FROM people WHERE id=?', (item['person']['id'],)).fetchone()
                person = with_owner(conn, dict(latest)) if latest else None
                local = self.current_local_review(conn, person) if person else None
                reason = local['escalation_reason'] if local and local['status'] == 'needs_research' else None
                if not reason or self.algorithms.input_hash(person, edges_of(conn, person['id']),
                        network_context(conn, [person['id']], me).get(person['id'])) != self.algorithms.input_hash(
                        item['person'], item['edges'], item['net']):
                    if person and local is None and (person.get('bio') or '').strip():
                        conn.execute('INSERT OR IGNORE INTO local_queue(person_id) VALUES(?)', (person['id'],))
                    if not self._commit_current(conn, ticket):
                        break
                    vs.append(None)
                    continue
                token = external_queue.claim(conn, person['id'], local['input_hash'])
                if not self._commit_current(conn, ticket):
                    break
                if token is None:
                    vs.append(None)
                    continue
                item['local_review'] = local
                item['claim_token'] = token
                vs.append(external_harness.broad(item['person'], item['tags'], item['edges'], item['net'], examples,
                    escalation_reason=reason, model=db.get_setting(conn, 'scout_model') or 'grok',
                    allowed=lambda: self._current(conn, ticket)) if reason else None)
        except Exception:  # never let one bad reply spin the worker on the same rows: fall back like 'no model'
            traceback.print_exc()
            conn.rollback()
            vs = [None] * len(items)
        conn.execute('BEGIN IMMEDIATE')
        if not self._current(conn, ticket):
            self._finish_claims(conn, items)
            return 0
        wrote = 0
        for it, v in zip(items, vs):
            p = it['person']
            review = it.get('local_review')
            if review and not external_queue.complete(conn, p['id'], review['input_hash'], v, claim_token=it['claim_token']):
                continue
            if v is None:
                if review:
                    skip[p['id']] = datetime.now().timestamp() + 1800
                continue
            latest = conn.execute('SELECT * FROM people WHERE id=?', (p['id'],)).fetchone()
            if latest is None or latest['updated_at'] != p['updated_at']:
                continue
            latest_p = with_owner(conn, dict(latest))
            current_review = self.current_local_review(conn, latest_p)
            if not review or not current_review or (current_review['input_hash'], current_review['updated_at']) != (review['input_hash'], review['updated_at']):
                continue
            latest_net = network_context(conn, [p['id']], me).get(p['id'])
            if self.algorithms.input_hash(latest_p, edges_of(conn, p['id']), latest_net) != self.algorithms.input_hash(p, it['edges'], it['net']):
                continue
            if v.get('content_fit') is not None:
                v['score'] = owner.owner_recommendation(latest_p, {'score': self.algorithms.blend(v['content_fit'], latest_net)})['score']
                v['tier'] = self.algorithms._tier(v['score'], bool((latest_p.get('bio') or '').strip()))
            processing_state.archive_verdict(conn, p['id'])
            if conn.execute('UPDATE verdicts SET score=?, tier=?, role=?, reason=?, model=?, input_hash=?, prompt=?, evidence=?, content_fit=? '
                            "WHERE person_id=? AND updated_at=? AND model!='leadscout'",
                            (v['score'], v['tier'], v['role'], v['reason'], v.get('model') or 'llm', self.algorithms.input_hash(p, it['edges'], it['net']),
                             v.get('prompt'), json.dumps(v.get('evidence') or []),
                             v.get('content_fit', min(getattr(self.algorithms, 'ROLE_CAP', {}).get(v['role'], 100), v['fit']) if v.get('fit') is not None else None),
                             p['id'], p['updated_at'])).rowcount:
                conn.execute('INSERT INTO ai_scoring_events(person_id,scored_at) VALUES(?,?)',
                             (p['id'], db.now()))
                conn.execute("DELETE FROM tags WHERE person_id=? AND source='auto'", (p['id'],))
                conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')",
                                 [(p['id'], t, g) for t, g in it['fresh_auto'] + (v.get('tags') or [])])
                deepscout.retag(conn, p['id'])
                wrote += 1
        if wrote:
            db.set_setting(conn, 'llm_rev', (db.get_setting(conn, 'llm_rev') or 0) + 1)
        if not self._commit_current(conn, ticket):
            self._finish_claims(conn, items)
            return 0
        return wrote


    def llm_step(self, conn, skip):
        """Synchronous single step (tests, tools): one person. The server itself runs LLMPool."""
        if self._closed or processing_modes.begin_work(conn, 'external') is None:
            return None
        _expire(skip)
        rows = self.llm_candidates(conn, 1, skip)
        if not rows:
            return None
        return self.run_llm(conn, rows, skip) > 0


    def retag_if_changed(self, conn):
        """When the rule taxonomy changes (qualify.TAGS_VERSION), let the qualify batch re-derive everyone's auto tags.
        LLM verdicts survive: requalify keeps them while the input hash is unchanged."""
        # "knows you" is an owner-only claim now; retire the old automatic copies even when the version is current.
        if conn.execute("DELETE FROM tags WHERE tag='knows you' AND source='auto'").rowcount:
            conn.commit()
        version = getattr(self.algorithms, 'TAGS_VERSION', None)
        if version is None or db.get_setting(conn, 'tags_version') == version:
            return False
        conn.execute("UPDATE verdicts SET updated_at=''")
        db.set_setting(conn, 'tags_version', version)
        conn.commit()
        return True


    def refresh_laya_prefilter_if_changed(self, conn):
        """Queue only Laya-scored verdicts after a prefilter policy change.

        The version write and invalidation share one transaction, so an interrupted
        startup can safely retry. The normal qualify worker reblends in batches.
        """
        version = self.algorithms.PREFILTER_VERSION
        if db.get_setting(conn, 'laya_prefilter_version') == version:
            return 0
        changed = conn.execute("UPDATE verdicts SET updated_at='' WHERE person_id IN "
                               "(SELECT person_id FROM laya)").rowcount
        db.set_setting(conn, 'laya_prefilter_version', version)
        conn.commit()
        return changed


class LLMPool:
    """Bounded model calls, each using the owning service and its database."""

    def __init__(self, service, batch=None, db_path=None):
        self.service = service
        self.db_path = str(db_path if db_path is not None else service.config.db)
        self.lock = threading.RLock()
        self.inflight = set()
        self.running = 0
        self.skip = {}
        self.batch = batch
        self._executor = None
        self._closed = False

    def step(self, conn):
        if processing_modes.begin_work(conn, 'external') is None:
            return False
        workers = max(1, min(3, int(db.get_setting(conn, 'llm_workers') or 1)))
        batch = max(1, int(self.batch or 1))
        with self.lock:
            if self._closed or getattr(self.service, '_closed', False):
                return False
            _expire(self.skip)
            free = workers - self.running
            if free <= 0:
                return False
            exclude = set(self.inflight) | set(self.skip)
            rows = self.service.llm_candidates(conn, free * batch, exclude)
            if not rows:
                return False
            groups = [rows[i:i + batch] for i in range(0, len(rows), batch)][:free]
            if self._executor is None:
                self._executor = DaemonExecutor(max_workers=3, thread_name_prefix='qualification')
            for group in groups:
                ids = {row['id'] for row in group}
                self.running += 1
                self.inflight.update(ids)
                try:
                    future = self._executor.submit(self._work, group)
                except BaseException:
                    self.running -= 1
                    self.inflight.difference_update(ids)
                    raise
                future.add_done_callback(lambda finished, ids=ids: self._finished(ids))
            return True

    def _work(self, rows):
        conn = None
        try:
            if self._closed or getattr(self.service, '_closed', False):
                return
            conn = db.connect(self.db_path)
            if self._closed or getattr(self.service, '_closed', False):
                return
            self.service.run_llm(conn, rows, self.skip)
        except Exception:
            traceback.print_exc()
        finally:
            if conn is not None:
                conn.close()

    def _finished(self, ids):
        with self.lock:
            self.running -= 1
            self.inflight.difference_update(ids)

    def idle(self):
        with self.lock:
            return self.running == 0

    def shutdown(self, wait=True):
        with self.lock:
            self._closed = True
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)
