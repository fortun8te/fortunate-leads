# Fortunate Leads — build contract

One local tool: a Chrome extension collects Instagram lists and bios at a safe pace, a small Python server stores and qualifies them, a one-page UI shows who is worth contacting and why.

Owner: Michael (@fortun8te). Agency: Fortunate — static ad creatives and product visuals for physical-product (DTC/e-commerce) brands, mostly US; Dutch operators selling to the US also fit. Min engagement ~EUR 2,000.

## Layout (repo root `~/fortunate-leads`)

```
extension/   MV3 extension (plain JS, no build step)
server/      Python 3 stdlib only (3.9+, SQLite 3.35+): server.py, db.py, accounts.py, qualify.py, llm.py, laya.py, rules.py,
             migrate.py, tests/
web/         index.html, app.js, app.css, mock.js (?mock=1), vendor/ (vendored libs and fonts, no CDN at runtime)
sidecar/     optional Laya decision sidecar (own venv)
ops/         LaunchAgent install, doctor, backup (macOS)
tests/       e2e/ (simulated Instagram + real extension + real server), bench.py (100k-people timings)
data/        leads.sqlite, pfp/, backups/, openrouter.json (gitignored)
docs/        SETUP.md, CONTRACT.md, HANDOFF.md, RESEARCH.md, ui/ (screenshots)
```

Server: `python3 server/server.py` → serves `http://127.0.0.1:8777` (UI at `/`, static from `web/`). DB path `data/leads.sqlite` (override `--db`). Bind 127.0.0.1 only.

Write endpoints under `/api/ext/*` accept requests only when `Origin` is `chrome-extension://fgdbghllamedgihmdcolaggnbhnakjnf` (the manifest `key` keeps that id). UI endpoints accept only same-origin (Host 127.0.0.1:8777 / localhost:8777); **every UI `POST` must carry `Origin: http://127.0.0.1:8777` (or localhost)** — browsers send it, `curl` needs `-H 'Origin: http://127.0.0.1:8777'`. GETs need no Origin. No tokens, no pairing.
Every response carries `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`.

## Data model (SQLite, WAL)

```sql
people(id INTEGER PRIMARY KEY, ig_id TEXT UNIQUE, handle TEXT UNIQUE NOT NULL COLLATE NOCASE,
  name TEXT, pic_url TEXT, pic_file TEXT, is_private INT, is_verified INT,
  bio TEXT, website TEXT, category TEXT, followers INT, following INT, posts INT, is_business INT,
  bio_at TEXT, bio_src TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL)
seeds(handle TEXT PRIMARY KEY COLLATE NOCASE, ig_id TEXT, is_me INT DEFAULT 0, added_at TEXT)
lists(seed TEXT, direction TEXT CHECK(direction IN('followers','following')), state TEXT,  -- queued|running|done|paused|error|private
  cursor TEXT, received INT DEFAULT 0, total INT, error TEXT, updated_at TEXT, PRIMARY KEY(seed,direction))
edges(seed TEXT, person_id INT, direction TEXT, first_seen TEXT, PRIMARY KEY(seed,person_id,direction))
  -- direction='followers': person follows seed. 'following': seed follows person.
tags(person_id INT, tag TEXT, grp TEXT, source TEXT CHECK(source IN('auto','manual','rule')), PRIMARY KEY(person_id,tag))
  -- auto: qualifier (rebuilt on requalify) · rule: user tag rules (synced on ingest) · manual: Michael. One source per (person, tag).
  -- indexes tags_tag_src(tag,source,person_id,grp) covering, tags_person_src(person_id,source,tag).
  -- DBs from before 'rule' are rebuilt in place by db.migrate_tags() on start (data kept).
tag_rules(id INTEGER PRIMARY KEY, tag TEXT, grp TEXT DEFAULT 'signal', field TEXT CHECK(field IN('bio','name','handle','category','website','any')),
  match TEXT, created_at TEXT)
saved_views(id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, query TEXT, created_at TEXT)   -- query = URL query string, no '?'
verdicts(person_id INT PRIMARY KEY, prefilter INT, score INT, tier TEXT, role TEXT, reason TEXT,
  model TEXT, input_hash TEXT, updated_at TEXT, prompt TEXT, evidence TEXT)   -- tier: hot|warm|cold|unread
  -- prompt = qualify.prompt_version (prompt + few-shot set) of an LLM verdict; evidence = JSON list of exact bio quotes
laya(person_id INT PRIMARY KEY, input_hash TEXT, answers TEXT, fit INT, updated_at TEXT)   -- optional Laya sidecar answers (soft signal)
marks(person_id INT PRIMARY KEY, status TEXT, note TEXT, updated_at TEXT)  -- interested|contacted|talking|client|no (no = Not a fit). 2026-09 migration: good→interested; maybe/known→status cleared + manual tag 'Maybe' / 'Already know them' (notes kept)
jobs(id INTEGER PRIMARY KEY, kind TEXT CHECK(kind IN('list','profile')), seed TEXT, direction TEXT, handle TEXT,
  priority INT DEFAULT 0, state TEXT DEFAULT 'queued', attempts INT DEFAULT 0, leased_until TEXT,
  lane TEXT, lease_token TEXT, created_at TEXT)
  -- state queued|leased|done|error. attempts = leases that expired or ended in an 'other' error (rate_limit/soft_block/
  -- login/challenge give the lease back). 5 'other' errors park a job ('error'); a profile job whose lease expired 5 times
  -- is parked too. List jobs reset attempts on every page.
settings(key TEXT PRIMARY KEY, value TEXT)   -- json values; includes paused, budgets, ext heartbeat, qualify, qualify_auto,
  -- llm_workers (4), llm_min (40), bio_min (25), main_list_share (0), budget ({list: 3000, profile: 300} per account per day),
  -- fewshot (frozen example set + its version), llm_rev (bumped by every LLM verdict write; part of data_rev)
pages(job_id INT, cursor TEXT, at TEXT, lane TEXT, users INT, PRIMARY KEY(job_id,cursor))   -- list pages ingested (idempotency + rates)
accounts(lane_id TEXT PRIMARY KEY, ig_id, handle, label, role 'lists'|'bios'|'both', budget JSON|NULL, paused, is_main,
  first_seen, last_seen, version, state, hold 'login'|'challenge'|NULL, cooldown_until, list_cool_until, rate JSON, today JSON,
  last_error, activity, text)   -- one row per extension install (Chrome profile); jobs.lane / lists.lane(+prev_lane, released_*)
public_bio_results(id INTEGER PRIMARY KEY, job_id, handle, at, outcome, cache_hit, source, observed_at, error)
public_bio_retries(job_id INTEGER PRIMARY KEY, next_at, failures)
```
Indexes worth knowing: `edges_person_seed(person_id, seed)` covering (lists count, seeds per person), `people_bio_at`,
`tags_tag_src`, `tags_person_src`, `pages_lane_at`. Connections use WAL, `synchronous=NORMAL`, `mmap_size=256 MB`,
`temp_store=MEMORY`. Columns added after the first release are added on start when missing.

