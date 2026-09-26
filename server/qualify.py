"""Lead qualifier for Fortunate Leads: prefilter (no bio), rule tags, rule verdict, one LLM verdict.

Pure functions except llm_verdict, which calls the local OpenRouter proxy (free models only).
Precision over recall everywhere: a wrong tag is worse than a missing one.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
import time
import unicodedata

import llm

TAG_GROUPS = ('role', 'niche', 'signal', 'size', 'source', 'ai')   # 'ai': only from a model verdict (never rules)
PROXY = llm.PROXY
MODELS = llm.MODELS
PROMPT_VERSION = 'q6'   # rubric + evidence + few-shot; the few-shot set is versioned separately (prompt_version)
TAGS_VERSION = 't6-reach'   # bump when rule tags change: the server re-derives everyone's auto tags once (LLM verdicts are kept)
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
                 r' (?:brand|company|label|co)(?! owners?| founders?| operators?| marketers?| deals?)'),
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
            **{t: 'signal' for t in ('Shop Link', 'Shopify', 'Ecom', 'Link Hub', 'Email', 'US', 'NL', 'UK', 'Verified', 'Business',
                                      'Too big', 'Other market')}}
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


# Ranking: the network (who follows whom among Michael's seeds and clients) carries the larger share of every score; the
# profile read (handle/name before the bio, then rules or the LLM on the bio) adjusts it.
NET_WEIGHT = 0.6


def _clamp(v):
    return max(0, min(100, int(round(v))))


REACH_FOLLOWERS = 300_000   # above this an account is a public figure: lists following them says nothing about access
OTHER_MARKET_RX = re.compile(r'🇮🇳|🇵🇰|🇧🇩|🇳🇬|🇮🇩|🇵🇭|🇪🇬|🇰🇪|\b(?:india|indian|mumbai|delhi|bangalore|bengaluru|hyderabad|chennai|'
                             r'kolkata|pune|ahmedabad|jaipur|pakistan|karachi|lahore|islamabad|bangladesh|dhaka|nigeria|lagos|abuja|'
                             r'indonesia|jakarta|philippines|manila|egypt|cairo|kenya|nairobi|ghana|accra)\b', re.I)


def too_big(followers, net=None) -> bool:
    """A public figure with no direct line to Michael: no entree, whatever the lists say."""
    net = net or {}
    return (isinstance(followers, int) and followers >= REACH_FOLLOWERS
            and net.get('me') not in ('mutual', 'followed') and not net.get('client_seeds'))


def other_market(person) -> bool:
    """Clearly based outside the markets Fortunate sells to, with nothing saying they sell into them."""
    text = _text(person) + ' ' + str(person.get('website') or '')
    return bool(OTHER_MARKET_RX.search(text)) and not any(rx.search(text) for rx in US_RX + NL_RX + UK_RX)


def network_strength(net) -> int:
    """0-100 from the network (net = server.network_context entry): lists count, seeds that follow them, how well their
    seeds' people converted (seed yield), links to Michael's good/client accounts, and a direct link to Michael."""
    net = net or {}
    lists = int(net.get('lists') or 0)
    s = 30 + (0, 0, 25, 38, 46)[min(lists, 4)] + (3 * (lists - 4) if lists > 4 else 0)
    by = sum(1 for _, d in net.get('seeds', []) if d == 'following')   # a seed chose to follow them: stronger than the reverse
    s += min(10, 5 * by)
    y = net.get('seed_yield')
    if y is not None and net.get('seed_marked'):
        s += max(-12, min(20, round((y - 0.25) * 60)))
    s += {'mutual': 16, 'follows': 10, 'followed': 8}.get(net.get('me'), 0)
    s += min(18, 6 * int(net.get('client_seeds') or 0))
    if too_big(net.get('followers'), net):
        s = min(s, 20)
    return _clamp(s)


def net_from_tags(tags) -> dict:
    """The network as far as the source tags tell (lists count, link to Michael) when no network context is at hand."""
    src = _names(tags).get('source', [])
    me = 'mutual' if {'follows you', 'you follow'} <= set(src) else 'follows' if 'follows you' in src else \
        'followed' if 'you follow' in src else None
    return {'lists': max(_seed_count({'source': src}), 1 if src else 0), 'me': me}


def blend(content, net) -> int:
    return _clamp(NET_WEIGHT * network_strength(net) + (1 - NET_WEIGHT) * content)


def prefilter(person: dict, seeds: list[str], net=None, laya_fit=None) -> int:
    """0-100 before any bio: the network blended with handle/name signals; Laya (optional) is one soft weighted signal."""
    if net is None:
        net = {'lists': len({s.lower().lstrip('@') for s in seeds or [] if s})}
    base = blend(_profile_signals(person), net)
    if person.get('is_private'):
        base = min(base, 35)
    if too_big(person.get('followers'), net) or other_market(person):
        base = min(base, 15)   # no bio read or model call for people there is no way in with
    if laya_fit is not None:
        base = round(0.75 * base + 0.25 * laya_fit)
    return _clamp(base)


def _profile_signals(person: dict) -> int:
    """0-100 from the handle, display name and counts only (what a list page shows)."""
    handle = str(person.get('handle') or '').lower()
    name = str(person.get('name') or '')
    tokens = [t for t in re.split(r'[._\d]+', handle) if t]
    score = 32
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
    return _clamp(score)


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
    if isinstance(f, int) and f >= REACH_FOLLOWERS and not {'followers', 'following'} & set(_seeds(edges, me)[1]):
        add('Too big', 'signal')
    if other_market(person):
        add('Other market', 'signal')
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
    if mine:
        add('Instagram link', 'source')
    if me and re.search(r'@' + re.escape(me.lstrip('@')) + r'(?![a-zA-Z0-9_.])', bio, re.I):
        add('mentions you', 'source')
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
    return len([t for t in g.get('source', []) if t.startswith('via @')]) + (1 if 'Instagram link' in g.get('source', []) else 0)


# Explicit business/service activity is required; interests and audience size are not activity.
PRODUCT_TERMS = r'(?:skincare|skin care|clothing|apparel|beauty|coffee|candles?|pets?|jewell?ery|supplements?|huidverzorging|kleding|kaarsen|sieraden|hautpflege|kleidung|kerzen|schmuck|cosmétique[s]?|vêtements|bougies|bijoux|ropa|velas|joyas|护肤|服装|蜡烛|مستحضرات التجميل|ملابس)'
MULTI_BRAND = re.compile(r'(?:marque|marca|merk|marke)\s+(?:de |de\s+soins\s+de\s+la\s+peau|van |für |di )?' + PRODUCT_TERMS +
                         r'|' + PRODUCT_TERMS + r'[- ]?(?:marke|merk|品牌|brand)|علامة تجارية.{0,24}' + PRODUCT_TERMS, re.I)
MULTI_FOUNDER = re.compile(r'(?<!\w)(?:oprichter|eigenaar|gründer(?:in)?|inhaber(?:in)?|fondateur|fondatrice|fundador(?:a)?|dueño|dueña|مؤسس(?:ة)?)(?!\w)|创始人|创办人', re.I)
INTEREST = re.compile(r'\b(?:interested in|learning|aspiring|enthusiast|fan of|love|lover|interesse in|liefhebber|passionné|passionnée|intéressé|intéressée|interessiert|fan von|me encanta|aficionado)\b|爱好|مهتم', re.I)


def _connector_support(text):
    service = ROLE_RX['Agency'].search(text) or ROLE_RX['Freelancer'].search(text)
    return bool(service and ECOM.search(text) and not re.search(
        r'\b(?:interested in|learning|aspiring|enthusiast|fan of)\b', text, re.I))


def _buyer_support(person):
    text = PROMO.sub(' ', unicodedata.normalize('NFC', _text(person)))
    text = ' '.join(part for part in re.split(r'[\n|.!?。]', text) if not INTEREST.search(part))
    niches = any(rx.search(text) for rx in NICHE_RX.values())
    explicit_brand = re.search(r'\b(?:physical.product|consumer|dtc|d2c|e-?com(?:merce)?|skincare|clothing|apparel|beauty|coffee|candle|pet|jewel(?:le)?ry|supplement)\s+(?:brand|company|label)\b(?!\s+(?:deals?|owners?|founders?|operators?|marketers?))', text, re.I)
    store = ROLE_RX['Store'].search(text)
    commerce = ROLE_RX['Brand'].search(text)
    # A shop link helps only alongside product evidence, not an affiliate storefront.
    shop = 'Shop Link' in {t for t, g in rule_tags(person, [], None) if g == 'signal'}
    selling = bool(store or (niches and (commerce or shop)))
    service = any(ROLE_RX[r].search(text) for r in ('Agency', 'Freelancer', 'Creative', 'Creator', 'Coach', 'Supplier'))
    ownership = re.search(r'(?:founder|owner|ceo)\s+(?:of |at )?(?:a |our |the )?(?:\w+\s+){0,3}' + PRODUCT_TERMS + r'\s+brand', text, re.I)
    direct_sales = store or shop or re.search(r'\b(?:shop (?:now|our|here|online)|handmade|handcrafted|ships?|shipping|our products)\b', text, re.I)
    if service and not direct_sales and not ownership and not MULTI_FOUNDER.search(text):
        return False
    return bool(explicit_brand or MULTI_BRAND.search(text) or selling)


def rule_verdict(person: dict, tags, net=None) -> dict:
    """Rules on the bio give the profile read; the network (net, else what the source tags say) the larger share."""
    g = _names(tags)
    roles, sig, niche = g.get('role', []), set(g.get('signal', [])), g.get('niche', [])
    has_bio = bool(str(person.get('bio') or '').strip())
    text = _text(person)
    role, score = ROLE_BASE.get(roles[0], ('unclear', 34)) if roles else ('unclear', 34)
    first = roles[0] if roles else None
    if first in ('Agency', 'Freelancer') and LOCAL_SERVICE.search(text) and not ECOM.search(text):
        role, score = 'peer', 28
    elif first in ('Agency', 'Freelancer') and _connector_support(text):
        score += 8
    elif first in ('Agency', 'Freelancer'):
        role, score = 'unclear', 34
    if first == 'Creative' and re.search(r'\bad (?:creatives?|designer)', text, re.I):
        role, score = 'peer', 30
    elif first == 'Creative' and ECOM.search(text) or first == 'Creative' and re.search(r'\bproduct\b', text, re.I):
        score += 8
    if role == 'buyer' and not _buyer_support(person):
        role, score = 'unclear', 34
    if role == 'unclear' and 'Founder' in sig and _buyer_support(person):
        role, score = 'buyer', 60
    elif role == 'unclear' and _buyer_support(person):
        role, score = 'buyer', 58
    elif role == 'unclear' and 'Founder' in sig:
        score = 42
    if SPAM.search(text):
        role, score = 'unrelated', 5
    if role == 'buyer':
        score += 6 * bool('Founder' in sig) + 7 * bool('Shop Link' in sig) + 5 * bool('Shopify' in sig) + 4 * bool(niche)
    if role in ('buyer', 'connector'):
        score += 4 * bool('US' in sig) + 3 * bool('NL' in sig) + 4 * bool('Scaling' in sig) + 2 * bool('Email' in sig)
    size = (g.get('size') or [None])[0]
    score += {'1M+': -22, '100k-1M': -6, '<1k': -2}.get(size, 0) if role in ('buyer', 'connector') else 0
    if role in ('unrelated', 'peer'):
        score = min(score, 35)
    if 'Too big' in sig or 'Other market' in sig:
        score = min(score, 20)
    profile_signal = _clamp(score)
    score = blend(profile_signal, net if net is not None else net_from_tags(tags))
    return {'score': score, 'content_fit': profile_signal if has_bio else None, 'role': role,
            'reason': _reason(role, first, g, has_bio, text), 'tier': _tier(score, has_bio)}


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
    if role == 'unclear' and first in ('Brand', 'Store'):
        noun = None
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
- Bios may be in Dutch, German, French, Spanish or another language. Judge their meaning, write the reason in English,
  and keep evidence quotes exactly as written in the bio or name. Do not turn a translation into an exact quote.
- Bios are vague on purpose. "CEO @x", "Founder @x", "building @x" means the business is @x: judge @x, and ask to look at it.
- The Instagram category label is chosen by the user. "Entrepreneur", "Digital creator", "Public figure", "Personal blog" say almost nothing.
- Link in bio: a shop domain with products is the best evidence there is. Link hubs (linktr.ee etc.) need the real destination.
- Follower counts: 300-100k is the sweet spot for a brand that hires a freelancer. Under 300 can be an early brand, still worth it if real.
  Over 1M is almost always a celebrity, big creator or big brand with an in-house team: very unlikely to hire him. Following far more than followers = follow-for-follow or spam.
- Many followers of a business account are fans, friends and students, not buyers. Without concrete business signs, lean unclear
  rather than guessing a role.
- Many good leads are lowkey: e-commerce owners and ads/growth people often keep a personal-looking profile and never say it in the bio.
  Quiet signs: sitting in several e-commerce/ads operators' networks, a brand @mention, "ops", "scaling", "8fig",
  a Shopify link, a second account for the business. Weigh these; do not dismiss a sparse profile that has them.
- Michael's own account is @fortun8te. A mention or follow is evidence of that action only; it does not prove they know him personally.
- Private or near-empty profiles with no negative signs: category unclear, fit around 40. Michael will judge those himself; do not call them unrelated just for being sparse.
- Never infer gender, age, ethnicity, nationality or wealth; they are irrelevant to fit. Never invent facts: only use what the evidence shows."""

