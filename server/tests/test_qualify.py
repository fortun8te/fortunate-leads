import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qualify as q  # noqa: E402

ME = 'fortun8te'


def P(handle, bio=None, name=None, website=None, category=None, followers=None, following=None, posts=None,
      is_private=0, is_verified=0, is_business=None):
    return dict(handle=handle, bio=bio, name=name, website=website, category=category, followers=followers, following=following,
                posts=posts, is_private=is_private, is_verified=is_verified, is_business=is_business)


def E(*seeds):
    return [{'seed': s.split(':')[0], 'direction': s.split(':')[1] if ':' in s else 'followers'} for s in seeds]


# (person, edges, expected roles (any of), must-have tags, must-not tags)
# First block: real bios from the old research DB. Rest: realistic synthetic profiles.
LABELLED = [
    (P('connorpugs', '📈250,000,000+ views per month 💰 Scaling companies with social media @pugs.media 📩 connor@pugs.media 👇more from me',
       'Connor Flannery', 'https://link.me/connorpugs', followers=1300000, is_verified=1), E('lukas.ntambala'),
     {'unrelated', 'connector'}, {'Creator', 'Email', 'Link Hub', '1M+', 'Verified'}, {'Brand'}),
    (P('dumbsmoneytv', "We're 3 full-time investors who quit our day jobs & made $30M+ investing 💰😳 experiences on YouTube We share our",
       'Dumb Money', 'https://youtube.com/dumbmoneylive', followers=262), E('lukas.ntambala'),
     {'unrelated', 'unclear'}, {'<1k'}, {'Brand', 'Scaling', 'Shop Link', 'Agency'}),
    (P('blumen_ofbasel', '🌷 Ewige Blumen 🎀 Personalisierte Blumen nach Wunsch 📍 Basel & Umgebung 🇨🇭 🚚 Wochenend-Lieferung möglich 💌 Bestellungen',
       'Blumenstrauß Bale', category='Florist', followers=155), E('noah_nahar'),
     {'unclear', 'unrelated', 'buyer'}, set(), {'US', 'NL', 'Agency'}),
    (P('aarondambruso', '22 giving it all Co-Founder @allvantiq Let’s grow together 🤝', 'Aaron D‘Ambruso', 'https://allvantiq.com',
       'Entrepreneur', 2097), E('lukas.ntambala', 'shepscales'),
     {'unclear'}, {'Founder', 'via @shepscales', 'in 2 lists'}, {'Brand', 'Personal'}),
    (P('promoting', '📈 £25M+ in landscaping projects closed with our system 🌱 APL & HTA Member 📅 Book 6 months ahead & grow your team',
       'Matt Freestone', 'https://newleafmediaco.com', followers=543), E('lukas.ntambala'),
     {'peer', 'unclear', 'unrelated'}, {'UK'}, {'Brand', 'Shop Link'}),
    (P('bold.builds.construction', '💥 Water Damage • Remodels • ADUs 📍 San Diego | 📲 DM for quotes 📞 619-648-3773 Main office', 'Eric Aguilera',
       'https://boldbuildsinc.com', followers=15600, is_verified=1), E('lukas.ntambala'),
     {'unclear', 'unrelated'}, {'US', '10k-100k'}, {'Brand', 'Agency'}),
    (P('detailersroadmap', '🎖️We scale 6, 7, & 8 figure detail shops 🏁 Partners in 1000s of Auto Businesses 🚨 Follow to see all of our tactics',
       'Detailers Roadmap - Detailing Websites & SEO', 'https://www.detailersroadmap.com', 'Entrepreneur', 2138), E('lukas.ntambala'),
     {'peer'}, {'Agency'}, {'Brand', 'Shop Link'}),
    (P('colin.schofield', '📍🇨🇴 Medellin | Boston | former salad-eater Founder @grsfd.ai', 'Colin Schofield', 'https://grsfd.ai', 'Gamer', 5940),
     E('shepscales'), {'unrelated', 'unclear'}, {'Founder', 'SaaS', 'US'}, {'Brand'}),
    (P('nathanbrownpro', 'Architectural Photographer Seattle // OC If you’re a real estate photographer watch this 👇', 'Nathan Brown',
       'https://youtu.be/JklNRhMw_v0', followers=3449), E('lukas.ntambala'), {'collaborator', 'peer', 'unrelated'}, {'Creative', 'US'}, {'Brand'}),
    (P('ismailhacking', 'better in real life cofounder + coo: @revvideoproductions 📍 san diego, but probably traveling 🌎', 'Ismail Hacking',
       followers=2580), E('lukas.ntambala'), {'unclear', 'collaborator'}, {'Founder', 'US'}, {'Brand'}),
    # --- buyers
    (P('glowlabskin', 'Clean skincare for sensitive skin 🌿 Founded by @sarahmills | Ships worldwide | Shop below 👇', 'Glow Lab Skincare',
       'https://glowlab.co/collections/all', 'Health/beauty', 24000, 800, 410, is_business=1), E('shepscales', 'noah_nahar', 'bramkoningsecom'),
     {'buyer'}, {'Brand', 'Skincare', 'Shop Link', 'in 3 lists', '10k-100k', 'Business'}, {'Agency', 'Personal'}),
    (P('sarahmills', 'Founder & CEO @glowlabskin 🧴 mom of 2 | NYC', 'Sarah Mills', followers=3100), E('shepscales', 'noah_nahar'),
     {'buyer', 'unclear'}, {'Founder', 'US', 'in 2 lists'}, {'Agency'}),
    (P('rootedapparel', 'Sustainable streetwear made in USA 🇺🇸 New drop 10.04 | Free shipping over $75', 'ROOTED Apparel Co.',
       'https://rootedapparel.com', 'Clothing (Brand)', 48000, is_business=1), E('markbuildsbrands'),
     {'buyer'}, {'Brand', 'Apparel', 'US'}, {'Agency', 'Creator'}),
    (P('daan.vermeer', 'Oprichter @puurhond 🐶 Natuurlijke hondensnacks | Webshop 👇 | Amsterdam', 'Daan Vermeer', 'https://puurhond.nl',
       followers=1900), E('bramkoningsecom', 'rutdeletter'),
     {'buyer'}, {'Founder', 'NL', 'Pets', 'Store'}, {'US', 'Agency'}),
    (P('studio.lumen.nl', 'Webwinkel voor woondecoratie & kaarsen 🕯️ Gratis verzending vanaf €50', 'Studio Lumen', 'https://studiolumen.nl/shop',
       followers=7200, is_business=1), E('rutdeletter'),
     {'buyer'}, {'Store', 'Home', 'NL', 'Shop Link'}, {'US', 'Agency'}),
    (P('fuelgummies', 'Creatine gummies that actually taste good 🍓 Use code FUEL10 for 10% off 📦 US & CA', 'FUEL', 'https://fuelgummies.myshopify.com',
       'Product/service', 12000, is_business=1), E('shepscales', 'floris.ads'),
     {'buyer'}, {'Brand', 'Supplements', 'Shopify', 'Shop Link'}, {'Agency'}),
    (P('mikeoperates', 'ops @ a 8fig dtc brand | scaling physical products | austin tx', 'Mike R.', followers=900), E('shepscales', 'leoxmoore', 'noah_nahar'),
     {'buyer', 'unclear'}, {'Scaling', 'US', 'in 3 lists'}, {'Personal', 'Creator'}),
    (P('auracandleco', 'Hand-poured soy candles 🕯 Handmade in Portland | Stockists ↓', 'Aura Candle Co', 'https://auracandle.co',
       followers=5400, is_business=1), E('noah_nahar'), {'buyer'}, {'Brand', 'Home'}, {'Agency'}),
    (P('lunajewels', 'Waterproof gold jewelry ✨ 18k plated | Shop now 👇 | 50k+ happy customers', 'Luna Jewels', 'https://lunajewels.com',
       followers=86000, is_business=1), E('adswithcami', 'floris.ads'), {'buyer'}, {'Brand', 'Jewelry', 'Shop Link'}, {'Agency'}),
    # --- connectors
    (P('floris.ads', 'Meta ads for ecom brands 📈 Media buyer | €20M+ managed | DM "SCALE"', 'Floris | Ecom Ads', followers=8800),
     E('fortun8te:following', 'rutdeletter'), {'connector'}, {'Freelancer', 'knows you', 'you follow'}, {'Brand', 'Personal'}),
    (P('peakgrowth.agency', 'We help DTC brands scale with paid social 🚀 Clients: 7-8 figure Shopify brands | Book a call ↓', 'Peak Growth Agency',
       'https://calendly.com/peakgrowth', 'Advertising/Marketing', 4100, is_business=1), E('markbuildsbrands', 'shepscales'),
     {'connector'}, {'Agency', 'Scaling', 'Shopify'}, {'Brand', 'Shop Link'}),
    (P('emailwithjess', 'Freelance email marketer for Shopify brands 💌 Klaviyo expert | jess@emailwithjess.com', 'Jess | Email Marketing',
       'https://emailwithjess.com', followers=2300), E('everflw.email'), {'connector'}, {'Freelancer', 'Email', 'Shopify'}, {'Brand', 'Agency'}),
    (P('cro.tom', 'CRO specialist for e-commerce stores | +32% CVR avg | UK 🇬🇧', 'Tom Hughes', followers=1200), E('noah_nahar'),
     {'connector'}, {'Freelancer', 'UK'}, {'Brand'}),
    # --- collaborators / peers
    (P('frameproductstudio', 'Product photographer for skincare & supplement brands 📸 LA based | Bookings: hello@framestudio.co', 'Frame Studio',
       'https://framestudio.co', followers=6100), E('adswithcami'), {'collaborator'}, {'Creative', 'Email', 'US'}, {'Brand'}),
    (P('ugcbylisa', 'UGC creator 🎥 beauty & wellness brands | portfolio 👇', 'Lisa | UGC', 'https://linktr.ee/ugcbylisa', followers=3300),
     E('adswithcami'), {'collaborator'}, {'Creative', 'Link Hub'}, {'Brand'}),
    (P('adcreativesbymax', 'I design static ad creatives for DTC brands 🎨 Portfolio below', 'Max | Ad Creatives', followers=1500), E('floris.ads'),
     {'peer', 'connector', 'collaborator', 'unclear'}, set(), {'Brand', 'Personal'}),
    (P('realtymarketingpro', 'Marketing agency for realtors 🏡 We help real estate agents get 20+ listings a month', 'Realty Marketing Pro', followers=2200),
     E('lukas.ntambala'), {'peer'}, {'Agency'}, {'Brand'}),
    # --- suppliers, SaaS, coaches, creators
    (P('packcraft.co', 'Custom packaging for DTC brands 📦 Mailer boxes, low MOQ | Private label friendly', 'PackCraft', 'https://packcraft.co',
       followers=9000, is_business=1), E('shepscales'), {'supplier'}, {'Supplier'}, {'Brand'}),
    (P('adbotai', 'AI-powered ad copy tool for marketers 🤖 Try free 👇', 'AdBot AI', 'https://adbot.ai', followers=4000), E('aditor.ai'),
     {'unrelated'}, {'SaaS'}, {'Brand'}),
    (P('ecomkinglucas', 'I teach you how to start a 6 figure Shopify store 💸 Free training 👇 1:1 mentorship', 'Lucas | Ecom Mentor',
       'https://stan.store/ecomking', followers=41000), E('lukas.ntambala'), {'unrelated'}, {'Coach', 'Link Hub'}, {'Brand', 'Store', 'Shop Link'}),
    (P('fitwithkayla', 'Fitness influencer 💪 Collabs: kayla@mgmt.com | Code KAYLA15 at @gymshark', 'Kayla Brooks', followers=820000, is_verified=1),
     E('lukas.ntambala'), {'unrelated'}, {'Creator', '100k-1M'}, {'Brand'}),
    # --- personal / spam
    (P('jake_22', '22 | Uni of Amsterdam 🎓 | he/him', 'Jake', followers=450), E('rutdeletter'), {'unrelated'}, {'Personal'}, {'Brand', 'Founder'}),
    (P('emma.v', 'living my best life ☀️ 📍 Rotterdam', 'Emma', followers=780), E('rutdeletter'), {'unrelated', 'unclear'}, set(),
     {'Brand', 'Founder', 'Agency'}),
    (P('crypto_signals_9921', 'Daily forex & crypto signals 📈 DM for VIP', None, followers=90, following=5000), E('lukas.ntambala'),
     {'unclear', 'unrelated'}, set(), {'Brand', 'Founder', 'Scaling'}),
    (P('ninafuller', 'dog mom 🐶 coffee lover ☕ | nyc', 'Nina', followers=600), E('noah_nahar'), {'unclear', 'unrelated'}, {'US'},
     {'Pets', 'Food & Drink', 'Brand'}),
]