## Qualifier interface (`server/qualify.py`, pure functions + one LLM call)

```python
TAG_GROUPS = ('role','niche','signal','size','source')   # UI groups tags by this (monochrome)
TAGS_VERSION = 't2'   # bump when rule tags change: on start the server lets the qualify batch re-derive all auto tags once
def prefilter(person: dict, seeds: list[str], net=None, laya_fit=None) -> int   # 0-100, no bio needed; orders the profile reads
def network_strength(net) -> int                               # 0-100 from the network context (below)
def blend(content, net) -> int                                 # NET_WEIGHT (0.6) * network + 0.4 * profile read
def rule_tags(person: dict, edges: list[dict], me: str|None) -> list[tuple[str,str]]   # [(tag, grp)]
def rule_verdict(person: dict, tags, net=None) -> dict         # {'score','role','reason','tier'} with no model
def llm_verdict(person: dict, tags, edges, ..., net=None, examples=None) -> dict | None   # same keys + 'model'; None if unavailable
def llm_verdicts(items, examples=None) -> list[dict|None]      # items [{person,tags,edges,net}], LLM_BATCH (4) profiles per call
def prompt_version(examples) -> str                            # PROMPT_VERSION + few-shot set hash, stored in verdicts.prompt
def input_hash(person: dict, edges) -> str                     # cache key; unchanged hash = skip re-run
```
Auto tag vocabulary (all `grp` fixed by `qualify.TAXONOMY`):
- role: Brand, Store, Agency, Freelancer, Creative (photographer/UGC/3D/video), Creator, Supplier, SaaS, Coach, Personal
- niche: Skincare, Beauty, Supplements, Apparel, Jewelry, Home, Pets, Coffee, Food & Drink, Fitness, Wellness, Baby, Accessories, Outdoor, Tech Gadgets
- signal: Founder, Scaling, Hiring, Ecom (DTC/e-commerce/Shopify words), Shopify, Shop Link, Link Hub, Email, US, NL, UK, Verified, Business
- size: <1k, 1k-10k, 10k-100k, 100k-1M, 1M+ · source: via @seed, in N lists, Instagram link, follows you, you follow
Precision over recall: promo codes for someone else's brand ("code X at @brand") count as Creator, not Brand; "dog owner", "CEO of my life",
"of course", "available on Spotify", "model agency", "mama to baby", affiliate storefronts (shopmy/LTK) and look-alike domains
(restorehealth.com, theworkshop.com) no longer fire. Instagram's own category label is used only when unambiguous ("… (Brand)", "E-commerce website", "Jewelry/watches").

