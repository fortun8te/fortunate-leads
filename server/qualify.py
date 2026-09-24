"""Lead qualifier for Fortunate Leads: prefilter (no bio), rule tags, rule verdict, one LLM verdict.

Pure functions except llm_verdict, which calls the local OpenRouter proxy (free models only).
Precision over recall everywhere: a wrong tag is worse than a missing one.
"""
from __future__ import annotations
import hashlib
import http.client
import json
import re
import time
import urllib.error
import urllib.request

TAG_GROUPS = ('role', 'niche', 'signal', 'size', 'source')
PROXY = 'http://127.0.0.1:18741/api/v1/chat/completions'
MODELS = ('z-ai/glm-5.2:free', 'google/gemma-4-31b-it:free', 'nvidia/nemotron-3-super-120b-a12b:free')
PROMPT_VERSION = 'q1'
TAGS_VERSION = 't2'   # bump when rule tags change: the server re-derives everyone's auto tags once (LLM verdicts are kept)
ROLES = ('buyer', 'connector', 'collaborator', 'peer', 'supplier', 'unrelated', 'unclear')

# ---------------------------------------------------------------- taxonomy
def _rx(*words, flags=re.I):
    return re.compile(r'(?<![\w@])(?:' + '|'.join(words) + r')(?!\w)', flags)

ROLE_RX = {
    'Agency': _rx(r'(?:ad|ads|marketing|creative|growth|performance|media|ecom|e-?commerce|dtc|email|branding|design|digital|paid social|social media|content)\s+agency',
                  r'agency', r'bureau', r'we (?:help|scale|grow|build)\b.{0,40}\b(?:brands?|stores?|shops?|compan\w+|businesses)',
                  r'growth partner', r'media buying', r'performance marketing', r'paid (?:social|ads)', r'klaviyo (?:partner|agency)',
                  r'scaling (?:ecom|e-?commerce|dtc|brands?|companies)'),
    'Freelancer': _rx(r'freelancer?', r'zzp(?:er)?', r'copywriter', r'media buyer', r'ads manager', r'email (?:marketer|strategist|specialist)',
                      r'cro (?:specialist|expert)', r'klaviyo (?:expert|specialist)', r'shopify (?:developer|expert|dev)'),
    'Creative': _rx(r'(?:product |commercial |architectural |food |fashion )?photographer', r'photography', r'videographer', r'ugc(?: creator)?',
                    r'3d (?:artist|designer|renders?|visuals?)', r'cgi', r'retoucher', r'art director', r'graphic designer', r'motion designer',
                    r'video production', r'productfotograaf', r'fotograaf', r'(?:static )?ad creatives?', r'ad designer'),
    'Creator': _rx(r'influencer', r'content creator', r'youtuber', r'podcast(?: host)?', r'streamer', r'for collabs?', r'brand deals', r'pr friendly',
                   r'mgmt', r'management:', r'views (?:a|per) month', r'\d+[km]?\+? views'),
    'Supplier': _rx(r'manufactur\w+', r'private label', r'oem', r'odm', r'fulfil+ment (?:center|centre|services?|partner|for)', r'3pl',
                    r'(?:custom |branded |sustainable )?packaging (?:solutions?|supplier|manufacturer|company|for (?:brands|dtc|e-?commerce)|design)',
                    r'custom packaging', r'factory', r'sourcing agent', r'wholesale supplier', r'supplier', r'white label'),
    'SaaS': _rx(r'saas', r'software', r'ai[- ]powered', r'api', r'ai (?:tool|platform|agent)s?', r'app for'),
    'Coach': _rx(r'coach(?:ing)?', r'mentor(?:ship)?', r'(?<!of )(?<!golf )(?<!main )(?<!crash )courses?', r'masterclass', r'academy', r'i teach', r'students', r'1:1', r'free training',
                 r'helping \w+ (?:make|earn|hit)'),
    'Store': _rx(r'boutique', r'concept store', r'online (?:store|shop)', r'webshop', r'web ?winkel', r'winkel', r'retailer', r'multi-?brand store',
                 r'stockist of', r'curated (?:shop|store|goods)'),
    'Brand': _rx(r'shop (?:now|below|our|the|online|here)', r'shop ?(?:👇|⬇️?|↓|:)', r'official (?:account|page)', r'free shipping',
                 r'(?:ships|shipping) (?:worldwide|nationwide|internationally)', r'ships (?:to|across|within) (?:the )?(?:us|usa|eu|uk|canada|u\.s\.)',
                 r'worldwide shipping', r'gratis verzending', r'use code', r'code \w+ for', r'\d+% off', r'new (?:drop|collection)', r'restock\w*',
                 r'back in stock', r'sold out', r'handmade', r'handcrafted', r'hand-?poured', r'small[- ]batch', r'est\.? ?(?:19|20)\d\d',
                 r'available (?:at|in|on)(?! (?:spotify|apple|all (?:streaming )?platforms|youtube|podcasts?|soundcloud|tidal|netflix|twitch|patreon|onlyfans))',
                 r'sold (?:at|in)', r'stockists?', r'tag us', r'\d+k?\+? (?:happy )?customers', r'(?:family|woman|women|black|veteran|latina?)[- ]owned',
                 r'made in (?:usa|the usa|america|nl|holland|italy|portugal)', r'(?:designed|made|roasted|crafted|poured) in (?:small batches|la|nyc)',
                 r'(?:skincare|skin care|clothing|apparel|fashion|jewelry|jewellery|beauty|supplement|pet|coffee|candle|dtc|d2c|e-?com(?:merce)?|consumer)'
                 r' (?:brand|company|label|co)(?! owners?| founders?| operators?| marketers?)'),
    'Personal': _rx(r'student', r'uni(?:versity)?', r'he/him', r'she/her', r'they/them', r'personal account', r'photo ?dump', r'god first',
                    r'mom of \d', r'dad of \d', r'wife', r'husband', r'\d\d ?y/?o', r'living my best life', r'dog mom', r'cat mom', r'mama to'),
}
# a promo code for someone else's brand is a creator signal, not a brand one
PROMO = re.compile(r'(?:use )?(?:my )?code:? ?[\w-]+ (?:[^\s@]+ ){0,5}(?:(?:at|on|for|with) )?@[\w.]+|\d+% off (?:at|on|with) @[\w.]+|'
                   r'(?:ambassador|affiliate|partner(?:ed)? with|athlete) (?:for |of |with )?@[\w.]+', re.I)
