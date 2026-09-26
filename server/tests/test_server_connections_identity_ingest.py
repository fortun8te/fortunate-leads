"""Real HTTP ingestion tests; this filename sorts after the qualifier unit tests.

The expected identities below come from the submitted page, never a metadata
lookup. Every server and database is temporary and contains fictional accounts.
"""
import unittest
from urllib.parse import urlencode

from test_server import Base
import db


_MISSING = object()


class ConnectionIdentityIngestTests(Base):
    def ingest(self, seed, users, source_id=_MISSING, direction='following', **extra):
        body = {'seed': seed, 'direction': direction, 'users': users, 'done': True}
        if source_id is not _MISSING:
            body['ig_id'] = source_id
        body.update(extra)
        status, result = self.call('/api/ext/list-page', body)
        self.assertEqual(status, 200, result)
        return result

    def explore(self, source, target):
        status, result = self.call('/api/connections?' + urlencode(
            {'from': source, 'to': target, 'mode': 'following'}))
        self.assertEqual(status, 200, result)
        return result

    def observations(self, seed):
        return [dict(row) for row in self.conn.execute(
            'SELECT seed,person_id,direction,seed_ig_id,person_ig_id '
            'FROM edge_observations WHERE seed=? '
            'ORDER BY seed_ig_id,person_ig_id,person_id', (seed,))]

    def assert_direct(self, source, target, expected_source_id, expected_member_id):
        result = self.explore(source, target)
        self.assertEqual([path['nodes'] for path in result['paths']], [[source, target]])
        self.assertEqual([(edge['from'], edge['to']) for edge in result['edges']], [(source, target)])
        self.assertEqual(
            [(item['seed_ig_id'], item['person_ig_id'])
             for edge in result['edges'] for item in edge['evidence']],
            [(expected_source_id, expected_member_id)])
        return result

    def test_supplied_and_omitted_ids_are_taken_from_each_http_page(self):
        for direction in ('following', 'followers'):
            for supplied_source in (False, True):
                for supplied_member in (False, True):
                    index = (4 if direction == 'followers' else 0) + 2 * supplied_source + supplied_member
                    seed, handle = f'fictional_source_{index}', f'fictional_member_{index}'
                    incoming_source, current_source, current_member = str(1000 + index), str(9000 + index), str(2000 + index)
                    self.conn.execute('INSERT INTO seeds(handle,ig_id) VALUES(?,?)', (seed, current_source))
                    pid = db.upsert_person(self.conn, {'handle': handle, 'ig_id': current_member})
                    self.conn.commit()
                    member = {'handle': handle}
                    if supplied_member:
                        member['ig_id'] = current_member
                    source_id = incoming_source if supplied_source else _MISSING
                    with self.subTest(direction=direction, source=supplied_source, member=supplied_member):
                        self.ingest(seed, [member], source_id, direction)
                        expected_sid = incoming_source if supplied_source else ''
                        expected_mid = current_member if supplied_member else ''
                        self.assertEqual(self.observations(seed), [{
                            'seed': seed, 'person_id': pid, 'direction': direction,
                            'seed_ig_id': expected_sid, 'person_ig_id': expected_mid}])
                        self.assertEqual(self.conn.execute('SELECT ig_id FROM people WHERE id=?', (pid,)).fetchone()[0], current_member)
                        self.assertEqual(self.conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (seed,)).fetchone()[0],
                                         incoming_source if supplied_source else current_source)
                        source_key = f'ig:{incoming_source}' if supplied_source else f'legacy:{seed}'
                        member_key = f'ig:{current_member}' if supplied_member else f'legacy-person:{pid}'
                        start, end = (source_key, member_key) if direction == 'following' else (member_key, source_key)
                        self.assert_direct(start, end, expected_sid, expected_mid)
                        if not supplied_source or not supplied_member:
                            current_start, current_end = (f'ig:{incoming_source if supplied_source else current_source}', f'ig:{current_member}')
                            if direction == 'followers':
                                current_start, current_end = current_end, current_start
                            self.assertEqual(self.explore(current_start, current_end)['paths'], [])

    def test_reused_member_handle_preserves_old_new_and_unidentified_members(self):
        self.ingest('fictional_old_source', [{'handle': 'fictional_reused', 'ig_id': '201'}], '101')
        old_pid = self.observations('fictional_old_source')[0]['person_id']
        self.ingest('fictional_new_source', [{'handle': 'fictional_reused', 'ig_id': '202'}], '102')
        new_pid = self.observations('fictional_new_source')[0]['person_id']
        self.ingest('fictional_unknown_source', [{'handle': 'fictional_reused'}], '103')
        self.assertNotEqual(old_pid, new_pid)
        self.assertEqual(self.observations('fictional_unknown_source')[0]['person_id'], new_pid)
        self.assertEqual(self.observations('fictional_unknown_source')[0]['person_ig_id'], '')
        self.assert_direct('ig:101', 'ig:201', '101', '201')
        self.assert_direct('ig:102', 'ig:202', '102', '202')
        self.assert_direct('ig:103', f'legacy-person:{new_pid}', '103', '')
        self.assertEqual(self.explore('ig:101', 'ig:202')['paths'], [])
        self.assertEqual(self.explore('ig:103', 'ig:202')['paths'], [])
        self.assertEqual(self.conn.execute('SELECT ig_id FROM people WHERE id=?', (old_pid,)).fetchone()[0], '201')

    def test_reused_source_handle_preserves_observation_owners_after_profile_update(self):
        seed = 'fictional_shared_source'
        for source_id in ('501', '502', _MISSING):
            self.ingest(seed, [{'handle': 'fictional_shared_member', 'ig_id': '700'}], source_id)
        self.assertEqual([(row['seed_ig_id'], row['person_ig_id']) for row in self.observations(seed)],
                         [('', '700'), ('501', '700'), ('502', '700')])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges WHERE seed=?', (seed,)).fetchone()[0], 1)
        status, response = self.call('/api/ext/profile', {'profile': {'handle': seed, 'ig_id': '999', 'bio': ''}})
        self.assertEqual(status, 200, response)
        self.assertEqual(self.conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (seed,)).fetchone()[0], '999')
        for source_id in ('501', '502'):
            self.assert_direct('ig:' + source_id, 'ig:700', source_id, '700')
        self.assert_direct('legacy:' + seed, 'ig:700', '', '700')
        self.assertEqual(self.explore('ig:999', 'ig:700')['paths'], [])

    def test_incoming_ids_win_over_conflicting_source_registry_and_person_metadata(self):
        seed, member = 'fictional_conflict_source', 'fictional_conflict_member'
        self.conn.execute('INSERT INTO seeds(handle,ig_id) VALUES(?,?)', (seed, '801'))
        db.upsert_person(self.conn, {'handle': seed, 'ig_id': '802'})
        db.upsert_person(self.conn, {'handle': member, 'ig_id': '803'})
        self.conn.commit()
        self.ingest(seed, [{'handle': member, 'ig_id': 902}], 901)
        row = self.observations(seed)[0]
        self.assertEqual((row['seed_ig_id'], row['person_ig_id']), ('901', '902'))
        self.assert_direct('ig:901', 'ig:902', '901', '902')
        self.assertEqual(self.explore('ig:802', 'ig:902')['paths'], [])
        self.assertEqual(self.explore('ig:901', 'ig:803')['paths'], [])

    def test_malformed_member_fields_are_dropped_without_promoting_metadata_id(self):
        invalid_ids = [None, True, False, 12.5, [], {}, {'id': '990'}]
        members = []
        for index, invalid_id in enumerate(invalid_ids):
            handle = f'fictional_bad_member_{index}'
            db.upsert_person(self.conn, {'handle': handle, 'ig_id': str(3000 + index),
                                         'name': 'Existing name', 'pic_url': 'https://example.invalid/existing.jpg'})
            members.append({'handle': handle, 'ig_id': invalid_id, 'name': ['wrong type'], 'pic_url': {'url': 'wrong type'}})
        self.conn.commit()
        self.ingest('fictional_sanitized', members + [None, [], 'junk', 7, True, {}, {'handle': 4},
                                                    {'handle': []}, {'handle': ''}, {'handle': ' @ '}], '100')
        rows = self.observations('fictional_sanitized')
        self.assertEqual(len(rows), len(invalid_ids))
        self.assertEqual({(row['seed_ig_id'], row['person_ig_id']) for row in rows}, {('100', '')})
        people = self.conn.execute('SELECT ig_id,name,pic_url FROM people ORDER BY id').fetchall()
        self.assertEqual([tuple(row) for row in people],
                         [(str(3000 + index), 'Existing name', 'https://example.invalid/existing.jpg')
                          for index in range(len(invalid_ids))])
        for row in rows:
            self.assert_direct('ig:100', f"legacy-person:{row['person_id']}", '100', '')

    def test_http_payload_cannot_override_captured_ids_or_unaccepted_profile_fields(self):
        self.ingest('fictional_payload', [{
            'handle': '@Fictional.Member', 'ig_id': 200, 'name': 'Fictional Member',
            'person_ig_id': '999', 'seed_ig_id': '998', 'person_id': 997, 'id': 996,
            'bio': 'not an accepted list field', 'followers': 123456, 'website': 'https://example.invalid/'}],
            100, seed_ig_id='995', person_ig_id='994', person_id=993)
        observation = self.observations('fictional_payload')[0]
        self.assertEqual((observation['seed_ig_id'], observation['person_ig_id']), ('100', '200'))
        person = self.conn.execute('SELECT * FROM people').fetchone()
        self.assertEqual((person['handle'], person['ig_id'], person['name']), ('fictional.member', '200', 'Fictional Member'))
        self.assertEqual((person['bio'], person['followers'], person['website']), (None, None, None))
        self.assertEqual(observation['person_id'], person['id'])
        self.assert_direct('ig:100', 'ig:200', '100', '200')

    def test_http_nonobject_payloads_are_rejected_without_any_identity_writes(self):
        for payload in ([], ['fictional'], True, 123, 'fictional'):
            with self.subTest(payload=payload):
                status, result = self.call('/api/ext/list-page', payload)
                self.assertEqual(status, 400, result)
        for table in ('people', 'seeds', 'edges', 'edge_observations'):
            self.assertEqual(self.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0, table)

    def test_invalid_source_ids_do_not_poison_the_current_source_registry(self):
        for index, invalid_id in enumerate((True, False, 1.5, {'id': '901'}, ['901'],
                                            '', '   ', 'garbage', -7, '9' * 33)):
            seed = f'fictional_invalid_source_{index}'
            self.conn.execute('INSERT INTO seeds(handle,ig_id) VALUES(?,?)', (seed, '900'))
            self.conn.commit()
            with self.subTest(source_id=invalid_id):
                status, result = self.call('/api/ext/list-page', {
                    'seed': seed, 'ig_id': invalid_id, 'direction': 'following',
                    'users': [{'handle': f'fictional_target_{index}', 'ig_id': str(200 + index)}], 'done': True})
                self.assertIn(status, (200, 400), result)
                self.assertEqual(self.conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (seed,)).fetchone()[0], '900')
                if status == 200:
                    self.assertEqual(self.observations(seed)[0]['seed_ig_id'], '')

    def test_unselectable_member_ids_do_not_replace_known_profile_identity(self):
        for index, invalid_id in enumerate(('', '   ', 'garbage', '-7', -7, '1.5', '9' * 33)):
            seed, member, existing_id = f'fictional_invalid_member_source_{index}', f'fictional_invalid_member_{index}', str(4000 + index)
            pid = db.upsert_person(self.conn, {'handle': member, 'ig_id': existing_id})
            self.conn.commit()
            with self.subTest(member_id=invalid_id):
                status, result = self.call('/api/ext/list-page', {
                    'seed': seed, 'ig_id': '100', 'direction': 'following',
                    'users': [{'handle': member, 'ig_id': invalid_id}], 'done': True})
                self.assertIn(status, (200, 400), result)
                self.assertEqual(tuple(self.conn.execute('SELECT id,ig_id FROM people WHERE handle=?', (member,)).fetchone()),
                                 (pid, existing_id))
                if status == 200:
                    self.assertEqual(self.observations(seed)[0]['person_ig_id'], '')
                    self.assert_direct('ig:100', f'legacy-person:{pid}', '100', '')
                    self.assertEqual(self.explore('ig:100', 'ig:' + existing_id)['paths'], [])

    def test_padded_ids_are_consistent_between_search_metadata_and_observations(self):
        status, result = self.call('/api/ext/list-page', {
            'seed': 'fictional_padded_source', 'ig_id': ' 101 ', 'direction': 'following',
            'users': [{'handle': 'fictional_padded_member', 'ig_id': ' 202 '}], 'done': True})
        self.assertIn(status, (200, 400), result)
        if status == 400:
            self.assertEqual(self.observations('fictional_padded_source'), [])
            return
        self.assertEqual(self.conn.execute("SELECT ig_id FROM seeds WHERE handle='fictional_padded_source'").fetchone()[0], '101')
        self.assertEqual(self.conn.execute("SELECT ig_id FROM people WHERE handle='fictional_padded_member'").fetchone()[0], '202')
        self.assert_direct('ig:101', 'ig:202', '101', '202')
        for handle, expected in (('fictional_padded_source', 'ig:101'), ('fictional_padded_member', 'ig:202')):
            status, search = self.call('/api/connections/search?' + urlencode({'q': handle}))
            self.assertEqual(status, 200, search)
            self.assertEqual([account['id'] for account in search['accounts']], [expected])


if __name__ == '__main__':
    unittest.main()