SCHEMA = """Reply with one compact JSON object only, no prose:
{"role": "buyer|connector|collaborator|peer|supplier|unrelated|unclear", "fit": 0-100,
 "reason": "one plain sentence under 25 words citing concrete evidence from the profile",
 "extra_tags": ["product niches from this list that the evidence clearly shows: %s"]}"""


LLM_BUDGET = 90   # seconds for one verdict across all models; the socket timeout alone does not bound a slow-drip reply
LLM_BATCH = 4     # profiles per model call (one JSON reply with a result per id); failures fall back per person
FEWSHOT_MAX = 8   # examples per label from Michael's own marks

RUBRIC = """Scoring rubric (fit 0-100) for Fortunate's ideal client:
- 80-100: founder, owner or decision-maker of a DTC physical-product brand (or that brand's own account), US-based or US-selling,
  or Dutch selling to the US, big enough to pay about EUR 2,000+ (real shop, products, customers, team or growth talk).
- 60-79: likely a product brand / its decision-maker but size, market or role is not fully clear.
- 40-59: connector (e-com agency/freelancer who can refer) or a sparse profile with real business signs.
- 20-39: creators/influencers, agencies for non-product clients, coaches and course sellers, suppliers and tools: usually not a fit,
  but they can know buyers. Score higher when the evidence shows they also run a product brand.
- 0-19: clearly personal, fan, spam or bot accounts.
- Reachability matters: public figures and mega-creators (300k+ followers, household names like Alex Hormozi) score 0-15
  unless the network lines show a direct link to Michael; being followed by many operators does not make them reachable.
- Markets: US, NL, UK, EU, Canada, Australia. Businesses clearly based elsewhere that do not sell into these score 0-20.
Your fit judges the profile itself. The network lines (lists, who follows them, links to Michael and his clients) are added to
the final ranking separately and weigh more than the bio, so do not push a fit to 0 just because the bio is sparse."""


