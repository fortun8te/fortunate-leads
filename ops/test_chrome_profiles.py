import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chrome_profiles


class ChromeProfilesTest(unittest.TestCase):
    def test_only_named_profiles_are_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'configured.json'
            config.write_text(json.dumps({'chrome_profiles': ['BOT', 'BOT2']}), encoding='utf-8')
            state = root / 'Local State'
            state.write_text(json.dumps({'profile': {'info_cache': {
                'Profile 2': {'name': 'Michael'}, 'Profile 5': {'name': 'BOT'},
                'Profile 6': {'name': 'BOT2'}, 'Profile 7': {'name': 'Unconfigured'},
            }}}), encoding='utf-8')
            self.assertEqual(chrome_profiles.resolve_profiles(state, config), ['Profile 5', 'Profile 6'])

    def test_missing_ambiguous_or_duplicate_profiles_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'configured.json'
            state = root / 'Local State'
            state.write_text(json.dumps({'profile': {'info_cache': {
                'Profile 5': {'name': 'BOT'}, 'Profile 6': {'name': 'BOT'},
            }}}), encoding='utf-8')
            config.write_text(json.dumps({'chrome_profiles': ['BOT']}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                chrome_profiles.resolve_profiles(state, config)
            config.write_text(json.dumps({'chrome_profiles': ['BOT', 'BOT']}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'unique names'):
                chrome_profiles.resolve_profiles(state, config)

    def test_checked_in_configuration_matches_the_existing_launcher_scope(self):
        self.assertEqual(chrome_profiles.configured_names(), ['Michael', 'BOT', 'BOT2'])
        self.assertEqual(chrome_profiles.configured_directories(), ['Profile 2', 'Profile 5', 'Profile 6'])

    def test_configured_directories_reject_duplicate_or_mismatched_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'configured.json'
            config.write_text(json.dumps({'chrome_profiles': ['BOT', 'BOT2'],
                                          'chrome_profile_dirs': ['Profile 5', 'Profile 5']}))
            with self.assertRaisesRegex(ValueError, 'uniquely match'):
                chrome_profiles.configured_directories(config)


if __name__ == '__main__':
    unittest.main()