**Weighting.** Every score (prefilter, rule verdict, LLM verdict) is `blend(profile read, network)`: 60 % network strength, 40 %
the profile read. Network strength: 30 for one list, +25/+38/+46 for 2/3/4 lists (+3 per list beyond), +5 per seed that follows
them (max 10), seed yield (y − 0.25) × 60 clamped −12..+20, Michael mutual/follows/followed +16/+10/+8, +6 per linked positive mark (interested/talking/client)
account (max 18). The profile read is handle/name signals before a bio, then the rule score or the LLM fit capped by role (unrelated 40,
peer 50, supplier 55, collaborator/unclear 65, connector 80, buyer 100). Without a network context the source tags stand in.

Staged pipeline (all stages run in background threads; HTTP handlers and ingest never wait on them):
1. **Prefilter** from list data only (name, handle, verified, private, seeds and direction, lists count) plus the **network context**
   (`server.network_context`): `seeds` [(seed, direction)] — being followed BY a seed weighs more than following it; `lists`;
   `me` = `mutual|follows|followed` for Michael's own (`is_me`) seed; `seed_yield` = best seed's (positive+1)/(marked+4) from
   Michael's marks, with `seed_marked`; `client_seeds` = the person's seeds that are people marked interested/talking/client. Laya (optional) adds
   a 25 % weighted soft signal. Private accounts stay ≤ 35. This orders the bio-read queue, so people get a first pass before any read.
2. **Rule tags** + rule verdict after the bio read (and for everyone without one).
3. **Laya** (optional, `server/laya.py`): `POST http://127.0.0.1:18742/decide` in batches of 64 with 5 questions (dtc_founder,
   brand_account, creator, service_provider, netherlands), 60 s timeout; `GET /health` 3 s, cached 60 s; down → skipped silently.
   Only a soft ranking signal: never a gate or verdict; it tags on its own only at p ≥ 0.9 for creator / brand_account / netherlands.
   Runs on people with a bio first, then list-only people.
4. **LLM** for the top candidates only: bio read, rule verdict current, `(prefilter + rule score) / 2 ≥ llm_min`, best first. Rubric:
   founder/decision-maker of a DTC physical-product brand, US or NL-selling-to-US, able to pay ~EUR 2k. JSON reply per profile:
   `role, niche, brand_handle, decision_maker, fit, evidence[], reason, extra_tags`; evidence quotes not found in the profile are
   dropped. Tags from the verdict: role, niche, `Founder` (decision-maker of a brand), `Fit: strong` (buyer, fit ≥ 75), `Fit: good`
   (buyer/connector, fit ≥ 55). Few-shot: up to 8 interested/talking/client and 8 `no` marks with a bio. A new Client enters
   future LLM prompts immediately; selected examples update when their note, manual tags, status, or profile changes. Other
   new marks enter when the mark count moves by ≥ 5 (or 20 %). That larger change also re-runs LLM verdicts from another
   prompt version with score ≥ 35 (warm or near it). Bounded pool: `llm_workers` concurrent
   calls (default 4) spread across providers/keys.

`person` dict = the `people` row. `edges` = list of `{seed, direction}`. Tiers: hot ≥70, warm 45–69, cold <45, `unread` when there is no bio yet (private or not read). `source` group tags are generated from edges: `via @seed`, `follows @seed`, `followed by @seed`, `in N lists` (N≥2), `Instagram link` when `me` is linked. LLM goes through the local OpenRouter proxy `http://127.0.0.1:18741/api/v1/chat/completions` (free models only). Per-model socket timeout 45 s and a 90 s budget per verdict across models; any transport error, non-JSON/non-object reply, missing content or model substitution = "unavailable" (`None`). The server then keeps the rule verdict and retries that person after 30 min; an exception inside `llm_verdict` is treated the same way.

**Providers** (`server/llm.py`, stdlib): the local proxy first, then `https://openrouter.ai/api/v1/chat/completions` direct, rotating
keys from `OPENROUTER_API_KEYS` (comma-separated) and `data/openrouter.json` `{"keys":[...],"models":[...],"daily_limit":n}` (written
by the Settings page, mode 600, reloaded at once; state is keyed by a key id = sha256(key)[:10], so counters survive edits).
429/402/408/5xx → that key+model cools down (Retry-After, else 30 s doubling to 1 h); 401/403 → key disabled until restart or a passing test;
transport error → provider cools 10 s doubling to 5 min. Free models first; per key+model daily counter (default 1000/day).
Headers `HTTP-Referer`, `X-Title: Fortunate Leads`; `response_format: json_object`. Keys are never logged or returned (`sk-…abcd`).
Only model IDs ending in `:free` are accepted for qualification. A free-model catalog entry is not a
guarantee that a request will succeed. Rule verdicts remain when all models fail.