def _call(model, messages, timeout):
    """One model through the provider layer. -> (content, model used). Raises ValueError / OSError when unusable."""
    try:
        return _providers().chat(messages, models=(model,), timeout=timeout, budget=timeout + 1)
    except llm.Unavailable as e:
        raise ValueError(str(e)) from None


def _providers():
    """Use the exact shared pool so every caller observes the same quotas."""
    return llm.get()


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


def network_lines(net):
    """Plain-language network signals for the LLM packet (net = server.network_context entry)."""
    if not net:
        return []
    rows = []
    by = [s for s, d in net.get('seeds', []) if d == 'following']
    of = [s for s, d in net.get('seeds', []) if d == 'followers']
    if by:
        rows.append('Followed BY these operators (they chose to follow this account): ' + ', '.join('@' + s for s in sorted(by)[:10]))
    if of:
        rows.append('Follows these operators: ' + ', '.join('@' + s for s in sorted(of)[:10]))
    if net.get('lists', 0) >= 2:
        rows.append(f"In {net['lists']} of Michael's lists")
    y = net.get('seed_yield')
    if y is not None and net.get('seed_marked'):
        rows.append(f"Best source list: {round(y * 100)}% of the people Michael marked from it were good/client")
    if net.get('client_seeds'):
        rows.append(f"Linked to {net['client_seeds']} account(s) Michael marked good/client")
    me = {'mutual': 'follows Michael and he follows them', 'follows': 'follows Michael', 'followed': 'Michael follows them'}.get(net.get('me'))
    if me:
        rows.append('Connected to Michael (@fortun8te): ' + me)
    return rows