class Labelled(unittest.TestCase):
    def run_all(self):
        out = []
        for person, edges, roles, must, mustnot in LABELLED:
            tags = q.rule_tags(person, edges, ME)
            names = {t for t, _ in tags}
            out.append((person, edges, tags, names, q.rule_verdict(person, tags), roles, must, mustnot))
        return out

    def test_tags_precise(self):
        for person, _, tags, names, v, roles, must, mustnot in self.run_all():
            with self.subTest(handle=person['handle']):
                self.assertTrue(must <= names, f'missing {must - names} in {sorted(names)}')
                self.assertFalse(mustnot & names, f'wrong {mustnot & names}')
                for t, g in tags:
                    self.assertIn(g, q.TAG_GROUPS)
                    self.assertTrue(g == 'source' or q.TAXONOMY.get(t) == g or g == 'size', (t, g))

    def test_roles(self):
        for person, _, _, names, v, roles, *_ in self.run_all():
            with self.subTest(handle=person['handle']):
                self.assertIn(v['role'], roles, v['reason'])
                self.assertIn(v['tier'], ('hot', 'warm', 'cold'))
                self.assertTrue(v['reason'].endswith('.') and len(v['reason']) < 160, v['reason'])

    def test_buyers_rank_above_the_rest(self):
        rows = self.run_all()
        buyers = [r[4]['score'] for r in rows if r[5] == {'buyer'}]
        others = [r[4]['score'] for r in rows if 'buyer' not in r[5] and 'connector' not in r[5]]
        self.assertGreater(min(buyers), max(others))
        self.assertTrue(all(r[4]['tier'] == 'hot' for r in rows if r[5] == {'buyer'} and 'in 3 lists' in r[3]))


