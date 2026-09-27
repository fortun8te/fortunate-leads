"""Per-egress request gate: the replacement for the workspace-wide single-request gate.

Today (server/accounts.py request_permit + server.py ext_error) every Instagram request of every
account passes ONE global FIFO permit with 2 s spacing, and one account's limit sets ONE shared
`cooldown` that stops all accounts. That is the right call while all Chrome profiles exit through
the same home IP: Instagram sees one IP. It is also why 3 accounts ~= 1 account of throughput.

Once each lane has its own verified egress, the gate should be keyed by egress instead:
  * lanes on different verified egresses get independent permits and independent cooldowns;
  * lanes without a verified egress ("unrouted") keep today's behaviour: one shared permit and one
    shared cooldown between them (safe default, and the migration path);
  * a login/challenge on any lane still pauses only that lane (plus whatever the owner decides).
Pure state-in/state-out so it can live inside the existing settings JSON and transaction.
"""
UNROUTED = 'unrouted'


def group_of(lane, lane_egress):
    """lane_egress: lane_id -> verified egress id (or None)."""
    return lane_egress.get(lane) or UNROUTED


def cooldown_scope(lane, lane_egress):
    """Lanes that must share a cooldown with `lane` after it hits a limit."""
    g = group_of(lane, lane_egress)
    lanes = set(lane_egress) | {lane}
    return {x for x in lanes if group_of(x, lane_egress) == g}


def acquire(state, lane, lane_egress, now, spacing=2.0, lease=90.0):
    """-> (granted: bool, wait_s, state). state = {group: {'active': {...}|None, 'next_at': t, 'cool_until': t}}."""
    g = group_of(lane, lane_egress)
    s = dict(state.get(g) or {'active': None, 'next_at': 0.0, 'cool_until': 0.0})
    if s.get('cool_until', 0) > now:
        return False, s['cool_until'] - now, dict(state, **{g: s})
    act = s.get('active')
    if act and act['until'] <= now:
        act = None      # expired; the caller decides whether that needs owner attention
    if act:
        return False, spacing, dict(state, **{g: dict(s, active=act)})
    if s.get('next_at', 0) > now:
        return False, s['next_at'] - now, dict(state, **{g: s})
    s['active'] = {'lane': lane, 'until': now + lease}
    s['next_at'] = now + spacing
    return True, 0.0, dict(state, **{g: s})


def release(state, lane, lane_egress, now, spacing=2.0):
    g = group_of(lane, lane_egress)
    s = dict(state.get(g) or {})
    if s.get('active') and s['active']['lane'] == lane:
        s['active'] = None
        s['next_at'] = max(s.get('next_at', 0), now + spacing)
    return dict(state, **{g: s})


def hit(state, lane, lane_egress, until):
    """A rate limit on `lane`: cool its egress group only."""
    g = group_of(lane, lane_egress)
    s = dict(state.get(g) or {'active': None, 'next_at': 0.0, 'cool_until': 0.0})
    s['cool_until'] = max(s.get('cool_until', 0), until)
    return dict(state, **{g: s})