OWNER_STATUS = {'interested': 'Interested (worth contacting)', 'contacted': 'Contacted', 'talking': 'Talking (in conversation)',
                'client': 'Client', 'no': 'Not a fit'}


def owner_lines(person):
    """Michael's own judgement on this person (status, note, his hand-set tags): labelled so the model weighs it above guesses."""
    out = []
    if person.get('status') in OWNER_STATUS:
        out.append(f"OWNER'S OWN JUDGEMENT - status Michael set himself: {OWNER_STATUS[person['status']]}")
    note = re.sub(r'\s+', ' ', str(person.get('note') or '')).strip()
    if note:
        out.append(f"OWNER'S OWN NOTE (Michael wrote this; trust it over the bio): {note[:500]}")
    if person.get('manual_tags'):
        out.append("Tags Michael set by hand: " + ', '.join(person['manual_tags'][:12]))
    if out:
        out.append("(Lines marked OWNER come from Michael himself: follow them. Not a fit means fit under 15; Interested, "
                   "Talking or Client means he wants them, so keep fit high unless his note says otherwise.)")
    return out


def _packet(person, tags, edges, net=None):
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
    if net:
        rows += network_lines(net)
    else:
        if others:
            rows.append("Appears in the follower/following lists of these e-commerce/ads operators: " + ', '.join('@' + s for s in sorted(others)[:10]))
        if mine:
            rows.append('Connected to Michael (@fortun8te): ' + ', '.join(sorted({'followers': 'follows him', 'following': 'he follows them'}.get(d, d) for d in mine)))
    rows += owner_lines(person)
    rows += person.get('web_lines') or []   # set by the server's web research step
    rt = [t for t, gr in tags or [] if gr in ('role', 'niche', 'signal')]
    if rt:
        rows.append('Keyword rules matched (hint, may be wrong): ' + ', '.join(rt))
    return '\n'.join(rows)