## Extension ↔ server

Extension is a paced executor. Server owns the queue; extension owns pacing, budgets and cooldowns.

Every extension call carries its lane: `lane_id` in POST bodies and `?lane=` on GETs, plus the logged-in account
(`account:{ig_id,handle}` / `&ig_id=&handle=`). Builds without it are the `default` lane. See `server/accounts.py`.

- `GET  /api/ext/next?kinds=list,profile&lane=` → `{"paused":bool, "budget":{list,profile}, "job": null | {"id","kind":"list","seed","ig_id"|null,"direction","cursor"|null,"received"} | {"id","kind":"profile","handle","ig_id"|null}}`.
  Leases a job to that lane for 10 min; an expired lease is handed out again (same job id, list cursor = last saved page). A leased job never goes to a second lane.
  Lists: the lane's own list first, then lists another lane left mid-way, then by priority, running lists first, **following before followers**. A list sticks to its lane while that lane is healthy (seen in 10 min, no login/challenge hold, not paused, no list cooldown); otherwise it is released and the next lane resumes it from the saved cursor (recorded in the `handoffs` setting). Role `bios` or a main account (unless `main_list_share` > 0) never gets lists.
  Profile planner: people without a bio, not private, not a seed, not a parked `handle~id` row, not marked `no`, **prefilter ≥ `bio_min`** (25), ordered by prefilter desc, then lists count; job priority = `prefilter*10 + min(lists,9)` (a UI "read now" uses 10000 and ignores `bio_min`). Qualification on: everyone eligible. Still collecting (qualify off, `qualify_auto` on): only people in ≥ 2 lists. Keeps queued jobs up to the remaining bio budget of the lanes that read bios (200 when a budget is 0 = no daily number).
- Extension pacing (per install, `extension/lib/core.js`): list pages 7–12 s apart with a 90–180 s break every 40–60 pages; bios 35–70 s apart on their own clock (so they fill list gaps and breaks); any two requests ≥ 2–5 s apart; a sliding window caps an account at 72 requests per 11 min; hits cool that bucket 10 min doubling (Retry-After wins, 3 hits in 24 h = done for today) and pause the lane 5 min. Budgets per account per day: 3000 list pages, 300 bios (server-overridable per account).
- `POST /api/ext/list-page` `{"job_id","seed","ig_id","direction","users":[{"ig_id","handle","name","pic_url","is_private","is_verified"}],"next_cursor":str|null,"done":bool,"total":int|null}`
- `POST /api/ext/profile` `{"job_id":int|null,"profile":{"ig_id","handle","name","bio","website","category","followers","following","posts","is_private","is_verified","is_business","pic_url"}}` — `job_id:null` = passively captured while Michael browsed Instagram (free bio, no extra request). `website` is stored only if it matches `^https?://`; counts are coerced to int ≥ 0 or null (`"1,234"` → 1234).
  A new `ig_id` arriving under a handle that belongs to a row with a different `ig_id` parks the old row as `handle~id` (it keeps its marks/edges/tags) and creates a new person.
- `POST /api/ext/error` `{"job_id","code":"rate_limit|soft_block|challenge|login|private|not_found|other","retry_at":iso|null,"message"}` — private/not_found finish the job; others release it. rate_limit/soft_block record a cooldown for display. challenge/login do **not** pause the server (the extension holds itself until Michael resumes it in the popup; the UI reads `ext.state`); the server `paused` flag is only set by `POST /api/scraper/pause`.
- `POST /api/ext/heartbeat` `{"version","state":"running|idle|paused|cooldown","cooldown_until":iso|null,"cool"?:{"list":iso|null,"profile":iso|null},"hold"?:"login"|"challenge"|null,"today":{"list":n,"profile":n},"budget":{"list":n,"profile":n},"last_error":str|null,"activity","text","rate"?:{"pages_hour":num,"people_hour":num,"last_hit_at":iso|null}}` every ≤30 s → `{"paused","budget"}` for that lane. A hold or list cooldown releases the lane's lists at once. Bad values are stored as null.

