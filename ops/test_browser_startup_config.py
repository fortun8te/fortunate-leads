import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('configure_browser_startup', Path(__file__).with_name('configure-browser-startup.py'))
configurer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configurer)


class ConfigureBrowserStartupTests(unittest.TestCase):
    def test_only_existing_profiles_and_known_accounts_can_be_bound(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'data').mkdir()
            chrome = root / 'chrome'
            (chrome / 'Profile 2').mkdir(parents=True)
            (chrome / 'Profile 2/Preferences').write_text('{}')
            with sqlite3.connect(root / 'data/leads.sqlite') as conn:
                conn.execute('CREATE TABLE accounts(lane_id TEXT, ig_id TEXT, collection_backend TEXT)')
                conn.execute("INSERT INTO accounts VALUES ('lane1','100','chrome')")
            self.assertEqual(configurer.configure(root, ['Profile 2=lane1'], chrome), 1)
            path = root / 'data/browser-startup.json'
            initial = path.read_text()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for invalid in (['Profile 2=other'], ['Profile 9=lane1'], ['../Profile 2=lane1'], ['Profile 2=lane1'] * 2):
                with self.assertRaises(ValueError):
                    configurer.configure(root, invalid, chrome)
                self.assertEqual(path.read_text(), initial)
            configurer.configure(root, [], chrome, enabled=False)
            self.assertFalse(json.loads(path.read_text())['enabled'])


if __name__ == '__main__':
    unittest.main()