def fewshot_text(examples):
    """examples = [{'handle','name','bio','label': 'good'|'no'}] from Michael's own marks."""
    good = [e for e in examples or [] if e.get('label') == 'good'][:FEWSHOT_MAX]
    bad = [e for e in examples or [] if e.get('label') == 'no'][:FEWSHOT_MAX]
    if not good and not bad:
        return ''
    line = lambda e: f"- @{e.get('handle')}" + (f" ({e['name']})" if e.get('name') else '') + ': ' + re.sub(r'\s+', ' ', str(e.get('bio') or ''))[:160]  # noqa: E731
    out = ["Michael's own past judgements (learn his taste from these):"]
    if good:
        out += ['Marked Interested / Talking / Client (he wants these):'] + [line(e) for e in good]
    if bad:
        out += ['Marked NO (not a fit):'] + [line(e) for e in bad]
    return '\n'.join(out)


def fewshot_version(examples):
    # The actual example text changes when a marked person's bio or name changes.
    key = sorted((str(e.get('handle')), str(e.get('name') or ''), str(e.get('bio') or ''), str(e.get('label') or ''))
                 for e in examples or [])
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:8] if key else '0'


def prompt_version(examples=None):
    return f'{PROMPT_VERSION}:{fewshot_version(examples)}'