All return `{"ok":true}` or `{"ok":false,"error":...}`. Server is idempotent: re-sent pages must not duplicate edges (`pages(job_id, cursor)`; a re-sent page never moves the cursor back; a late page for a job that is already done/error keeps its people but never changes the list's state or cursor).
Request bodies must be JSON objects (else 400); non-string text fields in profiles/users are dropped. Every list page and profile re-applies the tag rules to the people it touched.

## UI API

GET endpoints return the JSON directly; POST endpoints return `{"ok":true,...}`; errors are `{"ok":false,"error"}` with 400 (bad input) / 403 / 404 (unknown person id) / 500.

### Shared lead filter (`/api/leads`, `/api/counts`, `/api/tags`, `/api/map`)
| param | meaning |
|---|---|
| `tags=a,b` | has **all** of these tags (any source) |
| `any=c,d` | has **at least one** |
| `not=e,f` | has **none** |
| `status=` | absent: everyone except `no`; `interested,talking`: any of these; `none`: unmarked (combinable: `status=interested,none`); legacy `good` = `interested`; `all`: no status filter |
| `q=` | substring of handle, name or bio (`%` and `_` are literal) |
| `min_lists=N` | linked to ≥ N distinct seeds |
| `has_bio=1\|0` | bio read and non-empty / not |
| `seed=h` | linked to that seed (any direction); `seed=a,b` = linked to all of them |
| `followers_min=`, `followers_max=` | inclusive; people with unknown followers drop out when either is set |
| `tier=hot,warm` | legacy, still works |

Bad numbers / `has_bio` / `status` values → 400. Tag values are exact (case-sensitive) tag names, URL-encoded (`via%20%40seed`).

- `GET /api/leads?<filter>&sort=score|fit|recent|followers|connected&offset=0&limit=50` → `{"total", "rows":[{"id","handle","name","pic","bio","website","followers","following","posts","tier","score","role","reason","tags":[{"tag","grp","source"}],"via":["seed",...],"lists":int,"status"}]}` — `tags` ordered manual first, then rule, then auto; inside a source role, niche, signal, size, source. `pic` = `/img/{id}` or null. `note` = Michael's note or null (search `q=` also matches notes). `lists` = distinct seeds the person is linked to by any edge (both directions count once). `sort=connected` = `lists` desc, then followers desc. `sort=fit` = tier (hot, warm, cold, unread), then `lists` desc, then score. Another sort value is a 400. Michael's own account is never a row.
- `GET /api/tags?<filter>` → `[{"tag","grp","source":"auto"|"rule"|"manual","count","total"}]` — one entry per (tag, source); `count` = people in the current filtered set with it, `total` = overall. Sorted count desc, total desc, tag. Tags with `count: 0` are included.

### Tag management (manual tags)
- `POST /api/person/{id}/tags` `{"add":["..."],"remove":["..."]}` — add makes the tag manual (an auto/rule tag of the same name becomes manual); remove deletes the tag whatever its source (auto/rule tags come back when their rule still matches).
- `POST /api/tags/rename` `{"from","to"}` → `{"ok","renamed":n}` — manual tags only. If a person already has `to` (any source) the two merge into one manual `to`. Group: that of an existing `to`, else of `from`.
- `POST /api/tags/delete` `{"tag"}` → `{"ok","deleted":n}` — manual tags only; auto/rule tags of that name stay.
- `POST /api/people/bulk` `{"ids":[..≤5000], "add"?:[..], "remove"?:[..], "status"?:null|"interested"|"contacted"|"talking"|"client"|"no"}` → `{"ok","updated":n}` (n = ids that exist). `status` key absent = marks untouched; `null` = clear the status (notes kept).
- Tag names: trimmed, inner whitespace collapsed, 1–64 chars, no commas (commas separate filter values) → else 400.

### Tag rules (user-defined auto tagging, source `rule`)
- `GET /api/tag-rules` → `[{"id","tag","grp","field":"bio|name|handle|category|website|any","match","hits"}]` — `hits` = people carrying the tag with source `rule` (two rules with the same tag share it).
- `POST /api/tag-rules` `{"tag","field","match","grp"?}` → `{"ok",...same fields}` — creates and applies to everyone immediately (an identical tag+field+match returns the existing rule). `grp` defaults to the group the tag already has, else `signal`.
- `GET /api/tag-rules/preview?field=&match=` → `{"hits":n}` — people the rule would tag right now; same matcher and guards, nothing written (5 s budget; debounce while typing).
- `POST /api/tag-rules/{id}/delete` → `{"ok","deleted":0|1}` — removes the rule and its rule tags (people another rule with the same tag still matches keep it).
- `match`: comma-separated keywords, case-insensitive. In bio/name/category a keyword matches whole words (`dtc` not in `dtcx`; `vegan*` = prefix; spaces match any whitespace); in handle/website it is a substring. `/regex/` = Python regex, case-insensitive, same field semantics.
- Guards (→ 400 with a reason): regex ≤ 200 chars, ≤ 6 quantifiers of which ≤ 2 are `*`/`+`/wider than `{0,3}`, no quantified group containing `|` or another quantifier (`(a+)+`, `(?:a?){26}`, `(ab|cd)+`), no backreferences, must run the worst-case probe strings in < 5 ms; keywords ≤ 1000 chars, ≤ 100 keywords, one `*` each. Only the first 160 chars of each field are scanned. A full scan over everyone that takes > 20 s is abandoned (400, nothing written). The scan runs before any write, so it never blocks ingest.
- Rules re-apply to the people touched by every list page and profile, and in the qualify batch; a rule tag disappears when its person stops matching. Manual and auto tags are never touched by rules.

### Saved views
- `GET /api/views` → `[{"id","name","query"}]` (sorted by name). `POST /api/views` `{"name","query"}` → `{"ok","id","name","query"}` — `query` is the URL query string (leading `?` stripped); saving an existing name (any case) overwrites it. `POST /api/views/{id}/delete` → `{"ok","deleted":0|1}`.

### Follow-ups, activity and CSV export (2026-09-26)

- Lead rows also include `bio_at`, `bio_src` and `follow_up`, either null or `{due_on,note,completed_at,updated_at}`. Person detail includes `profile_read_pending` and `activity:{rows,next_cursor}` with the newest 50 entries.
- `POST /api/person/{id}/follow-up` accepts `{due_on:"YYYY-MM-DD",note?:str}` with a valid calendar date and a note up to 500 characters. An existing reminder is rescheduled and reopened. `{action:"complete"}` completes it; `{action:"clear"}` removes it. Action and date/note fields cannot be mixed. Responses include `follow_up`. Unchanged operations append no event.
- Shared filters for leads/counts/tags/map and filtered export accept `follow_up=due|overdue|scheduled|completed|none` and `today=YYYY-MM-DD`. Due means unfinished and `due_on <= today`; overdue uses `<`; scheduled means any unfinished reminder. None means no reminder row, not a completed row. Without `today`, the server's local calendar date is used. The UI injects the browser's current local date into requests and leaves it out of saved queries. `sort=follow_up` places open reminders first by due date, then person ID.
- `GET /api/person/{id}/activity?limit=50&cursor=...` returns `{rows,next_cursor}`. Limit is 1-100. Treat `next_cursor` as opaque and URL-encode it. Ordering is occurrence time descending, then ID descending. Each row has `{id,kind,body,before_value,after_value,happened_at,created_at}`; before/after are decoded JSON values.
- `POST /api/person/{id}/activity` accepts `{kind:"dm"|"reply"|"call"|"meeting"|"note",body,happened_at?}`. Body must contain 1-5,000 characters. Optional occurrence time must be an ISO timestamp with timezone; otherwise server time is used. Times normalize to UTC. Returns the latest history page plus `ok`.
- Automatic event kinds include `status`, `note`, `follow_up_scheduled`, `follow_up_completed`, `follow_up_cleared`, `follow_up_merged` and `identity_merged`. Status and note events record real changes, including bulk actions and undo. New tables are additive; existing states are not backfilled as invented historical events. Identity merging moves all activity and preserves conflicting reminder/note context.
- `POST /api/leads/export` accepts exactly one of `{query:"URL query string"}` or `{ids:[positive integer IDs]}`. Query limit: 16,000 characters. Selected limit: 1-5,000 IDs; duplicate IDs collapse, missing IDs fail visibly. Selected scope is independent of filters. Filtered scope uses the shared filter, excludes the owner's account, and respects sorting; pagination parameters do not truncate it.
- Successful export returns raw UTF-8 CSV with BOM, `Content-Disposition: attachment; filename="fortunate-leads.csv"` and `Cache-Control: no-store`. Failures remain ordinary JSON errors with existing same-origin checks. Fields: `id,handle,name,instagram_url,bio,website,followers,following,posts,tier,score,role,status,note,tags,sources,bio_at,bio_src,follow_up_due,follow_up_note,follow_up_completed_at`. Tags/sources are semicolon-separated. Empty filtered results still return the header. Text formula prefixes are guarded; numeric values stay numeric. CSV is assembled in memory and is not a complete backup.

### People, map, scraper
- `GET /api/counts?<filter>` → `{"hot","warm","cold","unread","interested","contacted","talking","client","no","none","open","total","with_bio"}` — tier counts within the filter without its `tier`; status counts within the filter without its `status` (`none` = unmarked, `open` = all but `no`); `total`/`with_bio` = the whole database. Cached per query and `data_rev`.
- `GET /api/person/{id}` → lead row + `{"edges":[{"seed","direction"}],"verdict":{...,"evidence":[str]},"note"}` — `evidence` is always a list.
- `POST /api/person/{id}/mark` `{"status"?:null|"interested"|"contacted"|"talking"|"client"|"no","note"?:str|null}` — an absent key is left as it is; `status:null` clears the status but keeps the note; `note:""`/`null` clears the note; the row goes when both are empty.
- `POST /api/person/{id}/read` → queue a profile read now (priority); 400 for a parked `handle~id` row
- `GET /api/map?<filter>&scope=leads|all&limit=400` → `{"nodes":[...],"links":[{"source":"s:seed","target","direction"}],"seed_links":[{"source":"s:a","target":"s:b","shared":n}],"rev":int}`
  - seed node: `{"id":"s:handle","kind":"seed","label","tier","score","pic","degree","followers","status","lists","tags","seeds","is_me","pid","note"}` — `pid` = the seed's own person id (null if never read; with it the UI opens that person's panel), `note` = Michael's note or null; `degree` = edges into that seed's lists; `followers`/`status`/`tags`/`lists`/`seeds` describe the seed's own person row (a seed can sit in other seeds' lists), empty/0/null if unknown. `is_me` = Michael's own account.
  - lead node: `{"id":"p:123","kind":"lead","label","handle","name","tier","fit":"strong|good|weak|unread","score","reason","pic","degree","lists","status","note","followers","tags","seeds"}` — `degree` = `lists` = distinct seeds; `tags` = up to 4 tag names, manual first (same order as leads); `seeds` = seed handles it is linked to.
  - The filter applies to leads only; all seeds are always returned. `scope=leads`: up to 60 % people linked to ≥ 2 seeds (most connected first), the rest by score. `scope=all`: by score. Default excludes `status=no`, as leads.
  - `seed_links` = top 50 seed pairs by shared audience (distinct people linked to both, any direction; ties by pair name), not filtered; recomputed when edges change, at most every 30 s while a list streams in. The rest of the response is cached per query and `data_rev` (people, verdicts, edges, seeds, marks, tags, tag rules, LLM writes).
