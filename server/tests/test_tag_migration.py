"""Exercise taxonomy migration with real qualification rules and cached verdicts."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('FL_NO_ORSLOT', '1')
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
import db
import server

# test_server installs a qualify stub globally. Load the real rules separately so
# this regression has the same behavior alone and in the complete test suite.
spec = importlib.util.spec_from_file_location('migration_qualify', SERVER_DIR / 'qualify.py')
real_qualify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(real_qualify)


class TagMigrationTest(unittest.TestCase):
    def test_already_current_version_repairs_and_commits_stale_auto_claim(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'qualify', real_qualify):
            path = str(Path(directory) / 'leads.sqlite')
            conn = db.init(path)
            self.addCleanup(conn.close)
            db.set_setting(conn, 'tags_version', real_qualify.TAGS_VERSION)
            for pid, source in [(1, 'auto'), (2, 'manual')]:
                conn.execute('INSERT INTO people(id,handle,first_seen,updated_at) VALUES(?,?,?,?)',
                             (pid, 'person' + str(pid), '2026-01-01', '2026-01-01'))
                conn.execute('INSERT INTO tags VALUES(?,?,?,?)', (pid, 'knows you', 'source', source))
                conn.execute("INSERT INTO tags VALUES(?,'Already know them','signal','manual')", (pid,))
                conn.execute("INSERT INTO tags VALUES(?,'AI: Has online shop','ai','auto')", (pid,))
                conn.execute("INSERT INTO verdicts(person_id,model,input_hash,score,updated_at) VALUES(?,'cached-model','hash',91,'2026-01-01')", (pid,))
            conn.commit()

            self.assertFalse(server.retag_if_changed(conn))
            self.assertFalse(server.retag_if_changed(conn))
            # Read through a separate connection: the early-return repair must commit.
            reader = db.connect(path)
            self.addCleanup(reader.close)
            tags = {tuple(row) for row in reader.execute('SELECT person_id,tag,source FROM tags')}
            self.assertNotIn((1, 'knows you', 'auto'), tags)
            self.assertIn((2, 'knows you', 'manual'), tags)
            for pid in (1, 2):
                self.assertIn((pid, 'Already know them', 'manual'), tags)
                self.assertIn((pid, 'AI: Has online shop', 'auto'), tags)
                verdict = reader.execute('SELECT model,input_hash,score,updated_at FROM verdicts WHERE person_id=?', (pid,)).fetchone()
                self.assertEqual(tuple(verdict), ('cached-model', 'hash', 91, '2026-01-01'))

    def test_old_auto_claim_removed_without_discarding_cached_llm_or_manual_tags(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'qualify', real_qualify):
            conn = db.init(str(Path(directory) / 'leads.sqlite'))
            self.addCleanup(conn.close)
            conn.execute("INSERT INTO seeds(handle,is_me) VALUES('owner',1)")
            rows = {}
            for handle, bio, cached in [('follow', 'Hello', True), ('mention', 'Hello @owner', True),
                                         ('rules', 'Hello @owner', False), ('manual', 'Hello', True)]:
                pid = conn.execute(
                    'INSERT INTO people(handle,bio,first_seen,updated_at) VALUES(?,?,?,?)',
                    (handle, bio, '2026-01-01', '2026-01-01')).lastrowid
                if handle == 'follow':
                    db.add_edge(conn, 'owner', pid, 'followers', '2026-01-01')   # an observed edge, with evidence
                conn.executemany('INSERT INTO tags VALUES(?,?,?,?)', [
                    (pid, 'knows you', 'source', 'manual' if handle == 'manual' else 'auto'),
                    (pid, 'Already know them', 'signal', 'manual'),
                    (pid, 'AI: Has online shop', 'ai', 'auto'),
                ])
                person = server.with_owner(conn, dict(conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()))
                input_hash = real_qualify.input_hash(person, server.edges_of(conn, pid)) if cached else None
                model = 'cached-model' if cached else 'rules'
                conn.execute('INSERT INTO verdicts(person_id,model,input_hash,score,content_fit,updated_at) VALUES(?,?,?,?,?,?)',
                             (pid, model, input_hash, 91, 91, person['updated_at']))
                rows[handle] = (pid, input_hash, model)
            db.set_setting(conn, 'tags_version', 't2')
            conn.commit()

            with patch.object(real_qualify, 'llm_verdict', side_effect=AssertionError('No model call needed')):
                self.assertTrue(server.retag_if_changed(conn))
                self.assertEqual(server.qualify_batch(conn), 4)
                self.assertFalse(server.retag_if_changed(conn))
                self.assertEqual(server.qualify_batch(conn), 0)

            self.assertEqual(db.get_setting(conn, 'tags_version'), real_qualify.TAGS_VERSION)
            for handle, (pid, input_hash, model) in rows.items():
                with self.subTest(handle=handle):
                    tags = {tuple(row) for row in conn.execute('SELECT tag,source FROM tags WHERE person_id=?', (pid,))}
                    self.assertNotIn(('knows you', 'auto'), tags)
                    self.assertIn(('Already know them', 'manual'), tags)
                    if handle == 'follow':
                        self.assertIn(('Instagram link', 'auto'), tags)
                        self.assertIn(('follows you', 'auto'), tags)
                    elif handle in ('mention', 'rules'):
                        self.assertIn(('mentions you', 'auto'), tags)
                        self.assertNotIn(('Instagram link', 'auto'), tags)
                    else:
                        self.assertIn(('knows you', 'manual'), tags)
                    if input_hash:
                        self.assertIn(('AI: Has online shop', 'auto'), tags)
                        # The model's business fit is kept; the displayed score reblends it with the network.
                        verdict = conn.execute('SELECT model,input_hash,content_fit FROM verdicts WHERE person_id=?', (pid,)).fetchone()
                        self.assertEqual(tuple(verdict), (model, input_hash, 91))


if __name__ == '__main__':
    unittest.main()