AGENCY_NOT = re.compile(r'(?:model(?:ing)?|talent|travel|casting|real estate|insurance|staffing|recruit\w*|booking) agency|'
                        r'signed (?:to|with|@|by)|represented by|\bmgmt\b|management:', re.I)
LOCAL_SERVICE = re.compile(r'\b(?:landscap\w*|real estate|realtors?|detail(?:ing|ers)?(?: shops?)?|contractors?|roof\w*|dentists?|restaurants?|'
                           r'local business\w*|law firms?|med ?spas?|gyms?|chiropractors?|plumb\w+|hvac|remodels?|construction|coaches|saas)\b', re.I)
SPAM = re.compile(r'\b(?:forex|crypto signals?|signals|dm for vip|follow for follow|f4f|giveaway page|cash ?app|onlyfans|sugar daddy|'
                  r'binary options|pips?)\b', re.I)
ECOM_TAG = re.compile(r'\b(?:ecom\w*|e-?commerce|dtc|d2c|direct[- ]to[- ]consumer|shopify|amazon fba|online stores?)\b', re.I)
ECOM = re.compile(r'\b(?:ecom\w*|e-?commerce|dtc|d2c|shopify|klaviyo|product brands?|(?:consumer |physical )?brands|amazon fba|online stores?)\b', re.I)