SCHEMA_ONE = """Reply with JSON only, no prose. For each profile:
{"id": <the id>, "handle": "the exact profile handle", "role": "buyer|connector|collaborator|peer|supplier|unrelated|unclear", "niche": "one of: %s, or null",
 "brand_handle": "@handle of the brand they run, or null", "decision_maker": true|false, "fit": 0-100,
 "evidence": ["up to 3 short exact quotes from the bio, name or WEB RESEARCH lines that support the verdict"],
 "reason": "one plain sentence under 25 words citing concrete evidence", "extra_tags": ["product niches from the list above the evidence clearly shows"],
 "stage": "pre-launch|early|growing|established|unknown (brands only)", "runs_ads": true|false|null, "us_market": true|false|null}
Use true only when the profile explicitly states the claim. US shipping supports us_market; a city alone does not. Running paid ads supports runs_ads; publicity does not. Otherwise null."""


def _system(examples, n):
    allowed = [t for t, g in TAXONOMY.items() if g == 'niche']
    fmt = SCHEMA_ONE % ', '.join(allowed)
    fmt += ('\nOne profile: reply with that one object.' if n == 1 else
            '\nSeveral profiles: reply {"results": [one object per profile, same ids and exact handles]}.')
    parts = [BRIEF, READING_INSTAGRAM, RUBRIC, fewshot_text(examples), fmt]
    return '\n\n'.join(p for p in parts if p)


ROLE_TAG = {'buyer': 'Brand', 'connector': 'Agency', 'collaborator': 'Creative', 'supplier': 'Supplier'}


ROLE_CAP = {'buyer': 100, 'connector': 80, 'collaborator': 65, 'unclear': 65, 'supplier': 55, 'peer': 50, 'unrelated': 40}   # on the fit


def _evidence(v, person):
    # The prompt asks for exact quotes from bio/name. Do not launder an inferred
    # category, URL or handle into a quote shown as direct profile evidence.
    fields = [unicodedata.normalize('NFC', str(person.get(k) or '')).casefold() for k in ('bio', 'name')]
    quotes = v.get('evidence')
    if not isinstance(quotes, list):
        return []
    out = []
    for q in quotes:
        if isinstance(q, str) and 2 < len(q.strip()) <= 160 and any(
                unicodedata.normalize('NFC', q.strip()).casefold() in field for field in fields):
            out.append(q.strip())
    return out[:3]


AI_STAGE = {'pre-launch': 'AI: Pre-launch', 'early': 'AI: Early stage', 'growing': 'AI: Growing', 'established': 'AI: Established'}


def ai_tags(v, fit):
    """Smarter tags that only a model verdict can give (grp 'ai', names start with 'AI: '). Rule verdicts never add these."""
    out = []
    role = v.get('role')
    if role == 'buyer' and isinstance(v.get('niche'), str) and TAXONOMY.get(v['niche']) == 'niche':
        out.append('AI: ' + v['niche'])
    if role == 'buyer' and isinstance(v.get('stage'), str) and AI_STAGE.get(v.get('stage')):
        out.append(AI_STAGE[v['stage']])
    if v.get('decision_maker') is True and role in ('buyer', 'connector'):
        out.append('AI: Decision maker')
    if v.get('runs_ads') is True:
        out.append('AI: Runs ads')
    if v.get('us_market') is True:
        out.append('AI: US market')
    if role == 'buyer' and fit >= 75:
        out.append('AI: Top fit')
    return out


