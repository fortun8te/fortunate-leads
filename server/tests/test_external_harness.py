"""No external services: transport isolation and evidence boundaries."""
import json
import os
import sqlite3
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import external_harness as harness
import deepscout


class HarnessTest(unittest.TestCase):
    def test_notes_are_never_in_external_packet(self):
        text = harness.packet({'id': 1, 'handle': 'brand', 'bio': 'We sell skincare.',
            'note': 'PRIVATE RAW NOTE', 'note_interpretation': {'text': 'PRIVATE INTERPRETATION'},
            'relationships': ['friend'], 'familiarity': 'close', 'manual_tags': ['Client']})
        self.assertNotIn('PRIVATE', text)
        self.assertIn('Friend', text)
        self.assertIn('Client', text)

    def test_broad_requires_escalation_and_cited_evidence(self):
        p = {'id': 1, 'handle': 'brand', 'bio': 'Founder of a skincare brand.'}
        with patch.object(harness, 'invoke', return_value={'role': 'buyer', 'fit': 95, 'evidence': ['Invented claim']}) as call:
            self.assertIsNone(harness.broad(p, [], []))
            call.assert_not_called()
            self.assertIsNone(harness.broad(p, [], [], escalation_reason='unclear ownership'))
        with patch.object(harness, 'invoke', return_value={'unresolved': True}):
            self.assertIsNone(harness.broad(p, [], [], escalation_reason='unknown market'))

    def test_broad_uses_shared_validator(self):
        p = {'id': 1, 'handle': 'brand', 'bio': 'Founder of a skincare brand.'}
        answer = {'role': 'buyer', 'fit': 85, 'decision_maker': True,
                  'evidence': ['Founder of a skincare brand.'], 'extra_tags': []}
        with patch.object(harness, 'invoke', return_value=answer):
            out = harness.broad(p, [], [], escalation_reason='unclear market')
        self.assertEqual(out['model'], 'hermes-broad:grok')
        self.assertEqual(out['role'], 'buyer')

    def test_isolated_home_no_fallback_and_only_selected_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'auth.json').write_text(json.dumps({'version': 1, 'providers': {
                'xai-oauth': {'token': 'SELECTED'}, 'other': {'token': 'UNRELATED'}},
                'credential_pool': {'other': ['UNRELATED']}}))
            homes = []
            def launch(args, **kwargs):
                home = Path(kwargs['env']['HERMES_HOME']); homes.append(home)
                config = json.loads((home / 'config.yaml').read_text())
                self.assertEqual(config['fallback_providers'], [])
                self.assertEqual(config['fallback_model'], [])
                self.assertEqual(config['mcp_servers'], {})
                self.assertEqual(config['toolsets'], ['web'])
                self.assertEqual(config['web']['backend'], 'searxng')
                self.assertNotIn('UNRELATED', (home / 'auth.json').read_text())
                self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
                self.assertIn('--ignore-rules', args)
                self.assertEqual(args[args.index('--max-turns') + 1], '4')
                self.assertTrue(kwargs['start_new_session'])
                p = MagicMock(returncode=0)
                p.communicate.return_value = ('{"unresolved":true}', '')
                return p
            with patch.object(harness.usage_ledger, 'PATH', root / 'usage.sqlite'), patch.object(harness, 'HERMES_ROOT', root), patch.object(harness, 'available', return_value=True), \
                 patch.dict(os.environ, {'OPENAI_API_KEY': 'UNRELATED'}), patch.object(harness.subprocess, 'Popen', side_effect=launch):
                self.assertEqual(harness.invoke('test'), {'unresolved': True})
            self.assertFalse(homes[0].exists())

    def test_cached_related_quote_reused_without_fetch(self):
        p = {'website': 'https://brand.example', 'web_site': 'brand.example: We sell skincare products.'}
        with patch.object(deepscout, '_fetch_cited_page') as fetch:
            out = harness.verified_research(p, [{'source': 'https://brand.example/about', 'quote': 'We sell skincare products.'}])
            fetch.assert_not_called()
            self.assertIn('We sell skincare products.', out['web_site'])

    def test_unrelated_and_fabricated_research_cannot_become_facts(self):
        p = {'website': 'https://brand.example'}
        with patch.object(deepscout, '_fetch_cited_page') as fetch:
            self.assertNotIn('web_site', harness.verified_research(p,
                [{'source': 'https://unrelated.example', 'quote': 'We sell skincare products.'}]))
            fetch.assert_not_called()
            fetch.return_value = ('https://brand.example/about', '<p>Welcome to our site.</p>')
            self.assertNotIn('web_site', harness.verified_research(p,
                [{'source': 'https://brand.example/about', 'quote': 'We sell skincare products.'}]))

    def test_negative_deep_verdict_needs_actual_exclusion(self):
        answer = {'verdict': 'no', 'reachable': False, 'summary': 'Not a fit', 'tags': [], 'sources': [], 'evidence': []}
        p = {'bio': 'Hello everyone'}
        self.assertFalse(deepscout.verify(p, answer)[0])
        answer['evidence'] = [{'source': 'profile.bio', 'quote': 'Hello everyone'}]
        self.assertFalse(deepscout.verify(p, answer)[0])
        p['bio'] = 'This is a fan account, not a business.'
        answer['evidence'][0]['quote'] = p['bio']
        self.assertTrue(deepscout.verify(p, answer)[0])

    def test_usage_saved_before_cleanup_and_missing_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / 'usage.sqlite'
            home = root / 'hermes'; home.mkdir()
            with sqlite3.connect(home / 'state.db') as conn:
                conn.execute('CREATE TABLE sessions(input_tokens INTEGER, output_tokens INTEGER)')
                conn.execute('INSERT INTO sessions VALUES(120,35)')
                conn.execute('INSERT INTO sessions VALUES(80,15)')
            with patch.object(harness.usage_ledger, 'PATH', ledger):
                harness._record_usage(home, 'xai-oauth', 'grok-4.6', 'broad', time.monotonic(), 'ok')
                with sqlite3.connect(home / 'state.db') as conn:
                    conn.execute('DELETE FROM sessions')
                    conn.execute('INSERT INTO sessions VALUES(0,0)')
                harness._record_usage(home, 'xai-oauth', 'grok-4.6', 'deep', time.monotonic(), 'invalid_reply')
                harness._record_usage(root / 'missing', 'xai-oauth', 'grok-4.6', 'broad', time.monotonic(), 'transport_error')
            with sqlite3.connect(ledger) as conn:
                rows = conn.execute('SELECT purpose,success,status,input_tokens,output_tokens FROM attempts ORDER BY id').fetchall()
            self.assertEqual(rows, [('external_broad',1,'ok',200,50),
                                    ('external_deep',0,'invalid_reply',None,None),
                                    ('external_broad',0,'transport_error',None,None)])
            report = harness.usage_ledger.summary(ledger, purpose='all')
            self.assertEqual(report['requests'], 3)
            self.assertEqual(report['failed_attempts'], 2)
            self.assertEqual(report['token_reports'], 1)
            self.assertEqual(report['missing_token_reports'], 2)
            self.assertEqual(report['input_tokens_reported'], 200)

    def test_pause_after_answer_does_not_start_citation_fetch(self):
        p = {'handle': 'brand', 'bio': 'We sell skincare products.'}
        answer = {'role': 'buyer', 'fit': 85, 'evidence': ['We sell skincare products.'],
                  'research': [{'source': 'https://brand.example', 'quote': 'We sell skincare products.'}]}
        with patch.object(harness, 'invoke', return_value=answer) as invoke, \
             patch.object(harness, 'verified_research') as verify:
            self.assertIsNone(harness.broad(p, [], [], escalation_reason='role_unclear',
                                           allowed=MagicMock(side_effect=[True, False])))
            invoke.assert_called_once()
            verify.assert_not_called()
        with patch.object(harness, 'invoke') as invoke:
            self.assertIsNone(harness.broad(p, [], [], escalation_reason='role_unclear', allowed=lambda: False))
            invoke.assert_not_called()