NICHE_RX = {
    'Skincare': _rx(r'skin ?care', r'serums?', r'moisturi[sz]ers?', r'spf', r'sunscreen', r'acne', r'huidverzorging', r'retinol',
                    r'cleansers?', r'face (?:oils?|masks?|wash)', r'body (?:butter|wash|lotion|oil)', r'tallow', r'lip balms?', r'eczema'),
    'Beauty': _rx(r'beauty', r'cosmetics?', r'make-?up', r'lashes', r'hair ?care', r'fragrances?', r'perfumes?', r'lipsticks?', r'nail polish',
                  r'shampoo', r'conditioner', r'hair (?:oil|growth|products)', r'deodorant'),
    'Supplements': _rx(r'supplements?', r'vitamins?', r'multivitamins?', r'protein (?:powders?|shakes?|blends?)', r'whey', r'creatine',
                       r'collagen', r'gummies', r'nootropics?', r'pre-?workout', r'electrolytes?', r'greens powder', r'supplementen',
                       r'magnesium', r'ashwagandha', r'probiotics?', r'sea moss', r'colostrum', r'omega[- ]?3s?'),
    'Apparel': _rx(r'apparel', r'clothing', r'streetwear', r'activewear', r'athleisure', r'swimwear', r'loungewear', r'lingerie', r'fashion (?:brand|label)',
                   r'hoodies', r'tees', r't-?shirts', r'sneakers', r'kleding', r'menswear', r'womenswear', r'denim', r'leggings', r'underwear',
                   r'knitwear', r'outerwear', r'sportswear', r'workwear', r'kidswear', r'socks'),
    'Jewelry': _rx(r'jewel(?:le)?ry', r'jewelery', r'necklaces?', r'bracelets?', r'earrings?', r'sieraden', r'watches', r'rings',
                   r'pendants?', r'charms', r'sterling silver', r'(?:14|18)k (?:gold|plated)', r'gold[- ]plated', r'demi-?fine'),
    'Home': _rx(r'home ?decor', r'homeware', r'candles?', r'furniture', r'bedding', r'kitchenware', r'cookware', r'ceramics', r'rugs', r'home goods',
                r'woondecoratie', r'interior (?:brand|products)', r'linens?', r'towels', r'mattress(?:es)?', r'pillows?', r'home fragrance',
                r'diffusers?', r'wall art', r'tableware', r'glassware', r'drinkware', r'tumblers?', r'water bottles?', r'planters?'),
    'Pets': _rx(r'pet (?:products|supplies|food|brand|care|accessories|wellness|supplements|shop|store)',
                r'dog (?:treats|food|toys|accessories|collars?|beds?|brand|harness\w*|leash\w*|shampoo|chews|supplements)',
                r'cat (?:food|toys|litter|treats|trees?)', r'for (?:dogs|cats|pets)', r'for your (?:dog|cat|pup|pet)s?', r'pet parents',
                r'hondensnacks', r'dierenwinkel'),
    'Coffee': _rx(r'coffee (?:roasters?|brand|company|co|beans|subscription)', r'roastery', r'roasters', r'specialty coffee', r'cold brew',
                  r'espresso (?:beans|blend)', r'(?:roasted|roasting) (?:in|coffee)'),
    'Food & Drink': _rx(r'hot sauce', r'sauces', r'seasonings?', r'spice (?:blends?|mixes?)', r'snacks?', r'chocolates?', r'matcha', r'energy drinks?',
                        r'spirits', r'craft beer', r'granola', r'kombucha', r'protein bars?', r'tea (?:brand|company)', r'teas', r'wines?', r'jerky',
                        r'olive oil', r'honey', r'cookies', r'candy', r'soda', r'sparkling water', r'functional (?:drinks?|beverages?|soda)',
                        r'beverages?', r'drink mix', r'mocktails?', r'non-?alcoholic', r'tequila', r'mezcal', r'vodka', r'whiske?y', r'bourbon'),
    'Fitness': _rx(r'fitness', r'gym ?wear', r'workouts?', r'resistance bands', r'yoga', r'pilates', r'home gym', r'gym equipment',
                   r'kettlebells?', r'dumbbells?', r'lifting (?:straps|belts?)'),
    'Wellness': _rx(r'wellness', r'wellbeing', r'self-?care', r'aromatherapy', r'essential oils', r'cbd', r'gut health', r'functional mushrooms?',
                    r'sleep (?:aids?|masks?|support|gummies|tea|products)', r'better sleep', r'adaptogens?'),
    'Baby': _rx(r'baby (?:products|brand|gear|clothing|clothes|care|skincare|food|carriers?|essentials|shop|store|boutique|toys|bottles?|'
                r'registry|must-?haves)', r'for (?:babies|toddlers|little ones|kids)', r'kids(?:wear|\' clothing| clothing| toys| products| furniture)',
                r'nursery (?:decor|furniture)', r'maternity', r'nursing (?:bras?|pillows?|wear)', r'teething', r'strollers?', r'diapers?',
                r'swaddles?', r'onesies', r'montessori'),
    'Accessories': _rx(r'handbags?', r'bags', r'backpacks?', r'sunglasses', r'eyewear', r'wallets?', r'belts', r'hats', r'scarves',
                       r'leather goods', r'totes?', r'purses?', r'hair (?:clips|accessories)', r'phone straps?'),
    'Outdoor': _rx(r'outdoor', r'camping', r'hiking', r'fishing', r'hunting', r'surf', r'cycling', r'overlanding'),
    'Tech Gadgets': _rx(r'gadgets?', r'electronics', r'phone cases?', r'headphones', r'chargers?', r'smart home', r'tech accessories'),
}
SIGNAL_RX = {
    'Founder': _rx(r'founder', r'co-?founder', r'ceo(?! of (?:my|me|chaos|vibes|life|being|the (?:house|family)))',
                   r'(?<!dog )(?<!cat )(?<!pet )(?<!home )(?<!car )(?<!proud )(?<!horse )(?:co-?)?owner(?! of (?:a|an|two|three|\d) (?:[\w-]+ ){0,2}(?:dog|cat|pup|puppy|horse|retriever|doodle|frenchie|pet)s?\b)',
                   r'oprichter', r'eigenaar', r'building @\w+', r'founded @\w+', r'cofounder', r'founder of', r'creator of @\w+'),
    'Scaling': _rx(r'[6-9][- ]?fig(?:ure)?s?', r'[6-9]fig', r'scaling', r'\$\d+(?:\.\d+)?\s?m\+? (?:in )?(?:revenue|sales|rev|arr)'),
    'Hiring': _rx(r'hiring', r'join (?:our|the) team', r'vacatures?', r'careers'),
}
US_STATES = r'AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC'
US_RX = [re.compile(r'🇺🇸|\b(?:USA|LA|U\.S\.)\b'), re.compile(r'\b(?:nyc|usa|socal|norcal)\b', re.I),
         re.compile(r'[a-z], ?(?:' + US_STATES + r')\b'), re.compile(r'\bUS(?:[- ]based| ?(?:&|and|\+|/) ?(?:CA|Canada|EU|UK)\b)'),
         _rx(r'united states', r'us-?based', r'new york', r'los angeles', r'san diego', r'san francisco', r'miami', r'austin', r'dallas', r'houston',
             r'chicago', r'boston', r'seattle', r'denver', r'atlanta', r'nashville', r'brooklyn', r'california', r'texas', r'florida', r'arizona', r'scottsdale',
             r'portland', r'phoenix', r'philadelphia', r'las vegas', r'charlotte', r'orlando', r'tampa', r'minneapolis', r'detroit', r'new jersey',
             r'colorado', r'utah', r'oregon', r'north carolina', r'georgia, usa', r'bay area', r'san jose', r'sacramento', r'salt lake city',
             r'columbus', r'indianapolis', r'kansas city', r'st\.? louis', r'pittsburgh', r'baltimore', r'raleigh', r'charleston', r'savannah',
             r'honolulu', r'hawaii', r'new orleans', r'milwaukee', r'cincinnati', r'oklahoma', r'washington,? d\.?c\.?', r'virginia', r'michigan',
             r'ohio', r'pennsylvania', r'massachusetts', r'tennessee', r'idaho', r'montana', r'nevada', r'new mexico', r'wisconsin', r'minnesota',
             r'missouri', r'kentucky', r'alabama', r'louisiana', r'south carolina', r'connecticut', r'maryland', r'american[- ]made',
             r'ships? (?:across|within|to) the (?:us|usa|u\.s\.)')]
NL_RX = [re.compile(r'🇳🇱|\bNL\b'),
         _rx(r'netherlands', r'nederland', r'holland', r'dutch', r'amsterdam', r'rotterdam', r'utrecht', r'den haag', r'eindhoven', r'groningen',
             r'oprichter', r'eigenaar', r'winkel', r'webwinkel', r'gratis verzending', r'bezorging', r'zzp', r'haarlem', r'leiden', r'delft',
             r'arnhem', r'nijmegen', r'breda', r'tilburg', r'maastricht', r'zwolle', r'amersfoort', r'den bosch', r"'s-hertogenbosch",
             r'hilversum', r'nederlandse', r'bestel (?:nu|online)')]
