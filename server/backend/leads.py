"""LeadService owns one workspace and its request dependencies."""

import deepscout
import json
import accounts
import note_mentions
import db
import owner
import owner_notes
import processing_modes
import processing_state
import rules
import workflows
import qual_api
from .common import (
    status_in,
    Bad,
    Conflict,
    qint,
    read_snapshot,
    data_rev,
    STATUSES,
    READ_PRIORITY,
    LISTS,
    PEOPLE_FROM,
    LEAD_SQL,
    NOT_ME,
    SORTS,
    KEEP,
)
from .evidence import edges_of, edge_history_of, with_owner
from .queries import lead_rows, lead_filter, tag_facets, counts, person_row
from .owner_edits import (
    set_status,
    clean_tag,
    manual_tag,
    tag_group,
    touch,
    add_manual,
    rule_out,
)


class LeadService:

    def __init__(self, config, cache):
        self.config = config
        self.cache = cache

    def api_leads(self, conn, q, b):
        where, args = lead_filter(q)
        sort = q.get('sort', ['score'])[0]
        if sort not in SORTS:
            raise Bad('sort must be one of ' + ', '.join(SORTS))
        order = SORTS[sort]
        offset = max(0, qint(q, 'offset') or 0)
        limit = min(500, max(1, qint(q, 'limit') or 50))
        sql_where = ' WHERE ' + ' AND '.join([NOT_ME] + where)
        with read_snapshot(conn):
            rev = data_rev(conn)
            # The unfiltered total needs neither verdicts nor connection summaries.
            # Sort a thin page first so SQLite never carries bios and notes through
            # the full ranking sort; hydrate only the selected profiles afterward.
            count_from = ('FROM people p LEFT JOIN marks m ON m.person_id=p.id'
                          if where == ["coalesce(m.status,'')!='no'"] and not args else PEOPLE_FROM)
            import lead_rank
            indexed = lead_rank.page_for_query(conn, sort, limit, offset, q)
            if indexed is not None:
                total, ids = indexed
            else:
                total = conn.execute(f'SELECT count(*) {count_from}{sql_where}', args).fetchone()[0]
                picked = conn.execute(f'SELECT p.id, {LISTS} AS lists {PEOPLE_FROM}{sql_where} '
                                      f'ORDER BY {order}, p.id LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
                ids = [row['id'] for row in picked]
            if ids:
                placeholders = ','.join('?' for _ in ids)
                by_id = {row['id']: row for row in conn.execute(f'{LEAD_SQL} WHERE p.id IN ({placeholders})', ids)}
                rows = [by_id[pid] for pid in ids]
            else:
                rows = []
            next_offset = offset + len(rows)
            return {'total': total, 'rows': lead_rows(conn, rows), 'rev': rev,
                    'next_offset': next_offset, 'has_more': next_offset < total}

    def api_tags(self, conn, q, b):
        return self.cache.cached(conn, 'tags', q, lambda: tag_facets(conn, q))

    def api_counts(self, conn, q, b):
        return self.cache.cached(conn, 'counts', q, lambda: counts(conn, q))

    def api_person(self, conn, q, b, pid):
        row = person_row(conn, pid)
        v = conn.execute('SELECT * FROM verdicts WHERE person_id=?', (pid,)).fetchone()
        verdict = dict(v) if v else None
        if verdict:
            try:
                ev = json.loads(verdict.get('evidence') or '[]')
            except ValueError:
                ev = []
            verdict['evidence'] = [x for x in ev if isinstance(x, str)] if isinstance(ev, list) else []
        owner_facts = with_owner(conn, dict(row))
        if verdict:
            verdict = owner.owner_recommendation(owner_facts, verdict)
        # Active work takes precedence over history; otherwise show the latest request for this profile.
        job = conn.execute("SELECT state FROM jobs WHERE kind='profile' AND handle=? AND state!='cancelled' "
                           "ORDER BY (state IN ('queued','leased')) DESC, id DESC LIMIT 1", (row['handle'],)).fetchone()
        pending = bool(job and job['state'] in ('queued', 'leased'))
        ranking_pending = bool(conn.execute('SELECT 1 FROM processing_rule_queue WHERE person_id=?', (pid,)).fetchone())
        if not ranking_pending and processing_modes.begin_work(conn, 'local_qualification') is not None:
            ranking_pending = bool(conn.execute('SELECT 1 FROM local_queue WHERE person_id=? AND priority=1', (pid,)).fetchone())
        profile_read = {'state': {'leased': 'reading', 'error': 'failed'}.get(job['state'], job['state'])} if job else None
        return dict(lead_rows(conn, [row])[0], edges=edges_of(conn, pid), edge_history=edge_history_of(conn, pid),
                    verdict=verdict, note=row['note'], site=qual_api.site_row(conn, pid),
                    activity=workflows.history(conn, pid), profile_read_pending=pending, profile_read=profile_read,
                    ranking_pending=ranking_pending, note_mentions=note_mentions.get(conn, pid),
                    review_history=processing_state.recent_history(conn, 6, pid),
                    scout=deepscout.result(conn, pid), note_interpretation=owner_notes.result(conn, pid))

    def api_mark(self, conn, q, b, pid):
        """Optional if_match/mark_rev compares the last read mark before applying a partial edit."""
        status, note = b.get('status', KEEP), b.get('note', KEEP)
        relationships, familiarity = b.get('relationships', KEEP), b.get('familiarity', KEEP)
        if relationships is not KEEP:
            try:
                relationships = owner.normalize_relationships(relationships)
            except ValueError as exc:
                raise Bad(str(exc)) from exc
        if familiarity is not KEEP and familiarity is not None and (not isinstance(familiarity, str) or familiarity not in owner.FAMILIARITIES):
            raise Bad('Choose a known familiarity')
        status = status_in(status) if isinstance(status, str) else status
        if status is not KEEP and status is not None and status not in STATUSES:
            raise Bad('bad status')
        if note is not KEEP and note is not None and (not isinstance(note, str) or len(note) > 5000):
            raise Bad('note must be text')
        expected = b.get('if_match', b.get('mark_rev', KEEP))
        if expected is not KEEP and not isinstance(expected, str):
            raise Bad('mark revision must be text')
        if 'if_match' in b and 'mark_rev' in b and b['if_match'] != b['mark_rev']:
            raise Bad('mark revisions disagree')
        # Acquire the writer lock before reading: two tabs cannot both pass the same comparison.
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = person_row(conn, pid)
            current = {key: row[key] for key in ('status', 'note', 'mark_rev')}
            current['id'] = pid
            owner.hydrate(conn, [current])
            if expected is not KEEP and expected != current['mark_rev']:
                raise Conflict(current)
            set_status(conn, [pid], status, note, relationships, familiarity)
            if note is not KEEP or 'note_mentions' in b:
                previous_mentions = note_mentions.get(conn, pid)
                note_mentions.save(conn, pid, (note if note is not KEEP else current['note']) or '', b.get('note_mentions'))
                if note_mentions.get(conn, pid) != previous_mentions:
                    conn.execute('UPDATE marks SET updated_at=? WHERE person_id=?', (db.now(), pid))
                    owner_notes.invalidate(conn, pid)
                    touch(conn, [pid])
            row = person_row(conn, pid)
            result = {key: row[key] for key in ('status', 'note', 'mark_rev')}
            result['id'] = pid
            result['note_mentions'] = note_mentions.get(conn, pid)
            owner.hydrate(conn, [result])
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    def api_tag_edit(self, conn, q, b, pid):
        for key in ('add', 'remove'):
            if key in b and (not isinstance(b[key], list) or not all(isinstance(t, str) for t in b[key])):
                raise Bad(f'{key} must be a list of tags')
        additions = [clean_tag(t) for t in b.get('add') or []]
        relation_names = {label.casefold(): key for key, label in owner.RELATIONSHIPS.items()}
        conn.execute('BEGIN IMMEDIATE')
        try:
            person = with_owner(conn, dict(person_row(conn, pid)))
            selected = set(owner.relationships(person))
            relation_edit = False
            for t in additions:
                if t.casefold() in relation_names:
                    selected.add(relation_names[t.casefold()])
                    relation_edit = True
                else:
                    add_manual(conn, [pid], [t])
            for t in b.get('remove') or []:
                conn.execute("DELETE FROM tags WHERE person_id=? AND tag=? AND source='manual'", (pid, t))
                if t.casefold() in relation_names:
                    key = relation_names[t.casefold()]
                    selected.discard(key)
                    if key == 'worked_with':
                        selected.discard('client')
                    relation_edit = True
            rules.sync(conn, [pid])
            if relation_edit:
                set_status(conn, [pid], relationships=owner.normalize_relationships(list(selected)))
            touch(conn, [pid])
            result = {'relationships': owner.normalize_relationships(list(selected))} if relation_edit else {}
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    def api_tag_rename(self, conn, q, b):
        src, dst = clean_tag(b.get('from')), clean_tag(b.get('to'))
        fixed = {label.casefold() for label in owner.RELATIONSHIPS.values()}
        if src.casefold() in fixed or dst.casefold() in fixed:
            raise Bad('Edit relationships on the profile')
        dst = manual_tag(dst)
        if src == dst:
            return {'renamed': 0}
        pids = [r[0] for r in conn.execute("SELECT person_id FROM tags WHERE tag=? AND source='manual'", (src,))]
        grp = tag_group(conn, dst, tag_group(conn, src))
        # merge: someone who already has `to` (any source) keeps one tag, now manual
        conn.execute("INSERT INTO tags(person_id, tag, grp, source) SELECT person_id, ?, ?, 'manual' FROM tags WHERE tag=? AND source='manual' "
                     "ON CONFLICT(person_id, tag) DO UPDATE SET source='manual'", (dst, grp, src))
        conn.execute("DELETE FROM tags WHERE tag=? AND source='manual'", (src,))
        touch(conn, pids)
        conn.commit()
        return {'renamed': len(pids)}

    def api_tag_delete(self, conn, q, b):
        tag = clean_tag(b.get('tag'))
        if tag.casefold() in {label.casefold() for label in owner.RELATIONSHIPS.values()}:
            raise Bad('Edit relationships on the profile')
        pids = [r[0] for r in conn.execute("SELECT person_id FROM tags WHERE tag=? AND source='manual'", (tag,))]
        conn.execute("DELETE FROM tags WHERE tag=? AND source='manual'", (tag,))
        touch(conn, pids)
        conn.commit()
        return {'deleted': len(pids)}

    def api_rules(self, conn, q, b):
        return [rule_out(conn, r) for r in conn.execute('SELECT * FROM tag_rules ORDER BY tag, id')]

    def api_rule_add(self, conn, q, b):
        tag, field = clean_tag(b.get('tag')), b.get('field')
        match = b.get('match').strip() if isinstance(b.get('match'), str) else b.get('match')
        try:
            rules.compile_match(field, match)
        except ValueError as e:
            raise Bad(str(e)) from None
        grp = b.get('grp') if b.get('grp') in rules.GROUPS else tag_group(conn, tag)
        row = conn.execute('SELECT * FROM tag_rules WHERE tag=? AND field=? AND match=?', (tag, field, match)).fetchone()
        if row:
            return rule_out(conn, row)
        rule, started = {'tag': tag, 'grp': grp, 'field': field, 'match': match}, db.now()
        try:
            pids = rules.matching_ids(conn, rule)  # read-only scan: ingest keeps writing meanwhile
        except ValueError as e:
            raise Bad(str(e)) from None
        rid = conn.execute('INSERT INTO tag_rules(tag, grp, field, match, created_at) VALUES(?,?,?,?,?)',
                           (tag, grp, field, match, db.now())).lastrowid
        rules.write_rule_tags(conn, rule, pids)
        # people ingested while we scanned did not know this rule yet
        rules.sync(conn, [r[0] for r in conn.execute('SELECT id FROM people WHERE updated_at>=?', (started,))])
        conn.commit()
        return rule_out(conn, conn.execute('SELECT * FROM tag_rules WHERE id=?', (rid,)).fetchone())

    def api_rule_preview(self, conn, q, b):
        """How many people a rule would tag, without saving anything. Same matcher and guards as create."""
        rule = {'field': q.get('field', [''])[0], 'match': q.get('match', [''])[0].strip()}
        try:
            rules.compile_match(rule['field'], rule['match'])
            return {'hits': len(rules.matching_ids(conn, rule, budget=rules.PREVIEW_BUDGET))}
        except ValueError as e:
            raise Bad(str(e)) from None

    def api_rule_delete(self, conn, q, b, rid):
        row = conn.execute('SELECT * FROM tag_rules WHERE id=?', (rid,)).fetchone()
        if not row:
            return {'deleted': 0}
        keep = []
        for other in rules.load(conn):  # another rule may give the same tag: scan for it before taking the write lock
            if other['tag'] == row['tag'] and other['id'] != rid:
                try:
                    keep.append((other, rules.matching_ids(conn, other)))
                except ValueError:
                    pass
        conn.execute('DELETE FROM tag_rules WHERE id=?', (rid,))
        conn.execute("DELETE FROM tags WHERE tag=? AND source='rule'", (row['tag'],))
        for other, pids in keep:
            rules.write_rule_tags(conn, other, pids)
        conn.commit()
        return {'deleted': 1}

    def api_read(self, conn, q, b, pid):
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        person = person_row(conn, pid)
        handle = person['handle']
        if '~' in handle:  # parked row of an account that gave up this handle: there is no profile to read under it
            raise Bad('this account no longer has a handle to read')
        target = {'handle': handle, 'target_ig_id': person['ig_id']}
        accounts.coalesce_profile_jobs(conn, target)
        if not conn.execute("UPDATE jobs SET priority=? WHERE kind='profile' AND handle=? COLLATE NOCASE AND state IN ('queued','leased')",
                            (READ_PRIORITY, handle)).rowcount:
            conn.execute("INSERT INTO jobs(kind, handle, priority, created_at) VALUES('profile',?,?,?)", (handle, READ_PRIORITY, db.now()))
        accounts.coalesce_profile_jobs(conn, target)
        conn.commit()
        return {}
