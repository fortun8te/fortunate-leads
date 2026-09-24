# Fortunate Leads — build contract

One local tool: a Chrome extension collects Instagram lists and bios at a safe pace, a small Python server stores and qualifies them, a one-page UI shows who is worth contacting and why.

Owner: Michael (@fortun8te). Agency: Fortunate — static ad creatives and product visuals for physical-product (DTC/e-commerce) brands, mostly US; Dutch operators selling to the US also fit. Min engagement ~EUR 2,000.

## Layout (repo root `~/fortunate-leads`)

```
extension/   MV3 extension (plain JS, no build step)
server/      Python 3 stdlib only (3.9+, SQLite 3.35+): server.py, db.py, qualify.py, rules.py, migrate.py, tests/
web/         index.html, app.js, app.css, vendor/ (vendored libs, no CDN at runtime)
data/        leads.sqlite, pfp/ (gitignored)
docs/        CONTRACT.md, RESEARCH.md
```

Server: `python3 server/server.py` → serves `http://127.0.0.1:8777` (UI at `/`, static from `web/`). DB path `data/leads.sqlite` (override `--db`). Bind 127.0.0.1 only.

Write endpoints under `/api/ext/*` accept requests only when `Origin` is `chrome-extension://fgdbghllamedgihmdcolaggnbhnakjnf` (the manifest `key` keeps that id). UI endpoints accept only same-origin (Host 127.0.0.1:8777 / localhost:8777); **every UI `POST` must carry `Origin: http://127.0.0.1:8777` (or localhost)** — browsers send it, `curl` needs `-H 'Origin: http://127.0.0.1:8777'`. GETs need no Origin. No tokens, no pairing.
Every response carries `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`.

## Data model (SQLite, WAL)

```sql
people(id INTEGER PRIMARY KEY, ig_id TEXT UNIQUE, handle TEXT UNIQUE NOT NULL COLLATE NOCASE,
  name TEXT, pic_url TEXT, pic_file TEXT, is_private INT, is_verified INT,
  bio TEXT, website TEXT, category TEXT, followers INT, following INT, posts INT, is_business INT,
  bio_at TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL)
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
  model TEXT, input_hash TEXT, updated_at TEXT)   -- tier: hot|warm|cold|unread
marks(person_id INT PRIMARY KEY, status TEXT, note TEXT, updated_at TEXT)  -- good|maybe|no|contacted|client|known
jobs(id INTEGER PRIMARY KEY, kind TEXT CHECK(kind IN('list','profile')), seed TEXT, direction TEXT, handle TEXT,
  priority INT DEFAULT 0, state TEXT DEFAULT 'queued', attempts INT DEFAULT 0, leased_until TEXT, created_at TEXT)
  -- state queued|leased|done|error. attempts = leases that expired or ended in an 'other' error (rate_limit/soft_block/
  -- login/challenge give the lease back). 5 'other' errors park a job ('error'); a profile job whose lease expired 5 times
  -- is parked too. List jobs reset attempts on every page.
settings(key TEXT PRIMARY KEY, value TEXT)   -- json values; includes paused, budgets, ext heartbeat, qualify, qualify_auto
pages(job_id INT, cursor TEXT, at TEXT, PRIMARY KEY(job_id,cursor))   -- list pages ingested (idempotency + soak rate)
```

## Qualifier interface (`server/qualify.py`, pure functions + one LLM call)

```python
TAG_GROUPS = ('role','niche','signal','size','source')   # UI groups tags by this (monochrome)
TAGS_VERSION = 't2'   # bump when rule tags change: on start the server lets the qualify batch re-derive all auto tags once
def prefilter(person: dict, seeds: list[str]) -> int          # 0-100, no bio needed; decides who gets a profile read
def rule_tags(person: dict, edges: list[dict], me: str|None) -> list[tuple[str,str]]   # [(tag, grp)]
def rule_verdict(person: dict, tags) -> dict                   # {'score','role','reason','tier'} with no model
def llm_verdict(person: dict, tags, edges) -> dict | None      # same keys + 'model'; None if model unavailable
def input_hash(person: dict, edges) -> str                     # cache key; unchanged hash = skip re-run
```
Auto tag vocabulary (all `grp` fixed by `qualify.TAXONOMY`):
- role: Brand, Store, Agency, Freelancer, Creative (photographer/UGC/3D/video), Creator, Supplier, SaaS, Coach, Personal
- niche: Skincare, Beauty, Supplements, Apparel, Jewelry, Home, Pets, Coffee, Food & Drink, Fitness, Wellness, Baby, Accessories, Outdoor, Tech Gadgets
- signal: Founder, Scaling, Hiring, Ecom (DTC/e-commerce/Shopify words), Shopify, Shop Link, Link Hub, Email, US, NL, UK, Verified, Business
- size: <1k, 1k-10k, 10k-100k, 100k-1M, 1M+ · source: via @seed, in N lists, knows you, follows you, you follow
Precision over recall: promo codes for someone else's brand ("code X at @brand") count as Creator, not Brand; "dog owner", "CEO of my life",
"of course", "available on Spotify", "model agency", "mama to baby", affiliate storefronts (shopmy/LTK) and look-alike domains
(restorehealth.com, theworkshop.com) no longer fire. Instagram's own category label is used only when unambiguous ("… (Brand)", "E-commerce website", "Jewelry/watches").

