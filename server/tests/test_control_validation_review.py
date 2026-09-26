import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import server


class ControlValidationReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'leads.sqlite')

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_pause_requires_explicit_boolean_without_mutation(self):
        db.set_setting(self.conn, 'paused', True)
        self.conn.commit()
        for body in ({}, {'paused': 'false'}, {'paused': 0}, {'paused': None}):
            with self.assertRaises(server.Bad):
                server.api_pause(self.conn, {}, body)
            self.assertTrue(db.get_setting(self.conn, 'paused'))
        server.api_pause(self.conn, {}, {'paused': False})
        self.assertFalse(db.get_setting(self.conn, 'paused'))

    def test_invalid_budget_cannot_silently_remove_limit(self):
        before = db.get_setting(self.conn, 'budget')
        for value in (False, True, -1, '-1', '10', 1.5, None, 999999):
            with self.assertRaises(server.Bad):
                server.api_budget(self.conn, {}, {'list': 99, 'profile': value})
            self.assertEqual(db.get_setting(self.conn, 'budget'), before)
        server.api_budget(self.conn, {}, {'profile': 0})
        self.assertEqual(db.get_setting(self.conn, 'budget')['profile'], 0)


if __name__ == '__main__':
    unittest.main()
