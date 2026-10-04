"""Offline checks for the manual private workspace snapshot."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


spec = importlib.util.spec_from_file_location('backup_workspace', Path(__file__).with_name('backup-workspace.py'))
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class BackupWorkspaceTests(unittest.TestCase):
    def test_complete_allowlisted_snapshot_and_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            data = repo / 'data'
            data.mkdir(parents=True)
            conn = sqlite3.connect(data / 'leads.sqlite')
            conn.execute('PRAGMA journal_mode=WAL')
            for name in ('people', 'seeds', 'lists', 'edges', 'accounts', 'settings'):
                conn.execute(f'CREATE TABLE {name}(value TEXT)')
            conn.execute("INSERT INTO seeds VALUES('friend')")
            conn.commit()
            for name in ('public-bios-cache.sqlite', 'llm_usage.sqlite'):
                with sqlite3.connect(data / name) as db:
                    db.execute('CREATE TABLE saved(value TEXT)')
                    db.execute("INSERT INTO saved VALUES('present')")
            for name in backup.DATA_DIRS:
                directory_path = data / name
                directory_path.mkdir()
                (directory_path / 'item.txt').write_text(name)
            (data / 'WORK_CHECKPOINT.md').write_text('paused at cursor')
            (data / 'openrouter.json').write_text('secret')
            (data / 'model.gguf').write_text('weight')
            (repo / 'ops').mkdir()
            (repo / 'ops/startup_profiles.json').write_text('{"chrome_profiles": []}')
            output = root / 'snapshot'
            backup.create_snapshot(repo, output)
            self.assertEqual(backup.verify_snapshot(output)['files'], 9)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual((output / 'data/leads.sqlite').stat().st_mode & 0o777, 0o600)
            self.assertFalse((output / 'data/openrouter.json').exists())
            self.assertFalse((output / 'data/model.gguf').exists())
            with sqlite3.connect(output / 'data/leads.sqlite') as restored:
                self.assertEqual(restored.execute('SELECT value FROM seeds').fetchone()[0], 'friend')
            self.assertEqual(json.loads((output / 'manifest.json').read_text())['format'],
                             'fortunate-leads-private-snapshot-v1')
            with self.assertRaises(FileExistsError):
                backup.create_snapshot(repo, output)
            (output / 'data/pfp/item.txt').write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                backup.verify_snapshot(output)
            conn.close()

    def make_repo(self, root):
        repo = root / 'repo'
        data = repo / 'data'
        (data / 'pfp').mkdir(parents=True)
        conn = sqlite3.connect(data / 'leads.sqlite')
        for name in ('people', 'seeds', 'lists', 'edges', 'accounts', 'settings'):
            conn.execute(f'CREATE TABLE {name}(value TEXT)')
        conn.execute("INSERT INTO people VALUES('ada')")
        conn.commit()
        conn.close()
        (data / 'pfp' / '1.jpg').write_bytes(b'\xff\xd8\xff' + b'x' * 64 + b'\xff\xd9')
        (repo / 'ops').mkdir()
        (repo / 'ops/startup_profiles.json').write_text('{"chrome_profiles": []}')
        return repo

    def test_restore_into_a_new_checkout_matches_the_snapshot_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.make_repo(root)
            output = root / 'snapshot'
            backup.create_snapshot(repo, output)
            fresh = root / 'new-checkout'
            result = backup.restore_snapshot(output, fresh)
            self.assertEqual(result['files'], 3)
            self.assertEqual((fresh / 'data/pfp/1.jpg').read_bytes(), (repo / 'data/pfp/1.jpg').read_bytes())
            with sqlite3.connect(fresh / 'data/leads.sqlite') as restored:
                self.assertEqual(restored.execute('SELECT value FROM people').fetchone()[0], 'ada')
            self.assertEqual((fresh / 'data/leads.sqlite').stat().st_mode & 0o777, 0o600)
            self.assertEqual((fresh / 'data').stat().st_mode & 0o777, 0o700)
            # a second restore into the same place must refuse, and change nothing
            before = (fresh / 'data/leads.sqlite').read_bytes()
            with self.assertRaisesRegex(FileExistsError, 'Refusing to overwrite'):
                backup.restore_snapshot(output, fresh)
            self.assertEqual((fresh / 'data/leads.sqlite').read_bytes(), before)
            with self.assertRaisesRegex(FileExistsError, 'Refusing to overwrite'):
                backup.restore_snapshot(output, repo)

    def test_restore_rejects_a_tampered_snapshot_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.make_repo(root)
            output = root / 'snapshot'
            backup.create_snapshot(repo, output)
            (output / 'data/pfp/1.jpg').write_bytes(b'tampered')
            fresh = root / 'new-checkout'
            with self.assertRaisesRegex(ValueError, 'checksum'):
                backup.restore_snapshot(output, fresh)
            self.assertFalse(fresh.exists())

    def test_restore_cleans_up_what_it_wrote_when_a_copy_fails(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.make_repo(root)
            output = root / 'snapshot'
            backup.create_snapshot(repo, output)
            fresh = root / 'new-checkout'
            real = backup.copy_file
            calls = []

            def flaky(source, target):
                calls.append(target)
                if len(calls) == 2:
                    raise OSError('disk full')
                return real(source, target)
            with mock.patch.object(backup, 'copy_file', side_effect=flaky):
                with self.assertRaises(OSError):
                    backup.restore_snapshot(output, fresh)
            self.assertFalse(fresh.exists())

    def test_symlink_in_data_is_rejected_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            pfp = repo / 'data/pfp'
            pfp.mkdir(parents=True)
            outside = root / 'outside.txt'
            outside.write_text('private')
            (pfp / 'linked.txt').symlink_to(outside)
            output = root / 'snapshot'
            with self.assertRaises(FileNotFoundError):
                backup.create_snapshot(repo, output)  # primary DB is mandatory
            with sqlite3.connect(repo / 'data/leads.sqlite') as db:
                for name in ('people', 'seeds', 'lists', 'edges', 'accounts', 'settings'):
                    db.execute(f'CREATE TABLE {name}(value TEXT)')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                backup.create_snapshot(repo, output)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