UK_RX = [re.compile(r'🇬🇧|£|\bUK\b'), _rx(r'united kingdom', r'london', r'manchester', r'england', r'scotland', r'birmingham')]
LINK_HUBS = ('linktr.ee', 'beacons.ai', 'lnk.bio', 'link.me', 'stan.store', 'campsite.bio', 'bio.site', 'taplink', 'hoo.be', 'msha.ke',
             'allmylinks', 'solo.to', 'linkin.bio', 'tap.bio', 'snipfeed', 'koji.to', 'withkoji', 'komi.io', 'linkpop.com', 'shor.by',
             'direct.me', 'milkshake.app', 'carrd.co', 'flowcode.com')
SHOPIFY_HOSTS = ('myshopify.com', 'shop.app', 'linkpop.com')      # Shopify's own storefront / link-in-bio products
MARKETPLACES = ('etsy.com', 'etsy.me', 'amzn.to', 'bigcartel.com', 'square.site', 'faire.com', 'bol.com')
CREATOR_SHOPS = ('shopmy.us', 'liketk.it', 'liketoknow.it', 'shopltk.com', 'ltk.app', 'amazon.com/shop/')   # affiliate storefronts, not a brand
SHOP_DOMAIN = re.compile(r'(?:^|[.-])(?:shop|store|winkel)|(?<!work)(?:shop|store|winkel)(?=[.-])|\.(?:shop|store)$')
SOCIAL = ('instagram.com', 'youtube.com', 'youtu.be', 'tiktok.com', 'twitter.com', 'x.com', 'facebook.com', 'linkedin.com', 'wa.me', 'calendly.com',
          'spotify.com', 'apple.com', 'discord.gg', 'whop.com', 'skool.com')
EMAIL_RX = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]{2,}')
SIZE_BANDS = ((1_000, '<1k'), (10_000, '1k-10k'), (100_000, '10k-100k'), (1_000_000, '100k-1M'))
TAXONOMY = {**{t: 'role' for t in ROLE_RX}, **{t: 'niche' for t in NICHE_RX}, **{t: 'signal' for t in SIGNAL_RX},
            **{t: 'signal' for t in ('Shop Link', 'Shopify', 'Ecom', 'Link Hub', 'Email', 'US', 'NL', 'UK', 'Verified', 'Business')}}
# Instagram category labels the account picked itself; only the unambiguous ones
CATEGORY_ROLE = (('Brand', re.compile(r'\(brand\)|^brand$', re.I)),
                 ('Store', re.compile(r'^(?:e-?commerce website|shopping & retail|retail company|clothing store|jewelry store|'
                                      r'cosmetics store|pet store|home decor store|boutique store)$', re.I)))
CATEGORY_NICHE = (('Jewelry', re.compile(r'^jewelry/watches$|jewelry store', re.I)), ('Pets', re.compile(r'^pet (?:supplies|store)', re.I)),
                  ('Supplements', re.compile(r'vitamins/supplements', re.I)), ('Baby', re.compile(r'^baby goods/kids goods$', re.I)),
                  ('Home', re.compile(r'^home decor|^furniture|^kitchen/cooking$', re.I)), ('Apparel', re.compile(r'^clothing', re.I)),
                  ('Beauty', re.compile(r'^(?:beauty, cosmetic & personal care|cosmetics store)$', re.I)))


def _domain(url):
    m = re.match(r'(?:https?://)?(?:www\.)?([^/?#\s]+)(.*)', str(url or '').strip().lower())
    return (m.group(1), m.group(2)) if m and '.' in m.group(1) else ('', '')


def _text(p):
    return ' \n '.join(str(p.get(k) or '') for k in ('bio', 'name', 'category'))


def _seeds(edges, me):
    me = (me or '').lower().lstrip('@')
    seeds = {}
    for e in edges or []:
        s = str(e.get('seed') or '').lower().lstrip('@')
        if s:
            seeds.setdefault(s, set()).add(e.get('direction'))
    return {s: d for s, d in seeds.items() if s != me}, seeds.get(me, set()) if me else set()


# ---------------------------------------------------------------- prefilter
H_BRAND = re.compile(r'^shop|shop$|^store|store(?:nl|us|uk)?$|brand|labs?$|watch(?:es)?co|skin(?!ny)|apparel|roasters?$|tallow|thelabel$|wear$|beauty|supply|supplies|goods|boutique|jewel|candle|coffee|'
                     r'cosmetic|clothing|nutrition|organics?|botanic|essentials|naturals|webshop|winkel|atelier|hq$|^get[a-z]{3}|^try[a-z]{3}|'
                     r'^drink|^wear|^use(?!r|less|d)', re.I)
H_CONNECTOR = re.compile(r'agency|media$|^ads|ads$|ads\b|growth|ecom|dtc|scale[sd]?$|scales|marketing|creative|design|visuals|studio|digital|email|'
                         r'copy|funnel|cro$|ugc|roas', re.I)
H_FOUNDER = re.compile(r'founder|ceo|builds|owner|^team', re.I)
H_BAD = re.compile(r'^fans?$|fanpage|fanacc|memes?|quotes?|edits$|^edits?|vibes|aesthetic|^stan$|^fp$|gaming|gamer|crypto|forex|nft|trading|'
                   r'betting|casino|^priv(?:ate)?$|finsta|spam|backup|^alt$|lover$|clips$|tiktok|repost|viral|onlyfans|babe$|cutie|lyrics|anime|'
                   r'manga|kpop', re.I)
N_BRAND = re.compile(r'(?<!\w)(?:shop|store|co\.?|official|labs|skin ?care|apparel|beauty|supply|goods|clothing|jewel\w*|cosmetics|'
                     r'webshop|winkel|& co|inc\.?|llc|b\.?v\.?|ltd|hq|candles?|coffee|supplements?|nutrition|wear|watches|jewelers|jewellers|'
                     r'roasters?|botanicals|naturals|organics|the label|activewear|swimwear)(?!\w)|[®™]', re.I)
