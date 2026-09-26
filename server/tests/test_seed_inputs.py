"""Seed input must resolve to a real Instagram profile before work is queued."""
from test_server import Base, db


class SeedInputTests(Base):
    def test_profile_urls_and_handles_share_one_seed(self):
        inputs = ['@NASA', 'HTTPS://M.INSTAGRAM.COM/%4eASA/?hl=en',
                  'instagram.com/nasa#profile', 'https://www.instagr.am/NASA/']
        status, body = self.call('/api/scraper/seeds', {'handles': inputs, 'directions': ['followers']})
        self.assertEqual((status, body['queued']), (200, 1))
        self.assertEqual([r[0] for r in self.conn.execute('SELECT handle FROM seeds')], ['nasa'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_bad_inputs_do_not_create_work(self):
        bad = ['https://evil.test/?next=instagram.com/NASA', 'https://instagram.com.evil.test/nasa',
               'https://evil.test@instagram.com/nasa', 'https://www.instagram.com/p/abc',
               'https://www.instagram.com/reel/abc', 'https://www.instagram.com/nasa/extra',
               'https://www.instagram.com/%2fnasa', 'https://www.instagram.com/',
               'alice~123', 'a!b', 'ftp://instagram.com/nasa', 123]
        for value in bad:
            with self.subTest(value=value):
                status, _ = self.call('/api/scraper/seeds', {'handles': ['valid', value], 'directions': ['following']})
                self.assertEqual(status, 400)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM seeds').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_plain_digits_are_server_usernames_and_parked_identity_is_internal(self):
        self.assertEqual(db.norm_handle('alice~123'), 'alice~123')
        self.assertEqual(db.norm_handle('https://www.instagram.com/12345?hl=en'), '12345')
        with self.assertRaises(ValueError):
            db.queue_list(self.conn, 'alice~123', 'followers')
        status, body = self.call('/api/scraper/seeds', {'handles': ['12345'], 'directions': ['followers']})
        self.assertEqual((status, body['queued']), (200, 1))
        self.assertEqual(self.conn.execute('SELECT handle FROM seeds').fetchone()[0], '12345')


if __name__ == '__main__':
    import unittest
    unittest.main()
