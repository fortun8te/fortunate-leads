import random
import unittest

import scale_helpers  # noqa: F401
from scale.pacing import HOUR, MIN, Pacer, list_pacer, main_account_pacer, profile_pacer, warmup_budget

T0 = 1_800_000_000.0


class PacerTest(unittest.TestCase):
    def pacer(self, **kw):
        base = dict(rate=6, min_rate=1, max_rate=10, step=1, increase_after=3, jitter=0, rng=random.Random(1))
        base.update(kw)
        return Pacer(**base)

    def test_spacing_follows_rate(self):
        p = self.pacer()
        self.assertTrue(p.try_acquire(T0))
        self.assertFalse(p.try_acquire(T0 + 9))
        self.assertTrue(p.try_acquire(T0 + 10))     # 6/min -> 10 s

    def test_additive_increase_capped(self):
        p = self.pacer()
        for _ in range(3 * 10):
            p.success(T0)
        self.assertEqual(p.rate, 10)

    def test_pushback_halves_and_escalates(self):
        p = self.pacer()
        end1 = p.pushback(T0)
        self.assertEqual(p.rate, 3)
        self.assertEqual(end1, T0 + 10 * MIN)
        self.assertFalse(p.try_acquire(T0 + 9 * MIN))
        end2 = p.pushback(T0 + 11 * MIN)
        self.assertEqual(end2, T0 + 11 * MIN + 20 * MIN)
        end3 = p.pushback(T0 + 40 * MIN)            # third strike in 24 h -> >= 2 h rest
        self.assertGreaterEqual(end3, T0 + 40 * MIN + 2 * HOUR)
        self.assertEqual(p.rate, 1)                 # floor

    def test_retry_after_wins(self):
        p = self.pacer()
        self.assertEqual(p.pushback(T0, retry_at=T0 + 3 * HOUR), T0 + 3 * HOUR)

    def test_window_caps_bursts(self):
        p = self.pacer(rate=60, max_rate=60, window_s=60, window_max=5)
        sent = sum(p.try_acquire(T0 + i) for i in range(60))
        self.assertEqual(sent, 5)

    def test_daily_budget_rolls_over(self):
        p = self.pacer(rate=60, max_rate=60, daily_budget=2)
        self.assertTrue(p.try_acquire(T0))
        self.assertTrue(p.try_acquire(T0 + 2))
        self.assertFalse(p.try_acquire(T0 + 4))
        next_day = (T0 // 86400 + 1) * 86400
        self.assertTrue(p.try_acquire(next_day + 1))

    def test_persistence_roundtrip(self):
        p = self.pacer()
        p.pushback(T0)
        q = self.pacer().load(p.to_dict())
        self.assertEqual(q.cool_until, p.cool_until)
        self.assertEqual(q.rate, p.rate)

    def test_presets(self):
        lp = list_pacer()
        self.assertLessEqual(lp.max_rate * 60, 390)   # never above the extension's 72/11 min ceiling
        self.assertEqual(profile_pacer().rate, 10)
        mp = main_account_pacer()
        self.assertLessEqual(mp.daily_budget, 100)
        mp.pushback(T0)
        self.assertGreaterEqual(mp.cool_until, T0 + 48 * HOUR)   # first strike = long rest

    def test_warmup_ramp(self):
        self.assertEqual(warmup_budget(0), 0)
        self.assertEqual(warmup_budget(2.9), 0)
        self.assertEqual(warmup_budget(3), 20)
        self.assertLess(warmup_budget(10), warmup_budget(21))
        self.assertEqual(warmup_budget(40), 3000)
        self.assertEqual(warmup_budget(40, clean_days=0), 1500)
        self.assertEqual(warmup_budget(40, target=500), 500)


if __name__ == '__main__':
    unittest.main()