N_SERVICE = re.compile(r'(?<!\w)(?:agency|media|ads|growth|ecom|e-?commerce|dtc|marketing|creatives?|design|cro|email|shopify|klaviyo|'
                       r'performance|meta ads|paid ads|ugc|studio)(?!\w)', re.I)
N_FOUNDER = re.compile(r'(?<!\w)(?:founder|co-?founder|ceo|owner|oprichter|eigenaar)(?!\w)', re.I)
N_NEG = re.compile(r'(?<!\w)(?:coach(?:ing)?|mentor|podcast|learn|course|academy|scientist|dr\.?|ph\.?d\.?|trader|trading|investor|investing|'
                   r'mindset|motivation|speaker|author|real estate|realtor|tattoo|gym|pt|personal trainer|fan ?page|memes?|quotes|edits|crypto|forex|'
                   r'photography|dj|artist|model|actor|actress|athlete|nft)(?!\w)', re.I)


def prefilter(person: dict, seeds: list[str]) -> int:
    handle = str(person.get('handle') or '').lower()
    name = str(person.get('name') or '')
    tokens = [t for t in re.split(r'[._\d]+', handle) if t]
    distinct = {s.lower().lstrip('@') for s in seeds or [] if s}
    score = 32
    n = len(distinct)
    score += (0, 0, 16, 26, 33)[min(n, 4)] + (3 * (n - 4) if n > 4 else 0)
    if any(H_BRAND.search(t) for t in tokens) or re.search(r'(?:^|[._])co$', handle):
        score += 14
    if any(H_CONNECTOR.search(t) for t in tokens):
        score += 10
    if any(H_FOUNDER.search(t) for t in tokens):
        score += 8
    if N_BRAND.search(name):
        score += 14
    elif N_SERVICE.search(name):
        score += 8
    elif re.search(r'[|•·–—] ?\w', name):
        score += 3
    if N_FOUNDER.search(name):
        score += 10
    if N_NEG.search(name):
        score -= 14
    if any(H_BAD.search(t) for t in tokens):
        score -= 30
    if re.search(r'\d{4,}', handle) or re.search(r'[a-z]\d{3}$', handle) or re.fullmatch(r'[a-z]{1,4}[._]?\d+[._]?\w*', handle):
        score -= 15
    if not name.strip():
        score -= 6
    if handle.count('_') + handle.count('.') >= 3 or '__' in handle:
        score -= 6
    if person.get('is_business'):
        score += 10
    if re.search(r'official', handle):
        score += 4
    if person.get('is_verified'):
        score += 2
    if person.get('bio') and person.get('website'):
        score += 4
    f = person.get('followers')
    if isinstance(f, int):
        score += 8 if 300 <= f <= 100_000 else -10 if f < 100 else -14 if f > 1_000_000 else 0
        fo = person.get('following')
        if isinstance(fo, int) and f < 300 and fo > 2_000:
            score -= 10
    if person.get('is_private'):
        score = min(score - 20, 35)
    return max(0, min(100, int(score)))


# ---------------------------------------------------------------- rule tags
def rule_tags(person: dict, edges: list[dict], me: str | None) -> list[tuple[str, str]]:
    text, bio = _text(person), str(person.get('bio') or '')
    dom, path = _domain(person.get('website'))
    category = str(person.get('category') or '').strip()
    tags: list[tuple[str, str]] = []
    add = lambda t, g: (t, g) not in tags and tags.append((t, g))
    promo = bool(PROMO.search(text))
    role_text = PROMO.sub(' ', text)  # "code KAYLA10 at @gymshark" is someone else's brand
    roles = [r for r, rx in ROLE_RX.items() if rx.search(role_text)]
    roles += [r for r, rx in CATEGORY_ROLE if rx.search(category) and r not in roles]
    if promo and 'Creator' not in roles and not ({'Brand', 'Store'} & set(roles)):
        roles.append('Creator')
    if 'Agency' in roles and AGENCY_NOT.search(text) and not ECOM.search(text):
        roles.remove('Agency')
    if 'Brand' in roles and 'Store' in roles:
        roles.remove('Brand')
    if 'Agency' in roles and 'Freelancer' in roles:
        roles.remove('Freelancer' if re.search(r'\bwe\b|\bour\b|\bagency\b', bio, re.I) else 'Agency')
    if 'Coach' in roles and 'Agency' in roles and not re.search(r'coach(?:ing)?\b(?! ?(?:es|ing (?:business|brand)))', bio, re.I):
        roles.remove('Coach')
    if 'SaaS' in roles and ({'Brand', 'Store'} & set(roles)):
        roles.remove('SaaS')
    if not ({'SaaS', 'Brand', 'Store'} & set(roles)) and dom.endswith(('.ai', '.io')) and SIGNAL_RX['Founder'].search(text):
        roles.append('SaaS')
    if 'Personal' in roles and (len(roles) > 1 or SIGNAL_RX['Founder'].search(text)):
        roles.remove('Personal')
    f = person.get('followers')
    if 'Creator' in roles and isinstance(f, int) and f >= 100_000:
        roles.insert(0, roles.pop(roles.index('Creator')))
    for r in roles[:2]:
        add(r, 'role')
    url = dom + path
    social = any(x in dom for x in SOCIAL)
    creator_shop = any(x in url for x in CREATOR_SHOPS)
    business = (bool(set(roles) - {'Personal', 'Creator'}) or person.get('is_business') or (dom and not social and not creator_shop)
                or SIGNAL_RX['Founder'].search(text))
    if business and 'Personal' not in roles:
        for niche, rx in NICHE_RX.items():
            if rx.search(role_text):
                add(niche, 'niche')
        for niche, rx in CATEGORY_NICHE:
            if rx.search(category):
                add(niche, 'niche')
    for sig, rx in SIGNAL_RX.items():
        if rx.search(text):
            add(sig, 'signal')
    if ECOM_TAG.search(text):
        add('Ecom', 'signal')
    hub = any(h in dom for h in LINK_HUBS)
    if any(dom == h or dom.endswith('.' + h) for h in SHOPIFY_HOSTS) or re.search(r'\bshopify\b', bio, re.I):
        add('Shopify', 'signal')
    shop_url = (SHOP_DOMAIN.search(dom) or re.search(r'/(?:products|collections|shop)\b', path) or dom.endswith(('myshopify.com', 'shop.app'))
                or any(dom == m or dom.endswith('.' + m) for m in MARKETPLACES))
    shop_bio = re.search(r'\bshop (?:now|below|here|our|online)\b|\bshop ?(?:👇|⬇|↓)', PROMO.sub(' ', bio), re.I)
    if dom and not social and not creator_shop and not hub and (shop_url or shop_bio):
        add('Shop Link', 'signal')
    if hub:
        add('Link Hub', 'signal')
    if EMAIL_RX.search(bio):
        add('Email', 'signal')
    for tag, rxs in (('US', US_RX), ('NL', NL_RX), ('UK', UK_RX)):
        if any(rx.search(text) for rx in rxs) or (tag == 'NL' and dom.endswith('.nl')) or (tag == 'UK' and dom.endswith('.co.uk')):
            add(tag, 'signal')
    if person.get('is_verified'):
        add('Verified', 'signal')
    if person.get('is_business'):
        add('Business', 'signal')
    if isinstance(f, int):
        add(next((label for cap, label in SIZE_BANDS if f < cap), '1M+'), 'size')
    others, mine = _seeds(edges, me)
    for s in sorted(others):
        add(f'via @{s}', 'source')
    total = len(others) + (1 if mine else 0)
    if total >= 2:
        add(f'in {total} lists', 'source')
    if mine or (me and re.search(r'@' + re.escape(me.lstrip('@')) + r'\b', bio, re.I)):
        add('knows you', 'source')
    if 'followers' in mine:
        add('follows you', 'source')
    if 'following' in mine:
        add('you follow', 'source')
    return tags


