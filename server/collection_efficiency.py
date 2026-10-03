"""Bounded, read-only list yield reporting. Rows are not unique people."""
from datetime import datetime, timedelta, timezone

MAX_EVENTS = 10000


def summary(conn, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=1)).isoformat()
    rows = conn.execute(
        "SELECT direction,returned_count,saved_entries,new_links FROM collector_events "
        "WHERE at>=? AND at<=? AND kind='list' AND outcome='page' "
        'ORDER BY at DESC,id DESC LIMIT ?', (since, now.isoformat(), MAX_EVENTS + 1)).fetchall()
    truncated = len(rows) > MAX_EVENTS
    groups = []
    for direction in ('following', 'followers'):
        pages = [r for r in rows[:MAX_EVENTS] if r['direction'] == direction]
        if not pages:
            continue
        returned = sum(r['returned_count'] or 0 for r in pages)
        saved = None if any(r['saved_entries'] is None for r in pages) else sum(r['saved_entries'] for r in pages)
        new_links = None if any(r['new_links'] is None for r in pages) else sum(r['new_links'] for r in pages)
        returned_known = all(r['returned_count'] is not None for r in pages)
        groups.append({'direction': direction, 'pages': len(pages),
                       'returned_entries': returned if returned_known else None,
                       'saved_entries': saved, 'new_connections': new_links,
                       'saved_per_page': round(saved / len(pages), 1) if saved is not None else None})
    return {'since': since, 'until': now.isoformat(), 'sampled': truncated,
            'scope': 'successful_list_pages', 'directions': groups}
