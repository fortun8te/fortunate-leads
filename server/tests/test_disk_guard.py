"""The disk guard warns early, pauses write-heavy work and new collection leases, and resumes with margin."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import disk_guard  # noqa: E402
from disk_guard import DiskGuard, GB  # noqa: E402
from test_server import Base  # noqa: E402

TOTAL = 900 * GB


class FakeDisk:
    def __init__(self, free_gb):
        self.free = int(free_gb * GB)
        self.now = 0.0
        self.reads = 0

    def usage(self):
        self.reads += 1
        return TOTAL, self.free

    def clock(self):
        return self.now


def guard_for(tmp, free_gb, **kw):
    disk = FakeDisk(free_gb)
    lines = []
    guard = DiskGuard(Path(tmp) / 'leads.sqlite', usage=disk.usage, clock=disk.clock, log=lines.append, **kw)
    return guard, disk, lines


class LevelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_levels_follow_free_space(self):
        for free, level in ((58, 'ok'), (10.0, 'ok'), (9.9, 'low'), (3.0, 'low'), (2.9, 'critical'), (0.2, 'critical')):
            guard, disk, _ = guard_for(self.tmp.name, free)
            state = guard.state()
            self.assertEqual(state['level'], level, free)
            self.assertEqual(state['paused'], level == 'critical')
        guard, _, _ = guard_for(self.tmp.name, 58)
        self.assertEqual(guard.state()['used_percent'], round(100.0 * (TOTAL - 58 * GB) / TOTAL, 1))

    def test_this_mac_at_94_percent_with_58_gb_free_is_fine(self):
        guard, _, lines = guard_for(self.tmp.name, 57.9)
        self.assertEqual(guard.level(), 'ok')
        self.assertEqual(lines, [])

    def test_resume_needs_margin_so_it_does_not_flap(self):
        guard, disk, lines = guard_for(self.tmp.name, 2.5)
        self.assertTrue(guard.paused())
        for free in (3.0, 3.5, 3.99):
            disk.free = int(free * GB)
            disk.now += 10
            self.assertTrue(guard.paused(), free)
        disk.free, disk.now = int(4.0 * GB), disk.now + 10
        self.assertFalse(guard.paused())
        self.assertEqual(guard.level(), 'low')
        self.assertEqual(sum('almost full' in line for line in lines), 1)   # logged once, not on every read
        self.assertTrue(any('low' in line for line in lines))

    def test_recovery_is_logged_once_when_space_returns(self):
        guard, disk, lines = guard_for(self.tmp.name, 2.0)
        guard.state()
        disk.free, disk.now = 50 * GB, disk.now + 10
        self.assertEqual(guard.level(), 'ok')
        self.assertIn('recovered', lines[-1])

    def test_pause_line_scales_with_the_database_and_its_wal(self):
        db = Path(self.tmp.name) / 'leads.sqlite'
        db.write_bytes(b'0')
        with open(db, 'r+b') as f:
            f.truncate(4 * GB)
        Path(str(db) + '-wal').write_bytes(b'')
        guard, _, _ = guard_for(self.tmp.name, 5)
        warn, pause, resume = guard.thresholds()
        self.assertEqual(pause, int(4 * GB * 1.5))
        self.assertGreater(warn, pause)
        self.assertGreater(resume, pause)
        self.assertTrue(guard.paused())          # 5 GB free is below 1.5 x a 4 GB database

    def test_environment_override_and_bad_values(self):
        with mock.patch.dict(os.environ, {'FL_DISK_WARN_GB': '20', 'FL_DISK_PAUSE_GB': '8'}):
            guard, _, _ = guard_for(self.tmp.name, 12)
            self.assertEqual((guard.warn_bytes, guard.pause_bytes), (20 * GB, 8 * GB))
            self.assertEqual(guard.level(), 'low')
        with mock.patch.dict(os.environ, {'FL_DISK_WARN_GB': 'lots', 'FL_DISK_PAUSE_GB': '-1'}):
            guard, _, _ = guard_for(self.tmp.name, 12)
            self.assertEqual((guard.warn_bytes, guard.pause_bytes), (10 * GB, 3 * GB))
        with mock.patch.dict(os.environ, {'FL_DISK_WARN_GB': '2', 'FL_DISK_PAUSE_GB': '5'}):
            guard, _, _ = guard_for(self.tmp.name, 12)
            self.assertLess(guard.pause_bytes, guard.warn_bytes)

    def test_reads_are_cached_and_unreadable_disk_never_pauses(self):
        guard, disk, _ = guard_for(self.tmp.name, 50)
        for _ in range(20):
            guard.paused()
        self.assertEqual(disk.reads, 1)
        disk.now += 6
        guard.state()
        self.assertEqual(disk.reads, 2)
        broken = DiskGuard(Path(self.tmp.name) / 'x.sqlite', usage=mock.Mock(side_effect=OSError('gone')))
        self.assertEqual(broken.level(), 'unknown')
        self.assertFalse(broken.paused())

    def test_require_keeps_room_for_a_big_write(self):
        guard, _, _ = guard_for(self.tmp.name, 8)
        self.assertTrue(guard.require(4 * GB))
        self.assertFalse(guard.require(6 * GB))     # would leave 2 GB, under the pause line

    def test_messages_follow_the_copy_guide(self):
        for free in (8.2, 2.4):
            guard, _, _ = guard_for(self.tmp.name, free)
            text = guard.message()
            self.assertRegex(text, r'^[A-Z]')
            for banned in ('Please', 'Error', 'Oops', '!', 'pause_', 'disk_low'):
                self.assertNotIn(banned, text)
            self.assertIn(f'{free:.1f} GB free', text)


class WrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        disk_guard.reset()
        self.addCleanup(disk_guard.reset)
        self.db = str(Path(self.tmp.name) / 'leads.sqlite')
        self.disk = FakeDisk(50)
        disk_guard._REGISTRY[str(Path(self.db).resolve())] = DiskGuard(
            self.db, usage=self.disk.usage, clock=self.disk.clock, log=lambda t: None)

    def test_heavy_steps_idle_while_critical_and_run_again_after(self):
        calls = []
        step = disk_guard.guarded('photos', lambda conn: calls.append(conn) or True, self.db)
        self.assertTrue(step('c1'))
        self.disk.free, self.disk.now = 1 * GB, 10
        self.assertFalse(step('c2'))
        self.assertEqual(calls, ['c1'])
        self.disk.free, self.disk.now = 30 * GB, 20
        self.assertTrue(step('c3'))
        self.assertEqual(disk_guard.for_database(self.db).state()['skipped'], {'photos': 1})

    def test_light_steps_are_never_wrapped(self):
        def repair(conn):
            return True
        self.assertIs(disk_guard.guarded('repair', repair, self.db), repair)
        self.assertIs(disk_guard.guarded('local-services', repair, self.db), repair)

    def test_every_write_heavy_name_is_a_real_worker_loop(self):
        source = (Path(__file__).resolve().parents[1] / 'backend' / 'app.py').read_text()
        loops = set(re.findall(r"\('([a-z-]+)', [\w.]+, [\d.]+, [\d.]+\)", source))
        self.assertTrue(disk_guard.WRITE_HEAVY <= loops, disk_guard.WRITE_HEAVY - loops)
        self.assertEqual(loops - disk_guard.WRITE_HEAVY, {'repair', 'local-services'})

    def test_lease_hold_shape(self):
        self.assertIsNone(disk_guard.lease_hold(self.db))
        self.disk.free, self.disk.now = 1 * GB, 10
        hold = disk_guard.lease_hold(self.db)
        self.assertEqual((hold['granted'], hold['job'], hold['paused'], hold['reason']), (False, None, True, 'disk_low'))
        self.assertEqual(hold['stages'], {'list': False, 'profile': False})
        self.assertEqual(hold['wait_ms'], 60000)


class CollectionHoldTest(Base):
    """Through the real HTTP handlers: a critical disk hands out no work, and recovery hands it out again."""

    def install(self, free_gb):
        self.disk = FakeDisk(free_gb)
        disk_guard.reset()
        self.addCleanup(disk_guard.reset)
        disk_guard._REGISTRY[str(Path(self.tmp.name, 'leads.sqlite').resolve())] = DiskGuard(
            self.tmp.name + '/leads.sqlite', usage=self.disk.usage, clock=self.disk.clock, log=lambda t: None)

    def test_no_lease_and_no_permit_while_critical_then_work_resumes(self):
        self.install(1.0)
        self.call('/api/scraper/seeds', {'handles': ['one'], 'directions': ['followers']})
        code, reply = self.call('/api/ext/next?lane=lane1&version=3.9.17')
        self.assertEqual(code, 200)
        self.assertIsNone(reply['job'])
        self.assertTrue(reply['paused'])
        self.assertEqual(reply['reason'], 'disk_low')
        self.assertIn('almost full', reply['message'])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE state='leased'").fetchone()[0], 0)
        code, reply = self.call('/api/ext/request', {'action': 'acquire', 'kind': 'list', 'lane_id': 'lane1',
                                                      'job_id': 1, 'lease_token': 'x'})
        self.assertFalse(reply['granted'])
        self.assertEqual(reply['reason'], 'disk_low')
        # releasing a permit still works while the disk is critical
        code, reply = self.call('/api/ext/request', {'action': 'release', 'lane_id': 'lane1', 'token': 'none'})
        self.assertNotEqual(reply.get('reason'), 'disk_low')
        self.disk.free, self.disk.now = 40 * GB, 100
        code, reply = self.call('/api/ext/next?lane=lane1&version=3.9.17')
        self.assertIsNotNone(reply['job'])

    def test_disk_endpoint_reports_level_and_message(self):
        self.install(8.0)
        code, state = self.call('/api/disk')
        self.assertEqual(code, 200)
        self.assertEqual(state['level'], 'low')
        self.assertFalse(state['paused'])
        self.assertIn('Disk space is low', state['message'])


if __name__ == '__main__':
    unittest.main()