# ---------------------------------------------------------------- rule verdict
ROLE_BASE = {'Brand': ('buyer', 66), 'Store': ('buyer', 62), 'Agency': ('connector', 52), 'Freelancer': ('connector', 48),
             'Creative': ('collaborator', 40), 'Supplier': ('supplier', 32), 'SaaS': ('unrelated', 22), 'Coach': ('unrelated', 18),
             'Creator': ('unrelated', 24), 'Personal': ('unrelated', 8)}


def _tier(score, has_bio):
    return 'unread' if not has_bio else 'hot' if score >= 70 else 'warm' if score >= 45 else 'cold'


def _names(tags):
    out = {}
    for t, g in tags or []:
        out.setdefault(g, []).append(t)
    return out


def _source_phrase(g):
    src = g.get('source', [])
    n = next((int(t.split()[1]) for t in src if t.startswith('in ') and t.endswith(' lists')), 0)
    if n >= 2:
        return f'in {n} of your lists'
    if 'follows you' in src:
        return 'follows you'
    if 'you follow' in src:
        return 'you follow them'
    via = [t[4:] for t in src if t.startswith('via @')]
    return f'via {via[0]}' if via else ''


def _seed_count(g):
    return len([t for t in g.get('source', []) if t.startswith('via @')]) + (1 if 'knows you' in g.get('source', []) else 0)


def rule_verdict(person: dict, tags) -> dict:
    g = _names(tags)
    roles, sig, niche = g.get('role', []), set(g.get('signal', [])), g.get('niche', [])
    has_bio = bool(str(person.get('bio') or '').strip())
    text = _text(person)
    role, score = ROLE_BASE.get(roles[0], ('unclear', 34)) if roles else ('unclear', 34)
    first = roles[0] if roles else None
    if first in ('Agency', 'Freelancer') and LOCAL_SERVICE.search(text) and not ECOM.search(text):
        role, score = 'peer', 28
    elif first in ('Agency', 'Freelancer') and ECOM.search(text):
        score += 8
    if first == 'Creative' and re.search(r'\bad (?:creatives?|designer)', text, re.I):
        role, score = 'peer', 30
    elif first == 'Creative' and ECOM.search(text) or first == 'Creative' and re.search(r'\bproduct\b', text, re.I):
        score += 8
    if role == 'unclear' and 'Founder' in sig and (niche or 'Shop Link' in sig or 'Shopify' in sig):
        role, score = 'buyer', 60
    elif role == 'unclear' and ('Shop Link' in sig or 'Shopify' in sig) and niche:
        role, score = 'buyer', 58
    elif role == 'unclear' and ECOM.search(text) and ({'Founder', 'Scaling'} & sig):
        role, score = 'buyer', 54
    elif role == 'unclear' and 'Founder' in sig:
        score = 42
    if SPAM.search(text):
        role, score = 'unrelated', 5
    if role == 'buyer':
        score += 6 * bool('Founder' in sig) + 7 * bool('Shop Link' in sig) + 5 * bool('Shopify' in sig) + 4 * bool(niche)
    if role in ('buyer', 'connector'):
        score += 4 * bool('US' in sig) + 3 * bool('NL' in sig) + 4 * bool('Scaling' in sig) + 2 * bool('Email' in sig)
    n = _seed_count(g)
    score += min(12, 4 * max(0, n - 1)) + 3 * ('knows you' in g.get('source', []))
    size = (g.get('size') or [None])[0]
    score += {'1M+': -22, '100k-1M': -6, '<1k': -2}.get(size, 0) if role in ('buyer', 'connector') else 0
    if role in ('unrelated', 'peer'):
        score = min(score, 35)
    score = max(0, min(100, score))
    return {'score': score, 'role': role, 'reason': _reason(role, first, g, has_bio, text), 'tier': _tier(score, has_bio)}


