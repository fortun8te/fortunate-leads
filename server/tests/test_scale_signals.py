import unittest

import scale_helpers  # noqa: F401
from scale import signals as S


class ClassifyTest(unittest.TestCase):
    def test_limits_and_walls(self):
        self.assertEqual(S.classify(429, '')[0], S.RATE_LIMIT)
        self.assertEqual(S.classify(401, '{"message":"Please wait a few minutes before you try again.",'
                                         '"require_login":true,"status":"fail"}')[0], S.RATE_LIMIT)
        self.assertEqual(S.classify(400, '{"message":"feedback_required","spam":true}')[0], S.SOFT_BLOCK)
        self.assertEqual(S.classify(400, '{"message":"checkpoint_required","checkpoint_url":"x"}')[0], S.CHALLENGE)
        self.assertEqual(S.classify(302, '', 'https://www.instagram.com/accounts/login/?next=/nike/')[0], S.LOGIN_WALL)
        self.assertEqual(S.classify(302, '', 'https://www.instagram.com/challenge/abc/')[0], S.CHALLENGE)
        self.assertEqual(S.classify(403, '{"message":"login_required"}')[0], S.LOGIN_WALL)
        self.assertEqual(S.classify(403, '')[0], S.IP_BLOCKED)
        self.assertEqual(S.classify(400, '{"message":"proxy_address_is_blocked"}')[0], S.IP_BLOCKED)
        self.assertEqual(S.classify(404, '')[0], S.NOT_FOUND)
        self.assertEqual(S.classify(0, '')[0], S.NETWORK)

    def test_ok_and_prefix(self):
        self.assertEqual(S.classify(200, 'for (;;);{"status":"ok","data":{}}')[0], S.OK)
        self.assertEqual(S.classify(200, '<html>login password</html>')[0], S.LOGIN_WALL)

    def test_list_pages(self):
        ok = {'users': [{'username': 'a', 'pk': 1}], 'next_max_id': '25', 'has_more': True, 'status': 'ok'}
        self.assertEqual(S.classify(200, json_body=ok, kind='list')[0], S.OK)
        empty_more = {'users': [], 'next_max_id': '50', 'has_more': True, 'status': 'ok'}
        self.assertEqual(S.classify(200, json_body=empty_more, kind='list'), (S.SOFT_BLOCK, 'empty_page_with_more'))
        missing = {'users': [{'username': 'a'}], 'has_more': True, 'status': 'ok'}
        self.assertEqual(S.classify(200, json_body=missing, kind='list')[0], S.OTHER)
        capped = {'users': [], 'should_limit_list_of_followers': True, 'status': 'ok'}
        self.assertEqual(S.classify(200, json_body=capped, kind='list'), (S.OK, 'limited'))
        self.assertEqual(S.classify(200, json_body={'status': 'ok'}, kind='list')[0], S.OTHER)


if __name__ == '__main__':
    unittest.main()
