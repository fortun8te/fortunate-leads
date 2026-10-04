"""Identity storage ownership; writes remain in the caller transaction."""

import json
from datetime import datetime, timezone

from .clock import now, utc_now
from .handles import norm_handle, normalize_ig_id
from .invalidation import dirty_seed_members, mark_network_dirty
from .settings import get_setting, set_setting


BIO_FIELDS = {'bio', 'bio_at', 'bio_src', 'website', 'category', 'followers', 'following', 'posts', 'is_business'}


PERSON_FIELDS = ('ig_id', 'handle', 'name', 'pic_url', 'is_private', 'is_verified', 'bio', 'website', 'category',
                 'followers', 'following', 'posts', 'is_business', 'bio_at', 'bio_src')


def move_seed(conn, old, new):
    """Move one proven seed identity and retain its list/job history."""
    dirty_seed_members(conn, old, new)
    conn.execute('UPDATE seeds SET handle=? WHERE handle=?', (new, old))
    conn.execute('UPDATE lists SET seed=? WHERE seed=?', (new, old))
    conn.execute('UPDATE edges SET seed=? WHERE seed=?', (new, old))
    conn.execute('UPDATE edge_evidence SET seed=? WHERE seed=?', (new, old))
    conn.execute('UPDATE jobs SET seed=? WHERE lower(seed)=lower(?)', (new, old))
    conn.execute('UPDATE jobs SET handle=? WHERE lower(handle)=lower(?)', (new, old))
    # A callback leased under the previous handle must not write to the new holder.
    conn.execute("UPDATE jobs SET state='queued',leased_until=NULL,lane=NULL,lease_token=NULL "
                 "WHERE state='leased' AND (seed=? OR handle=?)", (new, new))
    retries = get_setting(conn, 'lists_reopened') or {}
    for direction in ('followers', 'following'):
        key = f'{old}|{direction}'
        if key in retries:
            retries[f'{new}|{direction}'] = retries.pop(key)
    if retries:
        set_setting(conn, 'lists_reopened', retries)


def vacant_handle(conn, handle, suffix):
    candidate = f'{handle}~{suffix}'
    while conn.execute('SELECT 1 FROM people WHERE handle=? UNION ALL SELECT 1 FROM seeds WHERE handle=?',
                       (candidate, candidate)).fetchone():
        candidate += '~'
    return candidate


