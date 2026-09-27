"""Adaptive per-identity pacing: AIMD rate + sliding window + daily budget + escalating cooldowns.

One `Pacer` per (identity, request kind): e.g. (bot account "b1", "list"), (egress "tor-3", "profile").
Instagram's risk model scores call *types* separately (instagrapi guide, RESEARCH.md section 3),
so list pages and profile reads never share a pacer.

Behaviour
  * additive increase: after `increase_after` clean requests the rate rises by `step` req/min,
    never above `max_rate`;
  * multiplicative decrease on push-back (429, please wait, feedback_required, login wall):
    rate *= `decrease`, never below `min_rate`, plus a cooldown of `cooldown_base` doubling per
    hit inside 24 h (cap `cooldown_cap`); a provider Retry-After always wins; `strikes` hits inside
    24 h force at least `strike_pause` of rest (same policy as the extension's PACE);
  * a sliding window caps bursts (`window_max` requests per `window_s`);
  * an optional daily budget in the identity's local day.
Everything takes `now` (epoch seconds) so tests are deterministic.
"""
import random
import time

MIN = 60.0
HOUR = 3600.0
DAY = 86400.0


class Pacer:
    def __init__(self, rate=6.0, min_rate=1.0, max_rate=15.0, step=1.0, increase_after=20,
                 decrease=0.5, jitter=0.35, window_s=11 * MIN, window_max=None, daily_budget=0,
                 cooldown_base=10 * MIN, cooldown_cap=6 * HOUR, strikes=3, strike_pause=2 * HOUR,
                 rng=None, day_offset_s=0):
        assert 0 < min_rate <= rate <= max_rate
        self.rate, self.min_rate, self.max_rate, self.step = float(rate), float(min_rate), float(max_rate), float(step)
        self.increase_after, self.decrease, self.jitter = int(increase_after), float(decrease), float(jitter)
        self.window_s, self.window_max, self.daily_budget = float(window_s), window_max, int(daily_budget or 0)
        self.cooldown_base, self.cooldown_cap = float(cooldown_base), float(cooldown_cap)
        self.strikes, self.strike_pause = int(strikes), float(strike_pause)
        self.rng = rng or random.Random()
        self.day_offset_s = day_offset_s          # local-time offset for the daily budget rollover
        self.next_at = 0.0
        self.cool_until = 0.0
        self.hits = []                            # push-back times (24 h)
        self.log = []                             # request times (window)
        self.clean = 0                            # clean requests since the last change
        self.day = None
        self.today = 0
        self.total = 0

    # ---- queries -------------------------------------------------------
    def _roll(self, now):
        d = int((now + self.day_offset_s) // DAY)
        if d != self.day:
            self.day, self.today = d, 0

    def _window_release(self, now):
        if not self.window_max:
            return 0.0
        self.log = [t for t in self.log if now - t < self.window_s]
        if len(self.log) < self.window_max:
            return 0.0
        return self.log[len(self.log) - self.window_max] + self.window_s

    def ready_at(self, now=None):
        now = time.time() if now is None else now
        self._roll(now)
        at = max(self.next_at, self.cool_until, self._window_release(now))
        if self.daily_budget and self.today >= self.daily_budget:
            next_day = ((now + self.day_offset_s) // DAY + 1) * DAY - self.day_offset_s
            at = max(at, next_day)
        return at

    def cooling(self, now=None):
        now = time.time() if now is None else now
        return self.cool_until > now

    # ---- events --------------------------------------------------------
    def try_acquire(self, now=None):
        """Record a request if one may go now. Returns True when the caller may send it."""
        now = time.time() if now is None else now
        if self.ready_at(now) > now:
            return False
        gap = 60.0 / self.rate
        lo, hi = gap * (1 - self.jitter), gap * (1 + self.jitter)
        self.next_at = now + self.rng.uniform(lo, hi)
        self.log.append(now)
        self.today += 1
        self.total += 1
        return True

    def success(self, now=None):
        self.clean += 1
        if self.clean >= self.increase_after and self.rate < self.max_rate:
            self.rate = min(self.max_rate, self.rate + self.step)
            self.clean = 0

    def pushback(self, now=None, retry_at=None):
        """429 / please-wait / feedback_required / login wall. Returns the cooldown end."""
        now = time.time() if now is None else now
        self.hits = [t for t in self.hits if now - t < DAY] + [now]
        n = len(self.hits)
        self.rate = max(self.min_rate, self.rate * self.decrease)
        self.clean = 0
        until = now + min(self.cooldown_base * 2 ** (n - 1), self.cooldown_cap)
        if retry_at and retry_at > until:
            until = retry_at
        if n >= self.strikes:
            until = max(until, now + self.strike_pause)
        self.cool_until = max(self.cool_until, until)
        return self.cool_until

    def soft_error(self, now=None, delay=2 * MIN):
        """Unknown answer / transport trouble: short local delay, rate unchanged, not a strike."""
        now = time.time() if now is None else now
        self.next_at = max(self.next_at, now + delay)

    # ---- persistence ---------------------------------------------------
    STATE = ('rate', 'next_at', 'cool_until', 'hits', 'clean', 'day', 'today', 'total')

    def to_dict(self):
        return {k: getattr(self, k) for k in self.STATE}

    def load(self, d):
        for k in self.STATE:
            if k in (d or {}):
                setattr(self, k, d[k])
        self.rate = min(self.max_rate, max(self.min_rate, float(self.rate)))
        return self

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        return {'rate_per_min': round(self.rate, 2), 'ready_in_s': max(0, round(self.ready_at(now) - now, 1)),
                'cooling_until': self.cool_until if self.cool_until > now else None,
                'hits_24h': len([t for t in self.hits if now - t < DAY]), 'today': self.today,
                'daily_budget': self.daily_budget or None, 'total': self.total}


# ---- presets -------------------------------------------------------------
# Logged-in list pages per bot account. Ceiling matches the extension's 72 requests / 11 min
# window (~390/h) and instaloader's 199 / 30 min mobile-API throttle; the start rate is well
# below it and only climbs while the account stays clean. These are starting points to tune
# from measured soak data, not safe-rate claims.
def list_pacer(daily_budget=3000, rng=None, warm_rate=None):
    start = warm_rate or 3.0
    return Pacer(rate=start, min_rate=0.5, max_rate=max(start, 6.0), step=0.5, increase_after=40,
                 decrease=0.5, jitter=0.4, window_s=11 * MIN, window_max=60, daily_budget=daily_budget, rng=rng)


# Logged-out profile reads per egress unit (Tor circuit, IPv6 /64, phone). Measured 2026-09-24
# with igscraper: start 10, cap 15 req/min per circuit gave ~8-9 req/min sustained.
def profile_pacer(rng=None, start=10.0, cap=15.0):
    return Pacer(rate=start, min_rate=2.0, max_rate=cap, step=1.0, increase_after=25, decrease=0.6,
                 jitter=0.3, window_s=10 * MIN, window_max=int(cap * 10), cooldown_base=5 * MIN,
                 cooldown_cap=2 * HOUR, strikes=4, strike_pause=1 * HOUR, rng=rng)


# Main account: only when explicitly enabled, and then tiny.
def main_account_pacer(rng=None):
    return Pacer(rate=0.5, min_rate=0.25, max_rate=0.5, step=0.0, increase_after=10 ** 9, decrease=0.5,
                 window_s=HOUR, window_max=20, daily_budget=100, cooldown_base=6 * HOUR,
                 cooldown_cap=48 * HOUR, strikes=1, strike_pause=48 * HOUR, rng=rng)


def warmup_budget(age_days, clean_days=None, target=3000):
    """Daily list-page budget for a new bot account by age (days since creation).

    Practitioner guidance, not a measured Instagram rule: no scraping for the first days,
    then a ramp while the account stays clean. `clean_days` = consecutive days without a
    push-back; any hit resets the ramp to the previous stage (the caller passes 0).
    """
    if age_days < 3:
        return 0
    stages = [(3, 20), (5, 50), (7, 100), (10, 200), (14, 400), (21, 800), (28, 1500)]
    budget = 0
    for day, pages in stages:
        if age_days >= day:
            budget = pages
    if age_days >= 35:
        budget = target
    if clean_days is not None and clean_days < 2:
        budget = max(20, budget // 2)
    return min(budget, target)