class Pieces(unittest.TestCase):
    def test_unread_without_bio(self):
        p = P('someone', name='Some One', is_private=1)
        v = q.rule_verdict(p, q.rule_tags(p, E('shepscales'), ME))
        self.assertEqual(v['tier'], 'unread')
        self.assertIn('via @shepscales', v['reason'])

    def test_source_tags(self):
        tags = dict(q.rule_tags(P('x', 'hi'), E('fortun8te:followers', 'fortun8te:following', 'shepscales', 'noah_nahar:following'), ME))
        for t in ('via @shepscales', 'via @noah_nahar', 'in 3 lists', 'knows you', 'follows you', 'you follow'):
            self.assertEqual(tags.get(t), 'source', t)
        self.assertNotIn('via @fortun8te', tags)

    def test_size_bands(self):
        for f, band in ((10, '<1k'), (1000, '1k-10k'), (99999, '10k-100k'), (100000, '100k-1M'), (2000000, '1M+')):
            self.assertIn((band, 'size'), q.rule_tags(P('x', followers=f), [], None))

    def test_prefilter_ranks(self):
        good = q.prefilter(P('glowskincare.official', name='Glow Skincare', followers=5000, is_business=1), ['a', 'b', 'c'])
        founder = q.prefilter(P('mark.founder', name='Mark | Founder @brand'), ['a', 'b'])
        plain = q.prefilter(P('jan.devries', name='Jan de Vries'), ['a'])
        spam = q.prefilter(P('memes_daily_4821', name=''), ['a'])
        private = q.prefilter(P('shopcozy', name='Cozy Shop', is_private=1), ['a', 'b', 'c'])
        self.assertTrue(good > founder > plain > spam, (good, founder, plain, spam))
        self.assertLessEqual(private, 35)
        self.assertTrue(all(0 <= s <= 100 for s in (good, founder, plain, spam, private)))

    def test_prefilter_handle_traps(self):
        # word pieces inside ordinary names must not look like business signals
        for h in ('marco.rossi', 'roadsidejoe', 'leadsbylisa_personal'):
            self.assertLess(q.prefilter(P(h, name=h), ['a']), 50, h)

    def test_input_hash(self):
        a = q.input_hash(P('x', 'bio'), E('a', 'b'))
        self.assertEqual(a, q.input_hash(P('x', 'bio'), list(reversed(E('a', 'b')))))
        self.assertNotEqual(a, q.input_hash(P('x', 'bio2'), E('a', 'b')))
        self.assertNotEqual(a, q.input_hash(P('x', 'bio'), E('a')))

    def test_parse_json(self):
        self.assertEqual(q.parse_json('```json\n{"role":"buyer","fit":80}\n```')['fit'], 80)
        self.assertEqual(q.parse_json('<think>{no}</think> ok {"a": {"b":1}}'), {'a': {'b': 1}})
        self.assertIsNone(q.parse_json('nothing here'))

    def test_llm_unavailable_returns_none(self):
        old = q.PROXY
        q.PROXY = 'http://127.0.0.1:9/none'
        try:
            self.assertIsNone(q.llm_verdict(P('x', 'bio'), [], [], timeout=1))
        finally:
            q.PROXY = old


@unittest.skipUnless(os.environ.get('FL_LIVE_LLM'), 'set FL_LIVE_LLM=1 to run the live model pass')
class LiveLLM(unittest.TestCase):
    def test_agreement(self):
        hit = total = 0
        for person, edges, roles, *_ in LABELLED:
            tags = q.rule_tags(person, edges, ME)
            v = q.llm_verdict(person, tags, edges)
            if v is None:
                print(f"{person['handle']:24} no model reply")
                continue
            total += 1
            ok = v['role'] in roles
            hit += ok
            print(f"{'ok ' if ok else 'XX '}{person['handle']:24} {v['role']:12} {v['score']:3} {v['model']:32} {v['reason']} {v['tags']}")
        print(f'LLM agreement {hit}/{total}')
        self.assertGreater(total, 0)


if __name__ == '__main__':
    unittest.main()