`person` dict = the `people` row. `edges` = list of `{seed, direction}`. Tiers: hot ≥70, warm 45–69, cold <45, `unread` when there is no bio yet (private or not read). `source` group tags are generated from edges: `via @seed`, `follows @seed`, `followed by @seed`, `in N lists` (N≥2), `knows you` when `me` is linked. LLM goes through the local OpenRouter proxy `http://127.0.0.1:18741/api/v1/chat/completions` (free models only). Per-model socket timeout 45 s and a 90 s budget per verdict across models; any transport error, non-JSON/non-object reply, missing content or model substitution = "unavailable" (`None`). The server then keeps the rule verdict and retries that person after 30 min; an exception inside `llm_verdict` is treated the same way.

## Extension ↔ server

Extension is a paced executor. Server owns the queue; extension owns pacing, budgets and cooldowns.

- `GET  /api/ext/next` → `{"paused":bool, "job": null | {"id","kind":"list","seed","ig_id"|null,"direction","cursor"|null} | {"id","kind":"profile","handle","ig_id"|null}}`. Lists before profiles. Leases a job for 10 min; an expired lease is handed out again (same job id, list cursor = last saved page).
  Profile planner (qualification on): people without a bio, not private, not a seed, not marked `no`, ordered by **lists count desc, then prefilter desc**; job priority = `min(lists,9)*1000 + prefilter` (a UI "read now" uses 10000).
- `POST /api/ext/list-page` `{"job_id","seed","ig_id","direction","users":[{"ig_id","handle","name","pic_url","is_private","is_verified"}],"next_cursor":str|null,"done":bool,"total":int|null}`
- `POST /api/ext/profile` `{"job_id":int|null,"profile":{"ig_id","handle","name","bio","website","category","followers","following","posts","is_private","is_verified","is_business","pic_url"}}` — `job_id:null` = passively captured while Michael browsed Instagram (free bio, no extra request). `website` is stored only if it matches `^https?://`; counts are coerced to int ≥ 0 or null (`"1,234"` → 1234).
  A new `ig_id` arriving under a handle that belongs to a row with a different `ig_id` parks the old row as `handle~id` (it keeps its marks/edges/tags) and creates a new person.
- `POST /api/ext/error` `{"job_id","code":"rate_limit|soft_block|challenge|login|private|not_found|other","retry_at":iso|null,"message"}` — private/not_found finish the job; others release it. rate_limit/soft_block record a cooldown for display. challenge/login do **not** pause the server (the extension holds itself until Michael resumes it in the popup; the UI reads `ext.state`); the server `paused` flag is only set by `POST /api/scraper/pause`.
- `POST /api/ext/heartbeat` `{"version","state":"running|idle|paused|cooldown","cooldown_until":iso|null,"today":{"list":n,"profile":n},"budget":{"list":n,"profile":n},"last_error":str|null,"rate"?:{"pages_hour":num,"people_hour":num,"last_hit_at":iso|null}}` every ≤30 s. `rate` is optional; bad values are stored as null.

All return `{"ok":true}` or `{"ok":false,"error":...}`. Server is idempotent: re-sent pages must not duplicate edges (`pages(job_id, cursor)`; a re-sent page never moves the cursor back). Every list page and profile re-applies the tag rules to the people it touched.

## UI API

GET endpoints return the JSON directly; POST endpoints return `{"ok":true,...}`; errors are `{"ok":false,"error"}` with 400 (bad input) / 403 / 404 / 500.

### Shared lead filter (`/api/leads`, `/api/tags`, `/api/map`)
| param | meaning |
|---|---|
| `tags=a,b` | has **all** of these tags (any source) |
| `any=c,d` | has **at least one** |
| `not=e,f` | has **none** |
| `status=` | absent: everyone except `no`; `good,maybe`: any of these; `none`: unmarked (combinable: `status=good,none`); `all`: no status filter |
| `q=` | substring of handle, name or bio |
| `min_lists=N` | linked to ≥ N distinct seeds |
| `has_bio=1\|0` | bio read and non-empty / not |
| `seed=h` | linked to that seed (any direction); `seed=a,b` = linked to all of them |
| `followers_min=`, `followers_max=` | inclusive; people with unknown followers drop out when either is set |
| `tier=hot,warm` | legacy, still works |

Bad numbers / `has_bio` / `status` values → 400. Tag values are exact (case-sensitive) tag names, URL-encoded (`via%20%40seed`).