- `GET /api/scraper` → `{"llm":<same as /api/llm>,"ext":{"online","version","state","cooldown_until","today","budget","last_seen","activity","text","rate":{"pages_hour","people_hour","last_hit_at"}|null,"last_error"},"paused","qualify":bool,"qualify_auto":bool,"soak":{"1h":{"pages","people","new_people","profiles"},"6h":{...}},"people_today","lists":[{"seed","direction","state","received","total","updated_at","error"}],"queue":{"list":n,"profile":n}}` — `soak`: list pages ingested (retries not counted), edges added, people first seen, bios read in the window.
- `POST /api/scraper/seeds` `{"handles":["a","b"],"directions":["followers","following"]}`
- `POST /api/scraper/pause` `{"paused":bool}`
- `GET /api/control` (also `/api/ext/control` for the extension) → `{"stages":[{"id":"lists|bios|ai","label","help","state":"running|waiting|idle|paused","paused","now":sentence,"wait":{"why","seconds"}|null,"hour","today","unit","queue"}],"accounts":[{"lane_id","name","state","now","wait","paused","online","hour","today"}],"all_paused","at"}` — the control strip (web/controls.js, extension widget). AI also has `minute`; its minute, hour, and today counts come from committed model-score events. Older verdicts have no known scoring time and do not count toward these rates. Stages sit on settings `paused_lists`, `paused_bios` (plus the legacy `paused`, which covers both) and `qualify`.
- `POST /api/control` `{"stage":"lists|bios|ai|all","action":"pause|resume"}` or `{"account":lane_id,"action"}` → the new snapshot. `all` + pause = Stop everything. A paused lists/bios stage is filtered out of `/api/ext/next` (its response carries `"stages":{"list":bool,"profile":bool}`); pausing AI also turns `qualify_auto` off. Heartbeat 3.7+ may send `"ready":{"list":iso,"profile":iso}` (next allowed request per clock) so the strip can show breaks.
- `POST /api/scraper/budget` `{"list":n,"profile":n}` — the default per account per day: list ≤ 3000, profile ≤ 5000, `0` = no daily number.
- `POST /api/scraper/snowball` `{"min_status"?:"interested"|"client","limit"?:1-500}` → `{"ok","queued":n,"seeds":[...]}` — **opt-in** (never
  automatic): queues the `following` list of each good+client (or client only) person that is not private, not parked and has no
  following list yet (default limit 50, most recently marked first). Their people then pick up the same network signals.
