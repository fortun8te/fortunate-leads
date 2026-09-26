import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('scraper_inputs', Path(__file__).resolve().parents[1] / 'app' / 'igscraper' / 'inputs.py')
inputs = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = inputs
spec.loader.exec_module(inputs)
parse_target, target_key = inputs.parse_target, inputs.target_key


class InputTests(unittest.TestCase):
    def test_profile_urls_are_canonical(self):
        for value in ('@NASA', 'https://m.instagram.com/%4eASA/?hl=en',
                      'HTTPS://WWW.INSTAGRAM.COM/NASA/#profile'):
            with self.subTest(value=value):
                self.assertEqual(target_key(value), 'u:nasa')
                self.assertEqual(parse_target(value).input_url, 'https://www.instagram.com/nasa')

    def test_numeric_id_contract(self):
        self.assertEqual(parse_target('12345').user_id, '12345')
        self.assertEqual(parse_target('https://www.instagram.com/_u/12345').user_id, '12345')
        self.assertEqual(parse_target('https://www.instagram.com/12345').username, '12345')

    def test_non_profiles_are_rejected(self):
        for value in ('https://evil.test/?next=instagram.com/nasa', 'https://instagram.com.evil.test/nasa',
                      'https://evil.test@instagram.com/nasa', 'https://www.instagram.com/p/abc',
                      'https://www.instagram.com/nasa/extra', 'https://www.instagram.com/%2fnasa',
                      'https://www.instagram.com/_u/not-an-id', 'https://www.instagram.com/'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_target(value)


if __name__ == '__main__':
    unittest.main()
