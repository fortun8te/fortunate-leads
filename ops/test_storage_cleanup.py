import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('storage_cleanup', Path(__file__).with_name('storage_cleanup.py'))
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def test_preserves_latest_three_and_seven_dates(self):
        paths = [Path(f'leads-202609{day:02d}-{hour:02d}0000-1-1.sqlite')
                 for day in range(1, 11) for hour in range(5)]
        keep = cleanup.retained_backups(paths)
        self.assertEqual(len(keep), 9)
        self.assertTrue(set(sorted(paths)[-3:]) <= keep)
        self.assertEqual(len({p.name[6:14] for p in keep}), 7)

    def test_named_symlink_and_active_backups_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            root = data / 'backups'
            root.mkdir()
            for hour in range(6):
                (root / f'leads-20260927-{hour:02d}0000-1-1.sqlite').write_bytes(b'data')
            named = root / 'leads-before-release.sqlite'
            named.write_bytes(b'data')
            (root / 'leads-20260927-000000-1-1.sqlite-wal').touch()
            (root / 'leads-20260926-000000-1-1.sqlite').symlink_to(named)
            keep, entries = cleanup.plan(data)
            self.assertEqual(len(entries), 2)
            self.assertTrue(all('20260927-0' in e['path'] for e in entries))
            self.assertTrue(named.exists())

    def test_changed_file_refuses_entire_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.sqlite'
            path.write_bytes(b'old')
            s = path.stat()
            entry = {'path': str(path), 'reason': 'synthetic map benchmark',
                     'identity': [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]}
            path.write_bytes(b'changed')
            with patch.object(cleanup, 'open_files', return_value=False):
                with self.assertRaises(RuntimeError):
                    cleanup.apply([], [entry])
            self.assertTrue(path.exists())

    def test_open_file_refuses_deletion(self):
        with patch.object(cleanup, 'open_files', return_value=True):
            with self.assertRaises(RuntimeError):
                cleanup.apply([], [])


if __name__ == '__main__':
    unittest.main()