- `GET /api/llm` → `{"providers":[{"id":"proxy"|keyid,"name":"proxy"|"openrouter","url","key":"sk-…abcd"|null,"source":"env"|"file"|null,
  "disabled","cooldowns":{model:iso},"requests_today":{model:n},"last_error"}],"models","daily_limit","workers","llm_min","bio_min",
  "laya":{"url","up":bool|null},"config":"data/openrouter.json","verdicts":{"rules":n,"llm":n,"error":n}}` — never blocks on the sidecar.
- `GET /api/llm/health` → `{"proxy":{"url","up"},"laya":{"url","up"}}` — probes now (≤ 1 s proxy, ≤ 3 s Laya).
- `POST /api/llm/keys` `{"key"}` → `{"ok","id"}` (400: not a key / already added) · `POST /api/llm/keys/{id}/remove` (400 for an env key,
  404 unknown) · `POST /api/llm/keys/{id|proxy}/test` → `{"ok","passed":bool,"model","ms","error"|null}` (one tiny request; a pass re-enables
  a refused key) · `POST /api/llm/models` `{"models"?:[1-12 ids],"daily_limit"?:0-100000}` → `{"ok","models","daily_limit"}`.
  Full keys are never returned; UI POSTs only (same-origin Origin).
- `POST /api/settings/qualify` `{"on"?:bool,"auto"?:bool,"workers"?:1-16,"llm_min"?:0-100,"bio_min"?:0-100}` → `{"ok":true,"qualify":bool}` — absent keys are left alone. `on` starts/stops LLM verdicts and full bio planning (rule verdicts always run). `qualify_auto` (default true) switches qualification on by itself once lists exist and none is queued/running.