def _verdict(v, person, tags, used, version, net=None):
    # Web research (search results, their website) counts as evidence alongside the bio and name.
    web = str(person.get('web_text') or '').strip()
    if web:
        person = dict(person, bio=f"{person.get('bio') or ''}\n{web}")
    if not isinstance(v, dict) or v.get('role') not in ROLES or not isinstance(v.get('fit'), (int, float)) or isinstance(v.get('fit'), bool):
        return None
    if isinstance(v['fit'], float) and not math.isfinite(v['fit']):
        return None
    for key in ('decision_maker', 'runs_ads', 'us_market'):
        if v.get(key) is not None and type(v[key]) is not bool:
            return None
    for key in ('evidence', 'extra_tags'):
        if v.get(key) is not None and (not isinstance(v[key], list) or any(not isinstance(x, str) for x in v[key])):
            return None
    for key in ('stage', 'niche', 'brand_handle', 'reason'):
        if v.get(key) is not None and not isinstance(v[key], str):
            return None
    evidence = _evidence(v, person)
    if not evidence:
        return None
    source = unicodedata.normalize('NFC', ' '.join(str(person.get(k) or '') for k in ('bio', 'name')))
    if not str(person.get('bio') or '').strip() and not _buyer_support({'name': person.get('name')}):
        return None
    v = dict(v)
    if ((v['role'] == 'buyer' and not _buyer_support(person)) or
            (v['role'] == 'connector' and not _connector_support(source))):
        fallback = rule_verdict(person, rule_tags(person, [], None))
        v.update(role=fallback['role'], fit=fallback['content_fit'] or 34, decision_maker=False,
                 runs_ads=False, us_market=False, stage=None, niche=None, extra_tags=[], brand_handle=None)
    # Claims shown as badges must have their own support, even when the role is valid.
    decision = SIGNAL_RX['Founder'].search(source) or MULTI_FOUNDER.search(source) or re.search(r'\b(?:head|director) of (?:purchasing|marketing|e-?commerce)\b', source, re.I)
    v['decision_maker'] = v.get('decision_maker') is True and bool(decision)
    v['runs_ads'] = v.get('runs_ads') is True and bool(re.search(r'\b(?:running|we run|our|spend on)\s+(?:paid |meta |facebook )?ads\b|\bad spend\b', source, re.I))
    v['us_market'] = v.get('us_market') is True and bool(re.search(r'\b(?:ships?|shipping|selling|sells?)\s+(?:to |in |across |within )?(?:the )?(?:us|usa|united states)\b|\bUS market\b', source, re.I))
    stages = {'pre-launch': r'pre[- ]launch|launching soon', 'early': r'just launched|newly launched',
              'growing': r'we are growing|growing our|scaling our', 'established': r'established'}
    if v.get('stage') not in stages or not re.search(stages[v['stage']], source, re.I):
        v['stage'] = None
    allowed = [t for t, g in TAXONOMY.items() if g == 'niche' and NICHE_RX[t].search(source)]
    if v.get('niche') not in allowed:
        v['niche'] = None
    fit = int(max(0, min(ROLE_CAP[v['role']], v['fit'])))
    content_fit = min(ROLE_CAP[v['role']], fit)
    score = blend(content_fit, net if net is not None else net_from_tags(tags))
    # Showing the verified quote avoids laundering unsupported generated prose into facts.
    reason = 'Profile says: "' + evidence[0] + '".'
    extra = [t for t in (v.get('extra_tags') or []) if t in allowed]
    if v.get('niche') in allowed:
        extra.insert(0, v['niche'])
    have = {t for t, _ in tags or []}
    rt = ROLE_TAG.get(v['role'])
    if rt and not (have & {'Brand', 'Store'} if rt == 'Brand' else have & {'Agency', 'Freelancer'} if rt == 'Agency' else rt in have):
        extra.append(rt)
    new = [(t, TAXONOMY[t]) for t in dict.fromkeys(extra) if t not in have]
    fit_tag = 'Fit: strong' if fit >= 75 and v['role'] == 'buyer' else 'Fit: good' if fit >= 55 and v['role'] in ('buyer', 'connector') else None
    if fit_tag:
        new.append((fit_tag, 'signal'))
    new += [(t, 'ai') for t in ai_tags(v, fit) if t not in have]
    brand = v.get('brand_handle')
    brand = brand.strip() if isinstance(brand, str) and re.fullmatch(r'@?[\w.]{2,30}', brand.strip()) else None
    if brand and not (v['role'] == 'buyer' and decision and re.search(r'(?<![\w.])@' + re.escape(brand.lstrip('@')) + r'(?!\w|\.\w)', source, re.I)):
        brand = None
    return {'score': score, 'content_fit': content_fit, 'role': v['role'], 'reason': reason,
            'tier': _tier(score, bool(str(person.get('bio') or '').strip())),
            'model': used, 'fit': fit, 'tags': new, 'evidence': evidence, 'brand_handle': brand, 'prompt': version}


