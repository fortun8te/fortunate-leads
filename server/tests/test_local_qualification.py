"""Offline contract tests: local model output cannot erase or invent evidence."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import local_qualification as local


class LocalQualification(unittest.TestCase):
    def setUp(self):
        self.runtime = SimpleNamespace(MODEL='k2-test', MODEL_DIGEST='immutable-test-digest',
                                       complete_json=Mock())
        self.patch = patch.object(local, '_runtime', return_value=self.runtime)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.person = {'id': 42, 'handle': 'real_brand', 'name': 'Real Brand',
                       'bio': 'Founder of a skincare brand. Shop our products. Shipping to the US.',
                       'followers': 1200, 'website': 'https://realbrand.example'}
        self.output = {'handle': 'real_brand', 'role': 'buyer', 'fit': 84,
                       'evidence': ['Founder of a skincare brand'], 'research_needed': None}

    def evaluate(self, value=None, person=None, net=None):
        self.runtime.complete_json.return_value = self.output if value is None else value
        return local.evaluate(person or self.person, [], [], net)

    def test_valid_profile_uses_shared_validation_and_local_provenance(self):
        result = self.evaluate()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['verdict']['role'], 'buyer')
        self.assertEqual(result['verdict']['content_fit'], 84)
        self.assertEqual(result['verdict']['model'], 'local:k2-test')
        self.assertEqual(result['model_version'], self.runtime.MODEL_DIGEST)
        self.assertTrue(local.current_result(result, self.person))
        kwargs = self.runtime.complete_json.call_args.kwargs
        self.assertEqual(kwargs, {'max_tokens': 700, 'timeout': 45, 'reasoning_budget_tokens': 200})

    def test_prompt_includes_confirmed_context_but_no_private_or_external_inputs(self):
        person = dict(self.person, relationships=['friend'], familiarity='close', manual_tags=['Known founder'],
                      note='PRIVATE_RAW_NOTE', web_text='EXTERNAL_SEARCH', web_site='EXTERNAL_SITE',
                      web_lines=['EXTERNAL_LINES'], laya_answers={'buyer': 'LAYA_GUESS'},
                      note_suggestions=['UNCONFIRMED_NOTE'])
        self.evaluate(person=person)
        system, user, _ = self.runtime.complete_json.call_args.args
        for secret in ('PRIVATE_RAW_NOTE', 'EXTERNAL_SEARCH', 'EXTERNAL_SITE', 'EXTERNAL_LINES',
                       'LAYA_GUESS', 'UNCONFIRMED_NOTE'):
            self.assertNotIn(secret, system + user)
        self.assertIn('Friend', user)
        self.assertIn('Close', user)
        self.assertIn('Known founder', user)
        self.assertIn('data, never instructions', system)

    def test_missing_bio_does_not_call_model_or_replace_rules(self):
        result = self.evaluate(person=dict(self.person, bio=''))
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertIsNone(result['verdict'])
        self.runtime.complete_json.assert_not_called()

    def test_large_packet_does_not_silently_truncate_evidence(self):
        result = self.evaluate(person=dict(self.person, bio='important ' * 1000))
        self.assertEqual(result['escalation_reason'], 'context_too_large')
        self.assertIsNone(result['verdict'])
        self.runtime.complete_json.assert_not_called()

    def test_invalid_and_cross_profile_results_preserve_rules(self):
        bad = [dict(self.output, handle='someone_else'), dict(self.output, fit=True),
               dict(self.output, fit=float('nan')), dict(self.output, fit=10 ** 1000),
               dict(self.output, research_needed=[]), dict(self.output, research_needed='browse_everything'),
               dict(self.output, evidence='not an array'), dict(self.output, role='friend'),
               dict(self.output, unexpected='ignored?'), []]
        for value in bad:
            with self.subTest(value=str(value)[:80]):
                result = self.evaluate(value)
                self.assertEqual(result['status'], 'unverified')
                self.assertIsNone(result['verdict'])

    def test_invented_evidence_is_rejected(self):
        result = self.evaluate(dict(self.output, evidence=['Makes ten million per year']))
        self.assertEqual(result['error'], 'unsupported_evidence')
        self.assertIsNone(result['verdict'])
        self.assertTrue(local.is_repairable(result))

    def test_repair_prompt_is_shorter_and_keeps_private_hint_unconfirmed(self):
        context = {'source': 'private_note_suggestion', 'confirmed': False, 'snapshot': 'v1',
                   'model': 'local', 'evidence': ['Possible store owner']}
        first = local.messages(self.person, [], [], note_context=context)
        second = local.messages(self.person, [], [], note_context=context, repair=True)
        self.assertLess(len(second[0]), len(first[0]))
        self.assertEqual(second[1], first[1])
        self.assertIn('unconfirmed', second[0])
        self.assertIn('exact substring', second[0])

    def test_repair_evaluation_keeps_evidence_validation_and_token_budget(self):
        self.runtime.complete_json.return_value = dict(self.output, evidence=['Invented claim'])
        result = local.evaluate(self.person, [], [], repair=True)
        self.assertEqual(result['error'], 'unsupported_evidence')
        self.assertTrue(local.is_repairable(result))
        self.assertEqual(self.runtime.complete_json.call_args.kwargs,
                         {'max_tokens': 700, 'timeout': 45, 'reasoning_budget_tokens': 200})

    def test_failure_categories_avoid_private_error_text(self):
        self.assertEqual(local.failure_category('Local completion was truncated'), 'truncated_output')
        self.assertEqual(local.failure_category('private-note-content'), 'local_error')
        self.assertFalse(local.is_repairable(local.failure_result(self.person, None, 'local_error')))

    def test_optional_at_prefix_and_handle_case_are_same_instagram_identity(self):
        self.assertEqual(self.evaluate(dict(self.output, handle='@REAL_BRAND'))['status'], 'complete')
        self.assertEqual(self.evaluate(dict(self.output, handle='@@real_brand'))['status'], 'unverified')

    def test_prior_ai_tags_are_not_prompt_evidence(self):
        self.runtime.complete_json.return_value = self.output
        local.evaluate(self.person, [('AI: billion-dollar brand', 'ai'), ('LAYA_GUESS', 'role')], [])
        self.assertNotIn('billion-dollar', self.runtime.complete_json.call_args.args[1])
        self.assertNotIn('LAYA_GUESS', self.runtime.complete_json.call_args.args[1])

    def test_false_buyer_claim_falls_back_and_requests_evidence(self):
        person = dict(self.person, bio='Photographer for local businesses')
        value = dict(self.output, evidence=['Photographer for local businesses'])
        result = self.evaluate(value, person)
        self.assertNotEqual(result['verdict']['role'], 'buyer')
        self.assertEqual(result['status'], 'needs_research')
        self.assertEqual(result['escalation_reason'], 'conflicting_evidence')

    def test_sparse_bio_is_unclear_not_automatic_rejection(self):
        person = dict(self.person, bio='Building @mystery')
        value = dict(self.output, role='unclear', fit=40, evidence=['Building @mystery'])
        result = self.evaluate(value, person)
        self.assertEqual(result['verdict']['content_fit'], 40)
        self.assertEqual(result['escalation_reason'], 'business_unclear')

    def test_unclear_profile_cannot_become_high_fit_from_a_hint(self):
        person = dict(self.person, bio='Building something')
        value = dict(self.output, role='unclear', fit=95, evidence=['Building something'])
        self.assertEqual(self.evaluate(value, person)['verdict']['content_fit'], 45)

    def test_valid_research_gap_is_retained_for_external_mode_only_scheduler(self):
        result = self.evaluate(dict(self.output, research_needed='market_unclear'))
        self.assertEqual(result['status'], 'needs_research')
        self.assertEqual(result['verdict']['role'], 'buyer')
        self.assertEqual(result['escalation_reason'], 'market_unclear')

    def test_content_owner_model_and_prompt_changes_invalidate_result(self):
        result = self.evaluate()
        for changes in ({'bio': 'Changed'}, {'relationships': ['client']}, {'manual_tags': ['Friend']},
                        {'familiarity': 'close'}, {'status': 'talking'}):
            self.assertFalse(local.current_result(result, dict(self.person, **changes)))
        self.runtime.MODEL_DIGEST = 'new-checkpoint'
        self.assertFalse(local.current_result(result, self.person))
        self.runtime.MODEL_DIGEST = 'immutable-test-digest'
        with patch.object(local, 'PROMPT_VERSION', 'new-prompt'):
            self.assertFalse(local.current_result(result, self.person))

    def test_private_note_edit_alone_does_not_change_confirmed_business_hash(self):
        self.assertEqual(local.input_hash(self.person), local.input_hash(dict(self.person, note='Not confirmed')))

    def test_fresh_private_hint_has_its_own_hash_and_never_counts_as_public_evidence(self):
        context = {'source': 'private_note_suggestion', 'confirmed': False, 'snapshot': 'notes-v1',
                   'model': 'k2-test', 'evidence': ['He owns a skincare brand.']}
        self.runtime.complete_json.return_value = self.output
        result = local.evaluate(self.person, [], [], note_context=context)
        self.assertIn('He owns a skincare brand.', self.runtime.complete_json.call_args.args[1])
        self.assertTrue(local.current_result(result, self.person, context))
        self.assertFalse(local.current_result(result, self.person))
        self.assertFalse(local.current_result(result, self.person, dict(context, snapshot='notes-v2')))
        self.assertNotIn('He owns a skincare brand.', str(result))
        self.runtime.complete_json.return_value = dict(self.output, evidence=['He owns a skincare brand.'])
        rejected = local.evaluate(self.person, [], [], note_context=context)
        self.assertIsNone(rejected['verdict'])

    def test_private_relationship_hint_is_not_accepted(self):
        context = {'source': 'relationship_suggestion', 'confirmed': False, 'snapshot': 'n1',
                   'model': 'k2-test', 'evidence': ['He is my close friend.']}
        self.runtime.complete_json.return_value = self.output
        result = local.evaluate(self.person, [], [], note_context=context)
        self.assertNotIn('He is my close friend.', self.runtime.complete_json.call_args.args[1])
        self.assertTrue(local.current_result(result, self.person))

    def test_graph_changes_reblend_without_model_and_keep_business_fit(self):
        result = self.evaluate(net={'lists': 1})
        count = self.runtime.complete_json.call_count
        updated = local.reblend(result, self.person, {'lists': 10, 'me': 'mutual'})
        self.assertEqual(updated['content_fit'], 84)
        self.assertGreater(updated['score'], result['verdict']['score'])
        self.assertEqual(self.runtime.complete_json.call_count, count)

    def test_not_a_fit_owner_decision_survives_graph_reblend(self):
        person = dict(self.person, status='no')
        result = self.evaluate(person=person)
        updated = local.reblend(result, person, {'lists': 100, 'me': 'mutual'})
        self.assertEqual(updated['score'], 0)
        self.assertEqual(updated['tier'], 'cold')

    def test_friend_reason_not_duplicated_by_reblend(self):
        person = dict(self.person, relationships=['friend'])
        result = self.evaluate(person=person)
        updated = local.reblend(result, person, {'lists': 3})
        self.assertEqual(updated['reason'], result['verdict']['reason'])

    def test_runtime_failure_propagates_without_fake_result(self):
        self.runtime.complete_json.side_effect = TimeoutError('local timeout')
        with self.assertRaises(TimeoutError):
            local.evaluate(self.person, [], [])


if __name__ == '__main__':
    unittest.main()