- `GET /api/leads?<filter>&sort=score|recent|followers|connected&offset=0&limit=50` → `{"total", "rows":[{"id","handle","name","pic","bio","website","followers","following","posts","tier","score","role","reason","tags":[{"tag","grp","source"}],"via":["seed",...],"lists":int,"status"}]}` — `tags` ordered manual first, then rule, then auto; inside a source role, niche, signal, size, source. `pic` = `/img/{id}` or null. `lists` = distinct seeds the person is linked to by any edge (both directions count once). `sort=connected` = `lists` desc, then followers desc. Michael's own account is never a row.
- `GET /api/tags?<filter>` → `[{"tag","grp","source":"auto"|"rule"|"manual","count","total"}]` — one entry per (tag, source); `count` = people in the current filtered set with it, `total` = overall. Sorted count desc, total desc, tag. Tags with `count: 0` are included.

### Tag management (manual tags)
- `POST /api/person/{id}/tags` `{"add":["..."],"remove":["..."]}` — add makes the tag manual (an auto/rule tag of the same name becomes manual); remove deletes the tag whatever its source (auto/rule tags come back when their rule still matches).
- `POST /api/tags/rename` `{"from","to"}` → `{"ok","renamed":n}` — manual tags only. If a person already has `to` (any source) the two merge into one manual `to`. Group: that of an existing `to`, else of `from`.
- `POST /api/tags/delete` `{"tag"}` → `{"ok","deleted":n}` — manual tags only; auto/rule tags of that name stay.
- `POST /api/people/bulk` `{"ids":[..≤5000], "add"?:[..], "remove"?:[..], "status"?:null|"good"|"maybe"|"no"|"contacted"|"client"|"known"}` → `{"ok","updated":n}` (n = ids that exist). `status` key absent = marks untouched; `null` = clear the status (notes kept).
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

### People, map, scraper
- `GET /api/counts` → `{"hot","warm","cold","unread","good","maybe","contacted","total","with_bio"}`
- `GET /api/person/{id}` → lead row + `{"edges":[{"seed","direction"}],"verdict":{...},"note"}`
- `POST /api/person/{id}/mark` `{"status"?:null|"good"|"maybe"|"no"|"contacted"|"client"|"known","note"?:str|null}` — an absent key is left as it is; `status:null` clears the status but keeps the note; `note:""`/`null` clears the note; the row goes when both are empty.
- `POST /api/person/{id}/read` → queue a profile read now (priority)
- `GET /api/map?<filter>&scope=leads|all&limit=400` → `{"nodes":[...],"links":[{"source":"s:seed","target","direction"}],"seed_links":[{"source":"s:a","target":"s:b","shared":n}],"rev":int}`
  - seed node: `{"id":"s:handle","kind":"seed","label","tier","score","pic","degree","followers","status","lists","tags","seeds","is_me"}` — `degree` = edges into that seed's lists; `followers`/`status`/`tags`/`lists`/`seeds` describe the seed's own person row (a seed can sit in other seeds' lists), empty/0/null if unknown. `is_me` = Michael's own account.
  - lead node: `{"id":"p:123","kind":"lead","label","handle","name","tier","score","reason","pic","degree","lists","status","followers","tags","seeds"}` — `degree` = `lists` = distinct seeds; `tags` = up to 4 tag names, manual first (same order as leads); `seeds` = seed handles it is linked to.
  - The filter applies to leads only; all seeds are always returned. `scope=leads`: up to 60 % people linked to ≥ 2 seeds (most connected first), the rest by score. `scope=all`: by score. Default excludes `status=no`, as leads.
  - `seed_links` = top 50 seed pairs by shared audience (distinct people linked to both, any direction), not filtered; recomputed when edges change, at most every 30 s while a list streams in.
- `GET /api/scraper` → `{"ext":{"online","version","state","cooldown_until","today","budget","last_seen","activity","text","rate":{"pages_hour","people_hour","last_hit_at"}|null,"last_error"},"paused","qualify":bool,"qualify_auto":bool,"soak":{"1h":{"pages","people","new_people","profiles"},"6h":{...}},"people_today","lists":[{"seed","direction","state","received","total","updated_at","error"}],"queue":{"list":n,"profile":n}}` — `soak`: list pages ingested (retries not counted), edges added, people first seen, bios read in the window.
- `POST /api/scraper/seeds` `{"handles":["a","b"],"directions":["followers","following"]}`
- `POST /api/scraper/pause` `{"paused":bool}`
- `POST /api/scraper/budget` `{"list":n,"profile":n}`
- `POST /api/settings/qualify` `{"on":bool,"auto"?:bool}` → `{"ok":true,"qualify":bool}` — `on` starts/stops the bios planner and LLM verdicts (rule verdicts always run). `qualify_auto` (default true) switches qualification on by itself once lists exist and none is queued/running.
- `GET /img/{id}` → cached profile picture (downloaded once from the Instagram CDN into `data/pfp/`; https + `*.cdninstagram.com`/`*.fbcdn.net` only, redirects never followed), 404 if none → UI shows initials.

## Rules

- Never loosen Instagram pacing to go faster. One request lane. Limits → cooldown, escalating, auto-resume bounded.
- No explanatory paragraphs in the UI, no emoji decoration, no gradients, no "AI-generated" fluff. Real data only.
- Old system stays untouched as backup: `~/ig-follower-export`, `~/Documents/Codex/2026-09-23/here-s-the-full-prompt-with-2/work/`.
