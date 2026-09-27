"""Saved model choices remain stable across discovery, refreshes, and reloads."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import llm


class ModelOrderTest(unittest.TestCase):
    def test_catalogue_does_not_prepend_or_remove_saved_choices(self):
        chosen = ['vendor/first:free', 'stealth/chosen', 'vendor/unlisted:free']
        auto = {'stealth': ['stealth/new'], 'free': ['vendor/first:free']}
        self.assertEqual(llm.model_order(chosen, auto), tuple(chosen))

    def test_default_order_stays_fixed_when_new_models_are_discovered(self):
        self.assertEqual(llm.model_order(None, {'stealth': ['stealth/new']}), llm.MODELS)

    def test_invalid_and_paid_choices_are_filtered_without_reordering(self):
        self.assertEqual(llm.model_order(['bad id:free', 'vendor/paid', 'stealth/chosen',
                                          'vendor/first:free', 'stealth/chosen'], {}),
                         ('stealth/chosen', 'vendor/first:free'))

    def test_save_refresh_reload_and_readback_preserve_exact_order(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'OPENROUTER_API_KEYS': '', 'FL_NO_ORSLOT': '1'}), \
                patch.object(llm, '_post', side_effect=AssertionError('No provider requests allowed')):
            config = Path(directory) / 'openrouter.json'
            config.write_text(json.dumps({'keys': [], 'auto_models': {
                'stealth': ['stealth/old'], 'free': ['vendor/first:free']}}))
            provider = llm.Providers.load(config, proxy='')
            choices = ['vendor/first:free', 'stealth/chosen', 'vendor/unlisted:free']
            with patch.object(llm, 'get', return_value=provider), \
                    patch.object(llm, 'PROVIDERS', [provider]):
                llm.set_models(choices, path=config)
                self.assertEqual(provider.status()['models'], choices)
                llm.refresh_models(path=config, force=True, fetch=lambda: {'data': [{
                    'id': 'stealth/new', 'name': 'Anonymous model', 'context_length': 32000,
                    'pricing': {'prompt': '0', 'completion': '0'},
                    'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']},
                }]})
                self.assertEqual(provider.status()['models'], choices)
            self.assertEqual(json.loads(config.read_text())['models'], choices)
            self.assertEqual(llm.Providers.load(config, proxy='').status()['models'], choices)


if __name__ == '__main__':
    unittest.main()
