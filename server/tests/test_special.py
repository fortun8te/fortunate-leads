import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import deepscout


class SpecialPrompt(unittest.TestCase):
    def test_gathered_research_goes_in_the_prompt(self):
        p = {'handle': 'glowco', 'bio': 'Clean skincare', 'followers': 12000}
        self.assertNotIn('ALREADY GATHERED', deepscout.prompt(p))
        found = {'results': [{'title': 'Glow Co - clean skincare', 'url': 'https://glow.co/about', 'snippet': 'Founded by Ann'}], 'site': 'glow.co: shop'}
        text = deepscout.prompt(p, found)
        self.assertIn('ALREADY GATHERED', text)
        self.assertIn('Founded by Ann', text)
        self.assertTrue(text.startswith('Vet @glowco'))

    def test_grok_is_4_7(self):
        self.assertIn('grok-4.7', deepscout.MODELS['grok']['args'])


if __name__ == '__main__':
    unittest.main()
