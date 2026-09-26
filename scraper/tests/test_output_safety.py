"""Local export fidelity and safety checks; no account or network access."""

import csv
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from igscraper.output import to_csv, to_json, to_jsonl, write_stream
from igscrape_app.runner import write_outputs


class OutputSafetyTests(unittest.TestCase):
    def test_csv_disarms_formula_text_in_values_and_headers(self):
        item = {
            "username": "=HYPERLINK(\"https://example.invalid\")",
            "fullName": "\t+1+1",
            "biography": "-cmd\nsecond line",
            "website": "@SUM(1,2)",
            "=dynamicField": "ordinary value",
            "followersCount": 0,
            "postsCount": None,
        }
        row = next(csv.reader(io.StringIO(to_csv([item]))))
        self.assertIn("'=dynamicField", row)
        parsed = next(csv.DictReader(io.StringIO(to_csv([item]))))
        self.assertEqual(parsed["username"], "'" + item["username"])
        self.assertEqual(parsed["fullName"], "'" + item["fullName"])
        self.assertEqual(parsed["biography"], "'" + item["biography"])
        self.assertEqual(parsed["website"], "'" + item["website"])
        self.assertEqual(parsed["followersCount"], "0")
        self.assertEqual(parsed["postsCount"], "")
        self.assertEqual(json.loads(to_json([item]))[0], item)
        self.assertEqual(json.loads(to_jsonl([item])), item)

    def test_unicode_and_multiline_text_round_trip(self):
        item = {"username": "東京", "biography": 'café\nأهلاً, "quoted" ✨', "followersCount": 0}
        parsed = next(csv.DictReader(io.StringIO(to_csv([item]))))
        self.assertEqual(parsed["biography"], item["biography"])
        self.assertEqual(parsed["username"], item["username"])
        self.assertEqual(parsed["followersCount"], "0")

    def test_concurrent_replacement_never_exposes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            barrier = threading.Barrier(3)
            errors = []
            payloads = [[{"username": name, "biography": name * 200_000}]
                        for name in ("a", "b")]

            def write(items):
                try:
                    barrier.wait()
                    write_outputs(items, str(path))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=write, args=(items,)) for items in payloads]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            self.assertFalse(errors)
            self.assertIn(json.loads(path.read_text(encoding="utf-8")), payloads)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_same_destination_cannot_overwrite_json_with_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            with self.assertRaisesRegex(ValueError, "different file paths"):
                write_outputs([{"username": "a"}], str(path), str(path))
            self.assertFalse(path.exists())

    def test_streaming_csv_scans_all_rows_and_late_columns(self):
        calls = []
        def rows():
            calls.append(True)
            for index in range(1001):
                item = {'id': index, 'name': '東京'}
                if index == 1000:
                    item['late'] = '\ufeff=1+1'
                yield item
        output = io.StringIO(newline='')
        self.assertEqual(write_stream(rows, output, 'csv'), 1001)
        self.assertEqual(len(calls), 2)
        parsed = list(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual(len(parsed), 1001)
        self.assertEqual(parsed[-1]['late'], "'\ufeff=1+1")
        self.assertEqual(parsed[0]['id'], '0')

    def test_json_stream_consumes_once_and_preserves_null(self):
        for fmt in ('json', 'jsonl'):
            calls = []
            def rows():
                calls.append(True)
                yield {'zero': 0, 'unknown': None, 'name': 'أهلاً'}
            output = io.StringIO()
            self.assertEqual(write_stream(rows, output, fmt), 1)
            self.assertEqual(len(calls), 1)
            parsed = json.loads(output.getvalue())
            self.assertEqual(parsed if fmt == 'jsonl' else parsed[0],
                             {'zero': 0, 'unknown': None, 'name': 'أهلاً'})

    def test_ambiguous_csv_paths_and_nonfinite_json_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'collision'):
            to_csv([{'a/b': 'first', 'a': {'b': 'second'}}])
        for renderer in (to_json, to_jsonl):
            with self.assertRaises(ValueError):
                renderer([{'count': float('nan')}])

    def test_csv_rejects_columns_added_between_passes(self):
        rows = iter([[{'id': 1}], [{'id': 1, 'new': 'lost'}]])
        with self.assertRaises(ValueError):
            write_stream(lambda: iter(next(rows)), io.StringIO(), 'csv')


if __name__ == "__main__":
    unittest.main()