NOUN = {'Brand': 'brand', 'Store': 'online store', 'Agency': 'agency', 'Freelancer': 'freelancer', 'Creative': 'creative',
        'Supplier': 'supplier', 'SaaS': 'software company', 'Coach': 'coach or course seller', 'Creator': 'content creator', 'Personal': 'personal account'}


def _reason(role, first, g, has_bio, text=''):
    src = _source_phrase(g)
    if role == 'unrelated' and not first and has_bio:
        return 'Looks like spam or a signals account' + (', ' + src if src else '') + '.'
    if not has_bio:
        return ('No bio read yet, ' + src + '.') if src else 'No bio read yet.'
    sig, niche = set(g.get('signal', [])), [n.lower() for n in g.get('niche', [])][:2]
    noun = NOUN.get(first) or ('brand' if role == 'buyer' else None)
    if noun:
        what = (' and '.join(niche) + ' ' if niche and role in ('buyer', 'connector', 'supplier') else '') + noun
        what = ('an ' if what[0] in 'aeiou' else 'a ') + what
        head = ('Founder of ' if 'Founder' in sig and first not in ('Freelancer', 'Creator', 'Personal') else '') + what
        if role == 'peer':
            head = 'Makes ad creatives too, likely a peer' if first == 'Creative' else head + ' serving local or non-product businesses'
        elif role in ('connector', 'collaborator') and ECOM.search(text):
            m = ECOM.search(text).group(0).lower()
            head += ' for brands' if m.endswith('brands') and not m.startswith('product') else ' for e-commerce brands'
            sig = sig - {'Shopify'}
    elif 'Founder' in sig:
        head = 'Founder, but the business is not clear from the bio'
    else:
        head = 'No clear business in the bio'
    shopify = 'a Shopify store' if role == 'buyer' else 'a Shopify focus'
    extras = [x for x, on in ((shopify, 'Shopify' in sig), ('a shop link', 'Shop Link' in sig and 'Shopify' not in sig and role == 'buyer'),
                              ('7-8 figure talk', 'Scaling' in sig and role in ('buyer', 'connector'))) if on]
    s = head + (' with ' + ' and '.join(extras[:2]) if extras else '')
    geo = [x for x in ('US', 'NL', 'UK') if x in sig]
    s += f' ({geo[0]})' if geo else ''
    s += ', ' + src if src else ''
    return s[0].upper() + s[1:] + '.'


# ---------------------------------------------------------------- LLM verdict
BRIEF = """Michael runs Fortunate: he designs static ad creatives and product visuals for physical-product brands (DTC/e-commerce),
mainly US-focused brands and operators; Dutch operators selling to the US also fit. Minimum engagement about EUR 2,000.
The brand supplies strategy and copy; he makes the visuals. He does not do media buying.
Categories:
- buyer: owns, founded or runs a physical-product brand or online store (or is that brand's account). Best leads.
- connector: agency, consultant or freelancer whose clients are product brands / e-commerce (ads, email, CRO, Shopify dev, growth). Can refer work.
- collaborator: creative specialist with complementary work (photography, UGC, 3D/CGI, video) for brands.
- peer: does similar service work but for non-product clients (local businesses, SaaS, real estate), or makes ad creatives himself.
- supplier: fulfilment, packaging, manufacturing, software/tools sold to brands.
- unrelated: personal account, student, fan, influencer/creator without a product business, coach/course seller, investor, SaaS/tech founder, local tradesman, bot.
- unclear: real signs of a business but not enough to say what it is."""

READING_INSTAGRAM = """How to read Instagram profiles (be as sharp as a person scrolling, not a keyword matcher):
- Bios are vague on purpose. "CEO @x", "Founder @x", "building @x" means the business is @x: judge @x, and ask to look at it.
- The Instagram category label is chosen by the user. "Entrepreneur", "Digital creator", "Public figure", "Personal blog" say almost nothing.
- Link in bio: a shop domain with products is the best evidence there is. Link hubs (linktr.ee etc.) need the real destination.
- Follower counts: 300-100k is the sweet spot for a brand that hires a freelancer. Under 300 can be an early brand, still worth it if real.
  Over 1M is almost always a celebrity, big creator or big brand with an in-house team: very unlikely to hire him. Following far more than followers = follow-for-follow or spam.
- Many followers of a business account are fans, friends and students, not buyers. Default to unrelated unless there are concrete business signs.
- Many good leads are lowkey: e-commerce owners and ads/growth people often keep a personal-looking profile and never say it in the bio.
  Quiet signs: sitting in several e-commerce/ads operators' networks, a brand @mention, "ops", "scaling", "8fig",
  a Shopify link, a second account for the business. Weigh these; do not dismiss a sparse profile that has them.
- Michael's own account is @fortun8te. Mentions of or connections to @fortun8te mean they know him.
- Private or near-empty profiles with no negative signs: category unclear, fit around 40. Michael will judge those himself; do not call them unrelated just for being sparse.
- Never infer gender, age, ethnicity, nationality or wealth; they are irrelevant to fit. Never invent facts: only use what the evidence shows."""

