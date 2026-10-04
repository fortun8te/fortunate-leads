import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
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


PROTECTED = ('leads.sqlite', 'leads.sqlite-wal', 'leads.sqlite-shm', 'llm_usage.sqlite', 'public-bios-cache.sqlite',
             'openrouter.json', 'openrouter.state.json', 'browser-startup.json', '.server-source.sha256',
             'WORK_CHECKPOINT.md', 'agent-plan.json')
DAY = 86400


def make_db(path, rows=3):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE people(id INTEGER PRIMARY KEY)')
    db.execute('CREATE TABLE edges(x)')
    db.execute('CREATE TABLE tags(x)')
    db.executemany('INSERT INTO people VALUES(?)', [(i,) for i in range(rows)])
    db.commit()
    db.close()


def age(path, days, now):
    stamp = now - days * DAY
    os.utime(path, (stamp, stamp))


class TierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        self.backups = self.data / 'backups'
        self.backups.mkdir(parents=True)
        self.now = time.time()
        for name in PROTECTED:
            (self.data / name).write_bytes(b'precious ' + name.encode())
        (self.data / 'pfp').mkdir()
        (self.data / 'pfp' / '1.jpg').write_bytes(b'photo')
        (self.data / 'reviews').mkdir()
        (self.data / 'reviews' / 'r.json').write_text('{}')
        # three generated backups, the newest healthy
        for hour, days in ((1, 3), (2, 2), (3, 1)):
            path = self.backups / f'leads-202610{10 - days:02d}-0{hour}0000-1-1.sqlite'
            make_db(path)
            age(path, days, self.now)
        # named checkpoints of different ages
        for name, days in (('before-a.sqlite', 20), ('before-b.sqlite', 10), ('before-c.sqlite', 2)):
            make_db(self.backups / name)
            age(self.backups / name, days, self.now)
            (self.backups / (name + '-wal')).write_bytes(b'')
            (self.backups / (name + '-shm')).write_bytes(b'x' * 32)
        for name in ('leads.backup-1790274018.sqlite',):
            make_db(self.data / name)
        make_db(self.backups / 'leads-20260925-0331.sqlite')
        (self.data / 'tor').mkdir()
        (self.data / 'tor' / 'config').write_text('x')
        (self.data / 'scratch').mkdir()
        (self.data / 'scratch' / 'sub').mkdir()
        (self.data / 'scratch' / 'sub' / 'a.txt').write_text('x')
        (self.data / 'benchmark-live-32.json').write_text('{}')
        (self.data / 'leads.sqlite.map').mkdir()
        (self.data / 'leads.sqlite.map' / 'closeness.sqlite').write_bytes(b'cache')
        (self.backups / 'leads-20260926-0222.sqlite.partial-shm').write_bytes(b'x')

    def names(self, entries):
        return sorted(Path(e['path']).name for e in entries)

    def test_inventory_classifies_every_entry_and_flags_unknown_files(self):
        (self.data / 'mystery.bin').write_bytes(b'?')
        rows, backups = cleanup.inventory(self.data)
        by_name = {r['name']: r['class'] for r in rows}
        self.assertEqual(by_name['leads.sqlite'], 'irreplaceable')
        self.assertEqual(by_name['openrouter.json'], 'secret')
        self.assertEqual(by_name['public-bios-cache.sqlite'], 'slow-to-rebuild')
        self.assertEqual(by_name['pfp/'], 'slow-to-rebuild')
        self.assertEqual(by_name['tor/'], 'disposable')
        self.assertEqual(by_name['leads.sqlite.map/'], 'regenerable')
        self.assertEqual(by_name['leads.backup-1790274018.sqlite'], 'backup')
        self.assertEqual(by_name['mystery.bin'], 'unknown')
        kinds = {b['name']: b['kind'] for b in backups}
        self.assertEqual(kinds['before-a.sqlite'], 'checkpoint')
        self.assertEqual(kinds['before-a.sqlite-wal'], 'sidecar')
        self.assertEqual(kinds['leads-20260925-0331.sqlite'], 'legacy-dated')

    def test_no_tier_ever_lists_protected_data(self):
        every = {'legacy', 'checkpoints', 'disposable', 'regenerable', 'gzip-twins'}
        entries = cleanup.tier_plan(self.data, every, checkpoint_days=0, now=self.now)
        listed = {Path(e['path']).name for e in entries}
        self.assertFalse(listed & set(PROTECTED))
        for e in entries:
            self.assertNotIn('/pfp/', e['path'])
            self.assertNotIn('/reviews/', e['path'])
        self.assertTrue((self.data / 'pfp' / '1.jpg').exists())

    def test_checkpoints_need_age_a_newer_generated_backup_and_keep_the_newest(self):
        entries = cleanup.tier_plan(self.data, {'checkpoints'}, checkpoint_days=5, now=self.now)
        self.assertEqual(self.names(entries), ['before-a.sqlite', 'before-a.sqlite-shm', 'before-a.sqlite-wal',
                                               'before-b.sqlite', 'before-b.sqlite-shm', 'before-b.sqlite-wal'])
        self.assertEqual(cleanup.tier_plan(self.data, {'checkpoints'}, checkpoint_days=30, now=self.now), [])
        # with a huge age gate nothing, and with 0 days the newest checkpoint is still kept
        names = self.names(cleanup.tier_plan(self.data, {'checkpoints'}, checkpoint_days=0, now=self.now))
        self.assertNotIn('before-c.sqlite', names)
        # a checkpoint with unreplayed WAL content is left for a person
        (self.backups / 'before-a.sqlite-wal').write_bytes(b'unreplayed')
        names = self.names(cleanup.tier_plan(self.data, {'checkpoints'}, checkpoint_days=5, now=self.now))
        self.assertNotIn('before-a.sqlite', names)
        # and none are listed without a generated backup newer than the checkpoint
        for path in list(self.backups.glob('leads-2026100*.sqlite')):
            age(path, 30, self.now)
        self.assertEqual(cleanup.tier_plan(self.data, {'checkpoints'}, checkpoint_days=5, now=self.now), [])

    def test_legacy_disposable_and_regenerable_tiers(self):
        self.assertEqual(self.names(cleanup.tier_plan(self.data, {'legacy'})),
                         ['leads-20260925-0331.sqlite', 'leads.backup-1790274018.sqlite'])
        disposable = self.names(cleanup.tier_plan(self.data, {'disposable'}))
        self.assertEqual(disposable, ['a.txt', 'benchmark-live-32.json', 'config',
                                      'leads-20260926-0222.sqlite.partial-shm'])
        self.assertEqual(self.names(cleanup.tier_plan(self.data, {'regenerable'})), ['closeness.sqlite'])
        (self.data / 'tor' / 'link').symlink_to(self.data / 'leads.sqlite')
        self.assertNotIn('config', self.names(cleanup.tier_plan(self.data, {'disposable'})))   # a tree with a symlink is left alone

    def test_gzip_twin_tier_only_lists_originals_whose_copy_verifies(self):
        original = self.backups / 'before-c.sqlite'
        good = Path(str(original) + '.gz')
        with open(original, 'rb') as src, gzip.open(good, 'wb') as out:
            out.write(src.read())
        self.assertEqual(self.names(cleanup.tier_plan(self.data, {'gzip-twins'})), ['before-c.sqlite'])
        original.write_bytes(original.read_bytes() + b'changed after compression')
        self.assertEqual(cleanup.tier_plan(self.data, {'gzip-twins'}), [])
        good.write_bytes(b'not gzip at all')
        self.assertEqual(cleanup.tier_plan(self.data, {'gzip-twins'}), [])

    def test_apply_removes_listed_files_tidies_only_emptied_disposable_folders(self):
        keep, entries = cleanup.plan(self.data)
        extra = cleanup.tier_plan(self.data, {'legacy', 'checkpoints', 'disposable', 'regenerable'},
                                  checkpoint_days=5, now=self.now)
        everything = entries + extra
        before = {p.name for p in self.data.iterdir()}
        with patch.object(cleanup, 'open_files', return_value=False):
            cleanup.apply(keep, everything)
        after = {p.name for p in self.data.iterdir()}
        self.assertEqual(before - after, {'tor', 'scratch', 'benchmark-live-32.json', 'leads.sqlite.map',
                                          'leads.backup-1790274018.sqlite'})
        self.assertTrue(self.backups.is_dir())
        self.assertTrue((self.data / 'pfp' / '1.jpg').exists())
        for name in PROTECTED:
            self.assertTrue((self.data / name).exists(), name)
        self.assertFalse((self.backups / 'before-a.sqlite').exists())
        self.assertTrue((self.backups / 'before-c.sqlite').exists())

    def test_apply_refuses_to_remove_backups_when_the_newest_generated_backup_is_damaged(self):
        newest = sorted(self.backups.glob('leads-2026100*-1-1.sqlite'))[-1]
        newest.write_bytes(newest.read_bytes()[:2048])
        keep, entries = cleanup.plan(self.data)
        extra = cleanup.tier_plan(self.data, {'checkpoints'}, checkpoint_days=5, now=self.now)
        before = sorted(p.name for p in self.backups.iterdir())
        with patch.object(cleanup, 'open_files', return_value=False):
            with self.assertRaises((RuntimeError, sqlite3.DatabaseError)):
                cleanup.apply(keep, extra)
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()), before)

    def test_compress_plan_never_touches_the_live_database(self):
        rows = cleanup.compress_plan(self.data, now=self.now)
        names = {Path(r['path']).name for r in rows}
        self.assertFalse(names & set(PROTECTED) - {'public-bios-cache.sqlite'})
        self.assertIn('before-a.sqlite', names)
        self.assertNotIn('leads-20261009-030000-1-1.sqlite', names)    # newest daily is under two days old
        self.assertTrue(all(0 < r['ratio'] <= 1.5 for r in rows))

    def test_command_line_inventory_and_default_run_change_nothing(self):
        listing = sorted((p.name, p.stat().st_mtime_ns) for p in self.data.rglob('*'))
        script = Path(__file__).with_name('storage_cleanup.py')
        result = subprocess.run([sys.executable, str(script), '--data', str(self.data), '--inventory', '--compress-plan',
                                 '--checkpoint-days', '5'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('irreplaceable', result.stdout)
        self.assertIn('--include-old-checkpoints', result.stdout)
        result = subprocess.run([sys.executable, str(script), '--data', str(self.data), '--include-old-checkpoints'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['applied'])
        self.assertEqual(sorted((p.name, p.stat().st_mtime_ns) for p in self.data.rglob('*')), listing)


if __name__ == '__main__':
    unittest.main()