def llm_verdicts(items, examples=None, timeout: float = 45, models=None, budget: float = LLM_BUDGET) -> list:
    """items = [{'person','tags','edges','net'?}] -> one verdict dict or None per item (None: rule verdict stays, retried later).
    Profiles go LLM_BATCH per call; a batch reply missing someone leaves only that person at None."""
    out = [None] * len(items)
    version = prompt_version(examples)
    deadline = time.monotonic() + budget
    for i in range(0, len(items), LLM_BATCH):
        chunk = items[i:i + LLM_BATCH]
        left = deadline - time.monotonic()
        if left <= 1:
            break
        user = '\n\n'.join(f"### id={k}\n" + _packet(it['person'], it.get('tags'), it.get('edges'), it.get('net')) for k, it in enumerate(chunk))
        msgs = [{'role': 'system', 'content': _system(examples, len(chunk))}, {'role': 'user', 'content': user}]
        try:
            text, used = _providers().chat(msgs, models=models, timeout=timeout, budget=left, max_tokens=900 * len(chunk) + 600)   # room for models that think out loud before the JSON
        except llm.Unavailable:
            continue
        data = parse_json(text)
        if not data:
            continue
        results = data.get('results') if isinstance(data.get('results'), list) else [data] if len(chunk) == 1 else []
        identified = {}
        duplicates = set()
        for r in results:
            if not isinstance(r, dict):
                continue
            rid = r.get('id')
            if isinstance(rid, str) and len(rid) <= 9 and re.fullmatch(r'\d+', rid):
                rid = int(rid)
            if isinstance(rid, bool) or not isinstance(rid, int) or not 0 <= rid < len(chunk):
                continue
            if rid in identified:
                duplicates.add(rid)
            identified[rid] = r
        for rid, r in identified.items():
            if rid in duplicates:
                continue
            it = chunk[rid]
            handle = r.get('handle')
            expected = str(it['person'].get('handle') or '').lstrip('@').casefold()
            if not expected or not isinstance(handle, str) or handle.strip().lstrip('@').casefold() != expected:
                continue
            try:
                out[i + rid] = _verdict(r, it['person'], it.get('tags'), used, version, it.get('net'))
            except (TypeError, ValueError, OverflowError):
                # One malformed model result must not discard other people's valid replies.
                continue
    return out


def llm_verdict(person: dict, tags, edges, timeout: float = 45, models=None, budget: float = LLM_BUDGET, net=None, examples=None) -> dict | None:
    """None when no model gives a usable answer in time: the caller keeps the rule verdict and retries later."""
    return llm_verdicts([{'person': person, 'tags': tags, 'edges': edges, 'net': net}], examples, timeout, models, budget)[0]


def legacy_input_hash(person: dict, edges, net=None) -> str:
    keys = ('handle', 'name', 'bio', 'website', 'category', 'followers', 'following', 'posts', 'is_private', 'is_verified', 'is_business')
    payload = [PROMPT_VERSION, [person.get(k) for k in keys], sorted({(str(e.get('seed') or '').lower(), str(e.get('direction'))) for e in edges or []})]
    if net is not None:
        # These are the network facts used by the score and the model packet.
        # Ordering and transient database timestamps must not affect the key.
        payload.append({'lists': net.get('lists'),
                        'seeds': sorted({(str(s).lower(), str(d)) for s, d in net.get('seeds', [])}),
                        'seed_yield': net.get('seed_yield'), 'seed_marked': net.get('seed_marked'),
                        'client_seeds': net.get('client_seeds'), 'me': net.get('me')})
    owner = [person.get('status'), (person.get('note') or '').strip(), sorted(person.get('manual_tags') or [])]
    if any(owner):   # appended only when set, so hashes of people Michael never touched stay as they were
        payload.append(owner)
    return hashlib.sha256(json.dumps(payload, default=str, ensure_ascii=False).encode()).hexdigest()[:16]


def input_hash(person: dict, edges=None, net=None) -> str:
    """Business-fit evidence identity. Graph changes only reblend the saved fit."""
    keys = ('handle', 'name', 'bio', 'website', 'category', 'followers', 'following', 'posts',
            'is_private', 'is_verified', 'is_business')
    payload = ['content-v1', PROMPT_VERSION, [person.get(k) for k in keys],
               [person.get('status'), (person.get('note') or '').strip(),
                sorted(person.get('manual_tags') or [])]]
    return hashlib.sha256(json.dumps(payload, default=str, ensure_ascii=False).encode()).hexdigest()[:16]