### Accounts (lanes)
- `GET /api/accounts` → `{"accounts":[account],"alerts":[{"level":"error"|"warn","lane_id"|null,"text"}],"rate":{...},"main_list_share":0-1}`;
  account = `{"lane_id","ig_id","handle","label","name","role","budget":{list,profile},"budget_custom","paused","is_main","status":"running|online|cooldown|needs_login|challenge|offline|paused","online","healthy","cooldown_until","rate","last_limit","last_error","today":{list,profile},"hour":{pages,people},"activity","text","job","lists","version","first_seen","last_seen"}`. `/api/scraper` carries the same `accounts`, `alerts`, `rate`.
- `POST /api/accounts/{lane}` `{"role"?,"paused"?,"is_main"?,"label"?:str≤40|null,"budget"?:{list?,profile?}|null}` → `{"ok","account"}` (404 unknown lane).
- `POST /api/accounts/{lane}/remove` → `{"ok","removed":0|1}` — its leases and lists go back to the queue.
- `POST /api/settings/accounts` `{"main_list_share":0-1}` — share of the last hour's list pages the main account may take (0 = bios only).
- `GET /api/setup` → `{"repo","extension_path","extension_id","extension_version","server","lanes"}` for the add-account wizard.
- `GET /img/{id}` → cached profile picture (downloaded once from the Instagram CDN into `data/pfp/`; https + `*.cdninstagram.com`/`*.fbcdn.net` only, redirects never followed), 404 if none → UI shows initials.

## Rules

- Never loosen Instagram pacing to go faster. One request at a time per account. Limits → cooldown, escalating, auto-resume bounded.
- Short product language in the UI, no emoji decoration, no gradients, no filler. Real data only.
- Old system stays untouched as backup: `~/ig-follower-export`, `~/Documents/Codex/2026-09-23/here-s-the-full-prompt-with-2/work/`.

## Owner judgement in qualification (2026-09)
Setting a status or note (`/mark`, bulk) or a manual tag bumps `people.updated_at`, so the next qualify batch re-derives that
person. The LLM packet carries `OWNER'S OWN JUDGEMENT` (status) / `OWNER'S OWN NOTE` / hand-set tags lines, and
`input_hash` includes status + note + manual tags when any is set (hashes of untouched people are unchanged), so a changed
judgement re-runs the model for that person.
Selected examples also carry a bounded note (200 characters) and up to six manual tags into future bulk LLM prompts,
labelled as Michael's preferences rather than proof about another profile. Automatic tags are excluded. A note or tag
edit changes future prompt versions without re-running every prior verdict. The example tracks the person ID, so a
transferred handle cannot inherit the old owner's mark. `@fortun8te` is treated as Michael's account even if its seed
row is missing; it is excluded from Laya, bulk LLM and Leadscout candidates. Leadscout/Hermes still receives only public
profile fields for its independent evidence check; owner notes and tags are not included in its prompt. These examples
guide later prompts; they are not model retraining or independent validation of recommendation quality.

## Evidence-based pair comparison

See [CONNECTIONS.md](CONNECTIONS.md) for `/api/connections`, the additive observation ledger, input validation and limitations. The existing overview `/api/map` remains compatible. Follow links and bio mentions no longer generate an automatic personal-relationship claim.