SCHEMA = """Reply with one compact JSON object only, no prose:
{"role": "buyer|connector|collaborator|peer|supplier|unrelated|unclear", "fit": 0-100,
 "reason": "one plain sentence under 25 words citing concrete evidence from the profile",
 "extra_tags": ["product niches from this list that the evidence clearly shows: %s"]}"""


REPLY_MAX = 1_000_000
LLM_BUDGET = 90   # seconds for one verdict across all models; the socket timeout alone does not bound a slow-drip reply


def _call(model, messages, timeout):
    body = {'model': model, 'messages': messages, 'max_tokens': 400, 'temperature': 0.1, 'reasoning': {'effort': 'low', 'exclude': True}}
    req = urllib.request.Request(PROXY, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
            data = json.loads(r.read(REPLY_MAX))
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ValueError(f'{model}: HTTP {exc.code}') from None
    if not isinstance(data, dict):
        raise ValueError(f'{model}: reply is not an object')
    if data.get('error'):
        raise ValueError(str(data['error'])[:160])
    returned = str(data.get('model') or model)
    if returned.split(':')[0] != model.split(':')[0]:
        raise ValueError(f'proxy substituted {returned}')
    try:
        content = data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        content = None
    return content if isinstance(content, str) else '', model


def parse_json(text):
    text = re.sub(r'<think>.*?</think>', '', str(text or ''), flags=re.S)
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.I).strip()
    dec = json.JSONDecoder()
    for m in re.finditer(r'\{', text):
        try:
            v, _ = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(v, dict):
            return v
    return None


def _packet(person, tags, edges):
    others, mine = _seeds(edges, 'fortun8te')
    rows = [f"@{person.get('handle')}" + (f" · name: {person['name']}" if person.get('name') else '')]
    for label, key in (('Category label', 'category'), ('Bio', 'bio'), ('Link in bio', 'website')):
        if person.get(key):
            rows.append(f'{label}: {person[key]}')
    counts = [f'{person[k]:,} {k}' for k in ('followers', 'following', 'posts') if isinstance(person.get(k), int)]
    if counts:
        rows.append('Counts: ' + ', '.join(counts))
    flags = [n for n, k in (('business account', 'is_business'), ('verified', 'is_verified'), ('private', 'is_private')) if person.get(k)]
    if flags:
        rows.append('Account: ' + ', '.join(flags))
    if others:
        rows.append("Appears in the follower/following lists of these e-commerce/ads operators: " + ', '.join('@' + s for s in sorted(others)[:10]))
    if mine:
        rows.append('Connected to Michael (@fortun8te): ' + ', '.join(sorted({'followers': 'follows him', 'following': 'he follows them'}.get(d, d) for d in mine)))
    rt = [t for t, gr in tags or [] if gr in ('role', 'niche', 'signal')]
    if rt:
        rows.append('Keyword rules matched (hint, may be wrong): ' + ', '.join(rt))
    return '\n'.join(rows)


ROLE_TAG = {'buyer': 'Brand', 'connector': 'Agency', 'collaborator': 'Creative', 'supplier': 'Supplier'}


ROLE_CAP = {'buyer': 100, 'connector': 80, 'collaborator': 60, 'unclear': 60, 'supplier': 50, 'peer': 40, 'unrelated': 30}


def llm_verdict(person: dict, tags, edges, timeout: float = 45, models=MODELS, budget: float = LLM_BUDGET) -> dict | None:
    """None when no model gives a usable answer in time: the caller keeps the rule verdict and retries later."""
    allowed = [t for t, g in TAXONOMY.items() if g == 'niche']
    system = BRIEF + '\n\n' + READING_INSTAGRAM + '\n\n' + SCHEMA % ', '.join(allowed)
    msgs = [{'role': 'system', 'content': system}, {'role': 'user', 'content': _packet(person, tags, edges)}]
    deadline = time.monotonic() + budget
    for model in models:
        left = deadline - time.monotonic()
        if left <= 1:
            return None
        try:
            text, used = _call(model, msgs, min(timeout, left))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
            continue
        v = parse_json(text)
        if not v or v.get('role') not in ROLES or not isinstance(v.get('fit'), (int, float)):
            continue
        fit = int(max(0, min(100, v['fit'])))
        g = _names(tags)
        n = _seed_count(g)
        score = fit + min(10, 4 * max(0, n - 1)) + 3 * ('knows you' in g.get('source', []))
        score = max(0, min(ROLE_CAP[v['role']], score))
        reason = re.sub(r'\s+', ' ', str(v.get('reason') or '')).strip()[:220] or rule_verdict(person, tags)['reason']
        extra = [t for t in (v.get('extra_tags') or []) if isinstance(t, str) and t in allowed]
        have = {t for t, _ in tags or []}
        rt = ROLE_TAG.get(v['role'])
        if rt and not (have & {'Brand', 'Store'} if rt == 'Brand' else have & {'Agency', 'Freelancer'} if rt == 'Agency' else rt in have):
            extra.append(rt)
        new = [(t, TAXONOMY[t]) for t in dict.fromkeys(extra) if t not in have]
        return {'score': score, 'role': v['role'], 'reason': reason, 'tier': _tier(score, bool(str(person.get('bio') or '').strip())),
                'model': used, 'fit': fit, 'tags': new}
    return None


def input_hash(person: dict, edges) -> str:
    keys = ('handle', 'name', 'bio', 'website', 'category', 'followers', 'following', 'posts', 'is_private', 'is_verified', 'is_business')
    payload = [PROMPT_VERSION, [person.get(k) for k in keys], sorted({(str(e.get('seed') or '').lower(), str(e.get('direction'))) for e in edges or []})]
    return hashlib.sha256(json.dumps(payload, default=str, ensure_ascii=False).encode()).hexdigest()[:16]
