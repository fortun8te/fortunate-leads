"""Clock storage ownership; writes remain in the caller transaction."""

from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def utc_now():
    return datetime.now(timezone.utc)
