"""Numeric boundary cases for untrusted usage headers."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import biofetch


class UsageNumberTests(unittest.TestCase):
    def test_non_numeric_and_non_finite_metrics_are_ignored(self):
        for value in (True, False, None, [], {}, '80', float('nan'), float('inf'), -float('inf')):
            with self.subTest(value=value):
                header = json.dumps({'call_count': value, 'total_time': 12})
                self.assertEqual(biofetch.usage_pct({'x-app-usage': header}), 12)

    def test_large_integer_metric_does_not_overflow_or_hide_high_usage(self):
        value = 10 ** 400
        self.assertEqual(biofetch.usage_pct({'x-app-usage': json.dumps({'call_count': value})}), value)


if __name__ == '__main__':
    unittest.main()