def rename_seed(conn, old, new, ig_id):
    dirty_seed_members(conn, old, new)
    seed = conn.execute('SELECT * FROM seeds WHERE handle=?', (old,)).fetchone()
    if not seed or (normalize_ig_id(seed['ig_id']) and normalize_ig_id(seed['ig_id']) != ig_id):
        # Profile work follows a proven person identity even without a seed.
        # Revoke any old lease before this handle can belong to someone else.
        conn.execute("UPDATE jobs SET handle=?,state=CASE WHEN state='leased' THEN 'queued' ELSE state END, "
                     "leased_until=NULL,lane=NULL,lease_token=NULL WHERE kind='profile' AND lower(handle)=lower(?)",
                     (new, old))
        return
    target = conn.execute('SELECT * FROM seeds WHERE handle=?', (new,)).fetchone()
    if target and target['ig_id'] == ig_id:
        # Both seed records have identity proof. Keep the newest list state and
        # every historical job/edge; retire a duplicate live run if necessary.
        for source_list in conn.execute('SELECT * FROM lists WHERE seed=?', (old,)).fetchall():
            dest_list = conn.execute('SELECT * FROM lists WHERE seed=? AND direction=?',
                                     (new, source_list['direction'])).fetchone()
            if dest_list:
                winner = source_list if (source_list['updated_at'] or '') > (dest_list['updated_at'] or '') else dest_list
                loser = dest_list if winner is source_list else source_list
                conn.execute('DELETE FROM lists WHERE seed=? AND direction=?', (loser['seed'], loser['direction']))
                conn.execute("UPDATE jobs SET state='cancelled',leased_until=NULL,lane=NULL,lease_token=NULL "
                             "WHERE seed=? AND direction=? AND kind='list' AND state IN ('queued','leased')",
                             (loser['seed'], loser['direction']))
        for edge in conn.execute('SELECT * FROM edges WHERE seed=?', (old,)).fetchall():
            conn.execute('INSERT INTO edges VALUES(?,?,?,?) ON CONFLICT(seed,person_id,direction) DO UPDATE '
                         'SET first_seen=min(edges.first_seen,excluded.first_seen)',
                         (new, edge['person_id'], edge['direction'], edge['first_seen']))
        conn.execute('DELETE FROM edges WHERE seed=?', (old,))
        for ev in conn.execute('SELECT * FROM edge_evidence WHERE seed=?', (old,)).fetchall():
            conn.execute('INSERT INTO edge_evidence VALUES(?,?,?,?,?,?) ON CONFLICT(seed,person_id,direction) DO UPDATE SET '
                         'active=CASE WHEN excluded.checked_at>edge_evidence.checked_at THEN excluded.active ELSE edge_evidence.active END, '
                         'observed_at=nullif(max(coalesce(edge_evidence.observed_at,\'\'),coalesce(excluded.observed_at,\'\')),\'\'), '
                         'checked_at=max(edge_evidence.checked_at,excluded.checked_at)',
                         (new, ev['person_id'], ev['direction'], ev['active'], ev['observed_at'], ev['checked_at']))
        conn.execute('DELETE FROM edge_evidence WHERE seed=?', (old,))
        conn.execute('UPDATE seeds SET is_me=max(is_me,?),added_at=min(coalesce(added_at,?),coalesce(?,added_at)) WHERE handle=?',
                     (seed['is_me'], seed['added_at'], seed['added_at'], new))
        conn.execute('DELETE FROM seeds WHERE handle=?', (old,))
        move_seed(conn, old, new)
        return
    if target:
        # Even an unverified destination may describe another holder. Keep it separate.
        parked = vacant_handle(conn, new, target['ig_id'] or 'seed')
        move_seed(conn, new, parked)
        conn.execute("UPDATE jobs SET state='cancelled',leased_until=NULL,lane=NULL,lease_token=NULL "
                     "WHERE (seed=? OR handle=?) AND state IN ('queued','leased')", (parked, parked))
    move_seed(conn, old, new)
    conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (ig_id, new))


def park_person(conn, row):
    old = conn.execute('SELECT handle FROM people WHERE id=?', (row['id'],)).fetchone()[0]
    parked = vacant_handle(conn, old, row['id'])
    rename_seed(conn, old, parked, row['ig_id'])
    conn.execute('UPDATE people SET handle=? WHERE id=?', (parked, row['id']))
    conn.execute("UPDATE jobs SET state='cancelled',leased_until=NULL,lane=NULL,lease_token=NULL "
                 "WHERE (seed=? OR handle=?) AND state IN ('queued','leased')", (parked, parked))


def _preserve_seed_identity(conn, person):
    """Keep handle-keyed history attached to its known owner before a rename.

    An already identified seed can disagree with the current handle holder. Keep
    that evidence intact; only fill absent IDs, including legacy blank values.
    """
    ig_id = normalize_ig_id(person['ig_id'])
    if ig_id:
        seed = conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (person['handle'],)).fetchone()
        if seed is not None and normalize_ig_id(seed['ig_id']) is None:
            conn.execute('UPDATE seeds SET ig_id=? WHERE handle=? AND ig_id IS ?',
                         (ig_id, person['handle'], seed['ig_id']))


def _profile_time(value):
    """Compare capture instants, including legacy dates and offset timestamps."""
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (AttributeError, ValueError, OverflowError):
        return datetime.min.replace(tzinfo=timezone.utc)


