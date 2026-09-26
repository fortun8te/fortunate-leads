"""Broad inputs cannot contain the Grok verdict they are trained to predict."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'server'))
sys.path.insert(0, str(ROOT / 'sidecar'))
import broad_features  # noqa: E402
import train_broad  # noqa: E402


class BroadFeatureProvenance(unittest.TestCase):
    def test_teacher_verdict_and_auto_tags_do_not_become_features(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.sqlite'
            con = sqlite3.connect(path)
            con.executescript('''
                CREATE TABLE people(id INT, handle TEXT, name TEXT, category TEXT, bio TEXT, followers INT);
                CREATE TABLE verdicts(person_id INT, model TEXT, role TEXT, content_fit INT, score INT);
                CREATE TABLE marks(person_id INT, status TEXT);
                CREATE TABLE tags(person_id INT, tag TEXT, source TEXT);
                INSERT INTO people VALUES(1, 'candleshop', 'Candle Shop', NULL, 'Handmade candles. Shop now.', 1200);
                INSERT INTO verdicts VALUES(1, 'grok-4.7', 'buyer', 95, 95);
                INSERT INTO tags VALUES(1, 'AI: Fit strong', 'auto');
            ''')
            con.commit()
            with patch.object(train_broad.broad, 'HEAD', Path(directory) / 'broad_head.pkl'):
                people, rules, tags = train_broad.labels(path)
                self.assertEqual((rules[0], tags[0]), broad_features.from_profile(people[0]))
                con.execute("UPDATE verdicts SET content_fit=5, score=5 WHERE person_id=1")
                con.execute("INSERT INTO tags VALUES(1, 'Agency', 'auto')")
                con.commit()
                _, after_rules, after_tags = train_broad.labels(path)
            self.assertEqual((rules, tags), (after_rules, after_tags))
            self.assertNotIn('AI: Fit strong', tags[0])
            self.assertNotIn('Agency', tags[0])
            con.close()


if __name__ == '__main__':
    unittest.main()
