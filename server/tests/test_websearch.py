"""Web research: queries, relevance filtering, caching and packet lines, with SearXNG and websites faked."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import qualify
import websearch

PERSON = {'id': 1, 'handle': 'nounnaturals', 'name': 'Noun Naturals', 'website': 'nounnaturals.com', 'bio': 'Clean hair care'}
HITS = [
    {'title': 'Noun Naturals - LinkedIn', 'url': 'https://linkedin.com/company/noun', 'snippet': 'Personal care, 2-10 employees'},
    {'title': 'Khaled - Founder and CEO @ Noun Naturals', 'url': 'https://crunchbase.com/p/k', 'snippet': 'Founder of Noun Naturals'},
    {'title': 'Elevator - Wikipedia', 'url': 'https://en.wikipedia.org/wiki/Elevator', 'snippet': 'A lift'},
    {'title': 'nounnaturals', 'url': 'https://www.instagram.com/nounnaturals/', 'snippet': 'Instagram profile'},
] + [{'title': f'Noun Naturals page {i}', 'url': f'https://nounnaturals.com/p{i}', 'snippet': 'Shop'} for i in range(4)]


class WebResearchTest(unittest.TestCase):
    def test_queries_find_the_business_and_who_runs_it(self):
        qs = websearch.queries(PERSON)
        self.assertEqual(qs[0], '"Noun Naturals" nounnaturals')
        self.assertIn('-site:nounnaturals.com', qs[1])
        self.assertIn('founder', websearch.queries(dict(PERSON, website='linktr.ee/x'))[1])

    def test_lookup_keeps_relevant_varied_results(self):
        with patch.object(websearch, 'search', return_value=HITS), patch.object(websearch, 'read_website', return_value='site text'):
            found = websearch.lookup(PERSON)
        urls = [r['url'] for r in found['results']]
        self.assertNotIn('https://en.wikipedia.org/wiki/Elevator', urls)          # does not name them
        self.assertFalse(any('instagram.com' in u for u in urls))               # repeats the profile
        self.assertEqual(sum('nounnaturals.com' in u for u in urls), 2)          # two pages per site
        self.assertEqual(found['site'], 'site text')

    def test_cache_follows_profile_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.init(str(Path(tmp) / 't.sqlite'))
            websearch.ensure(conn)
            conn.execute("INSERT INTO people(id,handle,first_seen,updated_at) VALUES(1,'nounnaturals','x','x')")
            websearch.store(conn, PERSON, {'results': HITS[:1], 'site': 'site'})
            self.assertEqual(websearch.cached(conn, PERSON)['site'], 'site')
            self.assertIsNone(websearch.cached(conn, dict(PERSON, website='other.com')))
            conn.close()

    def test_web_lines_reach_the_packet_and_count_as_evidence(self):
        found = {'results': HITS[1:2], 'site': 'nounnaturals.com: store on Shopify'}
        person = dict(PERSON, web_lines=websearch.lines(found), web_text=websearch.text(found))
        packet = qualify._packet(person, [], [])
        self.assertIn('WEB RESEARCH', packet)
        self.assertIn('Founder of Noun Naturals', packet)
        v = {'role': 'unclear', 'fit': 50, 'evidence': ['Founder of Noun Naturals'], 'reason': 'Runs the brand'}
        self.assertIsNotNone(qualify._verdict(v, person, [], 'm', 'q'))
        self.assertIsNone(qualify._verdict(v, dict(PERSON), [], 'm', 'q'))   # without research the quote is not theirs

    def test_offline_switch_disables_research(self):
        self.assertFalse(websearch.available())


if __name__ == '__main__':
    unittest.main()