def upsert_person(conn, u, ts=None):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    ts = ts or now()
    vals = {k: u[k] for k in PERSON_FIELDS if u.get(k) is not None}
    for k in ('is_private', 'is_verified', 'is_business'):
        if k in vals:
            vals[k] = int(bool(vals[k]))
    if 'ig_id' in vals:
        ig_id = normalize_ig_id(vals['ig_id'])
        if ig_id is None:
            del vals['ig_id']
        else:
            vals['ig_id'] = ig_id
    if 'handle' in vals:
        vals['handle'] = norm_handle(vals['handle'])
        if not vals['handle']:
            raise ValueError('invalid Instagram handle')
    by_id = vals.get('ig_id') and conn.execute('SELECT id, handle, updated_at FROM people WHERE ig_id=?', (vals['ig_id'],)).fetchone()
    if by_id and _profile_time(ts) <= _profile_time(by_id['updated_at']):
        vals.pop('handle', None)  # an older observation cannot undo a known rename
    by_handle = vals.get('handle') and conn.execute('SELECT id, ig_id, handle, updated_at FROM people WHERE handle=?', (vals['handle'],)).fetchone()
    if by_handle and normalize_ig_id(by_handle['ig_id']) is None and vals.get('ig_id'):
        seed = conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (by_handle['handle'],)).fetchone()
        seed_id = normalize_ig_id(seed['ig_id']) if seed is not None else None
        if seed_id and seed_id != vals['ig_id']:
            raise ValueError('seed account identity conflicts with incoming profile')
    if (by_handle and by_handle['ig_id'] and vals.get('ig_id')
            and by_handle['ig_id'] != vals['ig_id'] and _profile_time(ts) <= _profile_time(by_handle['updated_at'])):
        # A historical or tied claim cannot displace a newer proven owner.
        # Keep an existing identity at its known handle, or retain a newly
        # discovered historical identity under a separate parked handle.
        if by_id:
            vals.pop('handle', None)
        else:
            vals['handle'] = vacant_handle(conn, vals['handle'], vals['ig_id'])
        by_handle = None
    if not by_id and by_handle and by_handle['ig_id'] and vals.get('ig_id') and by_handle['ig_id'] != vals['ig_id']:
        # a different account now holds this handle: the old row keeps its marks/edges/tags under a parked handle
        park_person(conn, by_handle)
        by_handle = None
    if by_id and by_handle and by_id['id'] != by_handle['id']:
        if by_handle['ig_id']:  # the handle now belongs to this ig_id; the old holder renamed
            park_person(conn, by_handle)
        else:  # same account seen before without ig_id: fold it in
            merge_people(conn, keep=by_id['id'], drop=by_handle['id'])
    row = by_id or by_handle
    if row:
        pid = row['id']
        current = conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
        if by_id and vals.get('handle') and current['handle'] != vals['handle']:
            rename_seed(conn, current['handle'], vals['handle'], vals['ig_id'])
        if current['bio_at'] and _profile_time(vals.get('bio_at') or ts) < _profile_time(current['bio_at']):
            vals = {k: v for k, v in vals.items() if k not in BIO_FIELDS}
        if _profile_time(ts) < _profile_time(current['updated_at']):
            vals = {k: v for k, v in vals.items() if k in BIO_FIELDS or k == 'ig_id' or current[k] is None}
        changes = {k: v for k, v in vals.items() if current[k] != v}
        if changes:
            # A successful reread can refresh the observation time/source without
            # changing the profile that qualification consumes. Preserve that
            # freshness, but do not schedule another rules pass for the same data.
            content_changed = any(k not in ('bio_at', 'bio_src') for k in changes)
            if 'pic_url' in changes:
                # CDN URLs rotate independently of the photo. Keep the last
                # downloaded image visible while a refresh waits for network access.
                changes['pic_refresh'] = int(bool(current['pic_file']))
                changes['pic_attempts'] = 0
                changes['pic_retry_at'] = None
                if not current['pic_file']:
                    changes['pic_file'] = None
            if content_changed:
                changes['updated_at'] = max((ts, current['updated_at']), key=_profile_time)
            conn.execute(f"UPDATE people SET {', '.join(k + '=?' for k in changes)} WHERE id=?",
                         (*changes.values(), pid))
        return pid
    vals['id'] = conn.execute('SELECT value+1 FROM person_id_sequence WHERE singleton=1').fetchone()[0]
    cols = list(vals) + ['first_seen', 'updated_at']
    return conn.execute(f"INSERT INTO people({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                        (*vals.values(), ts, ts)).lastrowid


def _earliest_observed(left, right):
    """Prefer the earliest trustworthy instant over invalid/naive/future dates.

    If neither date establishes an instant, retain a deterministic source value
    for legacy compatibility. It remains unknown to evidence readers. Two
    missing values stay NULL; ledger callers use '' for their NOT NULL column.
    """
    valid = []
    cutoff = utc_now()
    for value in (left, right):
        if not isinstance(value, str):
            continue
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if dt.tzinfo is not None and dt <= cutoff:
                valid.append(dt.astimezone(timezone.utc))
        except (ValueError, OverflowError):
            continue
    if valid:
        return min(valid).isoformat()
    unknown = [value for value in (left, right) if isinstance(value, str) and value]
    return min(unknown) if unknown else None


def merge_people(conn, keep, drop):
    import workflows
    if keep == drop:
        return
    # Leave committing to the caller, including when called outside an existing transaction.
    if not conn.in_transaction:
        conn.execute('BEGIN')
    conn.execute('SAVEPOINT merge_people')
    try:
        if conn.execute('SELECT count(*) FROM people WHERE id IN (?,?)', (keep, drop)).fetchone()[0] != 2:
            raise ValueError('both people must exist before merging')
        pa = conn.execute('SELECT * FROM people WHERE id=?', (keep,)).fetchone()
        pb = conn.execute('SELECT * FROM people WHERE id=?', (drop,)).fetchone()
        affected_seeds = {r[0] for r in conn.execute('SELECT DISTINCT seed FROM edges WHERE person_id IN (?,?)', (keep, drop))}
        dirty_seed_members(conn, *affected_seeds)
        # Fold profile fields: the newer bio wins; other fields fill gaps on the survivor.
        fields = {}
        newer_bio = pb['bio_at'] and (not pa['bio_at'] or pb['bio_at'] > pa['bio_at'])
        for key in pa.keys():
            if key in ('id', 'ig_id', 'handle', 'first_seen', 'updated_at', 'pic_file', 'pic_refresh', 'pic_attempts', 'pic_retry_at'):
                continue
            take = (newer_bio or not pa['bio_at']) if key in BIO_FIELDS else (pa[key] is None or pa[key] == '')
            if pb[key] is not None and take:
                fields[key] = pb[key]
        fields['first_seen'] = min(pa['first_seen'], pb['first_seen'])
        fields['updated_at'] = max(pa['updated_at'], pb['updated_at'])
        conn.execute(f"UPDATE people SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), keep))
        conn.execute('UPDATE activity SET person_id=? WHERE person_id=?', (keep, drop))
        kept, dropped = workflows.follow_up(conn, keep), workflows.follow_up(conn, drop)
        if dropped:
            # Keep an open reminder over completed; then earliest due. Record both snapshots on conflict.
            chosen = min([x for x in (kept, dropped) if x], key=lambda x: (x['completed_at'] is not None, x['due_on']))
            conn.execute('INSERT OR REPLACE INTO followups VALUES(?,?,?,?,?)', (keep, chosen['due_on'], chosen['note'], chosen['completed_at'], chosen['updated_at']))
            conn.execute('DELETE FROM followups WHERE person_id=?', (drop,))
            if kept:
                workflows.event(conn, keep, 'follow_up_merged', before={'kept': kept, 'merged': dropped}, after=chosen)

        # Human relationship history survives identity consolidation independently of outreach stage.
        for legacy_pid in (keep, drop):
            if conn.execute("SELECT 1 FROM marks WHERE person_id=? AND status='client' UNION SELECT 1 FROM tags WHERE person_id=? AND source='manual' AND lower(trim(tag))='client'", (legacy_pid, legacy_pid)).fetchone():
                context = conn.execute('SELECT * FROM owner_context WHERE person_id=?', (legacy_pid,)).fetchone()
                values = set(json.loads(context['relationships']) if context else []) | {'client', 'worked_with'}
                conn.execute('INSERT OR REPLACE INTO owner_context VALUES(?,?,?,?)',
                             (legacy_pid, json.dumps(sorted(values)), context['familiarity'] if context else None,
                              context['updated_at'] if context else now()))
        contexts = conn.execute('SELECT * FROM owner_context WHERE person_id IN (?,?) ORDER BY updated_at DESC', (keep, drop)).fetchall()
        if contexts:
            relations = sorted({value for r in contexts for value in json.loads(r['relationships'])})
            familiarity = next((r['familiarity'] for r in contexts if r['familiarity']), None)
            conn.execute('INSERT OR REPLACE INTO owner_context VALUES(?,?,?,?)',
                         (keep, json.dumps(relations), familiarity, contexts[0]['updated_at']))
            conn.execute('DELETE FROM owner_context WHERE person_id=?', (drop,))

        km = conn.execute('SELECT * FROM marks WHERE person_id=?', (keep,)).fetchone()
        dm = conn.execute('SELECT * FROM marks WHERE person_id=?', (drop,)).fetchone()
        before, after = {}, {}
        if km and dm:
            # The newer explicit status wins; a note-only duplicate carries no status decision.
            latest = dm if (dm['updated_at'] or '') > (km['updated_at'] or '') else km
            other = km if latest is dm else dm
            status = latest['status'] if latest['status'] is not None else other['status']
            notes = list(dict.fromkeys(x for x in (km['note'], dm['note']) if x))
            combined = '\n\n'.join(notes) or None
            # Keep the editable note within its limit; the full dropped note stays in history.
            note = combined if combined is None or len(combined) <= 5000 else km['note']
            before, after = dict(dm), {'status': status, 'note': note}
            conn.execute('UPDATE marks SET status=?,note=?,updated_at=? WHERE person_id=?',
                         (status, note, latest['updated_at'], keep))

        # Derived scores belong to their profile inputs. Keep the survivor's result.
        # Website reads instead keep the newest useful evidence, ahead of empty failures.
        def site_priority(row):
            try:
                signals = json.loads(row['signals'] or '{}')
            except (ValueError, TypeError):
                signals = {}
            useful = bool(row['title'] or row['summary'] or isinstance(signals, dict) and signals or not row['error'])
            try:
                stamp = datetime.fromisoformat((row['at'] or '').replace('Z', '+00:00'))
                stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
            except (ValueError, TypeError):
                stamp = datetime.min.replace(tzinfo=timezone.utc)
            return useful, stamp

        # Website reads are optional tables, created lazily by qual_api.
        optional = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('site_reads','site_evidence')")}
        keep_had_site = 'site_reads' in optional and \
            conn.execute('SELECT 1 FROM site_reads WHERE person_id=?', (keep,)).fetchone()
        conflicts, chosen_records = {}, {}
        singletons = ['verdicts', 'laya']
        if 'site_reads' in optional:
            singletons.append('site_reads')
        for table in singletons:
            kept = conn.execute(f'SELECT * FROM {table} WHERE person_id=?', (keep,)).fetchone()
            dropped = conn.execute(f'SELECT * FROM {table} WHERE person_id=?', (drop,)).fetchone()
            if kept and dropped:
                if any(kept[key] != dropped[key] for key in kept.keys() if key != 'person_id'):
                    chosen = kept
                    if table == 'site_reads' and site_priority(dropped) > site_priority(kept):
                        chosen = dropped
                        columns = tuple(key for key in
                                        ('url', 'final_url', 'title', 'summary', 'signals', 'error', 'model', 'at', 'content_hash')
                                        if key in dropped.keys())
                        conn.execute('UPDATE site_reads SET ' + ','.join(key + '=?' for key in columns) + ' WHERE person_id=?',
                                     (*[dropped[key] for key in columns], keep))
                        # Keep site evidence paired with the chosen read.
                        if 'site_evidence' in optional:
                            conn.execute('DELETE FROM site_evidence WHERE person_id=?', (keep,))
                            conn.execute('UPDATE site_evidence SET person_id=? WHERE person_id=?', (keep, drop))
                    conflicts[table] = {'kept': dict(kept), 'merged': dict(dropped)}
                    chosen_records[table] = dict(chosen, person_id=keep)
            conn.execute(f'UPDATE OR IGNORE {table} SET person_id=? WHERE person_id=?', (keep, drop))
            conn.execute(f'DELETE FROM {table} WHERE person_id=?', (drop,))

        for dropped in conn.execute('SELECT * FROM tags WHERE person_id=?', (drop,)).fetchall():
            kept = conn.execute('SELECT * FROM tags WHERE person_id=? AND tag=?', (keep, dropped['tag'])).fetchone()
            if kept and (kept['grp'], kept['source']) != (dropped['grp'], dropped['source']):
                chosen = dropped if dropped['source'] == 'manual' and kept['source'] != 'manual' else kept
                conflicts.setdefault('tags', []).append({'kept': dict(kept), 'merged': dict(dropped)})
                chosen_records.setdefault('tags', []).append(dict(chosen, person_id=keep))
                if chosen is dropped:
                    conn.execute('UPDATE tags SET grp=?,source=? WHERE person_id=? AND tag=?',
                                 (dropped['grp'], dropped['source'], keep, dropped['tag']))

        # Retain the earliest edge and every distinct page observation when identities merge.
        conn.create_function('earliest_observed', 2, _earliest_observed)
        conn.execute('INSERT INTO edges(seed,person_id,direction,first_seen) '
                     'SELECT seed,?,direction,first_seen FROM edges WHERE person_id=? '
                     'ON CONFLICT(seed,person_id,direction) DO UPDATE SET first_seen='
                     'earliest_observed(edges.first_seen,excluded.first_seen)',
                     (keep, drop))
        conn.execute('DELETE FROM edges WHERE person_id=?', (drop,))
        conn.execute('INSERT INTO edge_observations(seed,person_id,direction,page_key,job_id,observed_at) '
                     'SELECT seed,?,direction,page_key,job_id,observed_at FROM edge_observations WHERE person_id=? '
                     'ON CONFLICT(seed,person_id,direction,page_key) DO UPDATE SET '
                     # The ledger's NOT NULL date uses an empty string for unknown.
                     "observed_at=coalesce(earliest_observed(edge_observations.observed_at,excluded.observed_at),'')", (keep, drop))
        conn.execute('DELETE FROM edge_observations WHERE person_id=?', (drop,))
        for ev in conn.execute('SELECT * FROM edge_evidence WHERE person_id=?', (drop,)).fetchall():
            conn.execute('INSERT INTO edge_evidence VALUES(?,?,?,?,?,?) ON CONFLICT(seed,person_id,direction) DO UPDATE SET '
                         'active=CASE WHEN excluded.checked_at>edge_evidence.checked_at THEN excluded.active ELSE edge_evidence.active END, '
                         'observed_at=nullif(max(coalesce(edge_evidence.observed_at,\'\'),coalesce(excluded.observed_at,\'\')),\'\'), '
                         'checked_at=max(edge_evidence.checked_at,excluded.checked_at)',
                         (ev['seed'], keep, ev['direction'], ev['active'], ev['observed_at'], ev['checked_at']))
        conn.execute('DELETE FROM edge_evidence WHERE person_id=?', (drop,))
        conn.execute('INSERT INTO list_members SELECT job_id, ?, observed_at FROM list_members WHERE person_id=? '
                     'ON CONFLICT(job_id,person_id) DO UPDATE SET observed_at=max(list_members.observed_at,excluded.observed_at)', (keep, drop))
        conn.execute('DELETE FROM list_members WHERE person_id=?', (drop,))
        if 'site_evidence' in optional:
            if not keep_had_site:   # the survivor took the duplicate's only read: bring its evidence along
                conn.execute('UPDATE site_evidence SET person_id=? WHERE person_id=?', (keep, drop))
            conn.execute('DELETE FROM site_evidence WHERE person_id=?', (drop,))
        for table in ('tags', 'marks'):
            conn.execute(f'UPDATE OR IGNORE {table} SET person_id=? WHERE person_id=?', (keep, drop))
            conn.execute(f'DELETE FROM {table} WHERE person_id=?', (drop,))
        if conflicts:
            before['records'], after['records'] = conflicts, chosen_records
        if before:
            workflows.event(conn, keep, 'identity_merged', before=before, after=after)
        conn.execute('DELETE FROM network_dirty WHERE person_id=?', (drop,))
        mark_network_dirty(conn, [keep])
        import note_mentions
        note_mentions.merge(conn, keep, drop)
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
        conn.execute('DELETE FROM people WHERE id=?', (drop,))
    except Exception:
        conn.execute('ROLLBACK TO merge_people')
        conn.execute('RELEASE merge_people')
        raise
    conn.execute('RELEASE merge_people')
