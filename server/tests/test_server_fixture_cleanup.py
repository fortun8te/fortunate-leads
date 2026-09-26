"""A failed HTTP fixture setup must not change later qualification tests."""
import unittest
from unittest.mock import patch

from test_server import Base, server


class FixtureCleanupTest(unittest.TestCase):
    def test_failed_bind_restores_real_qualifier(self):
        class FailedBind(Base):
            def runTest(self):
                pass

        original = server.qualify
        result = unittest.TestResult()
        with patch.dict(server.CFG), patch('test_server.ThreadingHTTPServer', side_effect=OSError('bind denied')):
            FailedBind().run(result)
        self.assertEqual(len(result.errors), 1)
        self.assertIn('bind denied', result.errors[0][1])
        self.assertIs(server.qualify, original)
