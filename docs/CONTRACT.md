# Fortunate Leads — build contract

One local tool: a Chrome extension collects Instagram lists and bios at a safe pace, a small Python server stores and qualifies them, a one-page UI shows who is worth contacting and why.

Owner: Michael (@fortun8te). Agency: Fortunate — static ad creatives and product visuals for physical-product (DTC/e-commerce) brands, mostly US; Dutch operators selling to the US also fit. Min engagement ~EUR 2,000.

## Layout (repo root `~/fortunate-leads`)

```
extension/   MV3 extension (plain JS, no build step)
server/      Python 3 stdlib only: server.py, db.py, qualify.py, migrate.py, tests/
web/         index.html, app.js, app.css, vendor/ (vendored libs, no CDN at runtime)
data/        leads.sqlite, pfp/ (gitignored)
docs/        CONTRACT.md, RESEARCH.md
```

Server: `python3 server/server.py` → serves `http://127.0.0.1:8777` (UI at `/`, static from `web/`). DB path `data/leads.sqlite` (override `--db`). Bind 127.0.0.1 only.

Write endpoints under `/api/ext/*` accept requests only when `Origin` is `chrome-extension://fgdbghllamedgihmdcolaggnbhnakjnf` (the manifest `key` keeps that id). UI endpoints accept only same-origin (Host 127.0.0.1:8777 / localhost:8777). No tokens, no pairing.

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
tags(person_id INT, tag TEXT, grp TEXT, source TEXT CHECK(source IN('auto','manual')), PRIMARY KEY(person_id,tag))
verdicts(person_id INT PRIMARY KEY, prefilter INT, score INT, tier TEXT, role TEXT, reason TEXT,
  model TEXT, input_hash TEXT, updated_at TEXT)   -- tier: hot|warm|cold|unread
marks(person_id INT PRIMARY KEY, status TEXT, note TEXT, updated_at TEXT)  -- good|maybe|no|contacted|client|known
jobs(id INTEGER PRIMARY KEY, kind TEXT CHECK(kind IN('list','profile')), seed TEXT, direction TEXT, handle TEXT,
  priority INT DEFAULT 0, state TEXT DEFAULT 'queued', attempts INT DEFAULT 0, leased_until TEXT, created_at TEXT)
settings(key TEXT PRIMARY KEY, value TEXT)   -- json values; includes paused, budgets, ext heartbeat, qualify, qualify_auto
pages(job_id INT, cursor TEXT, at TEXT, PRIMARY KEY(job_id,cursor))   -- list pages ingested (idempotency + soak rate)
```

## Qualifier interface (`server/qualify.py`, pure functions + one LLM call)

```python
TAG_GROUPS = ('role','niche','signal','size','source')   # UI colours by group
def prefilter(person: dict, seeds: list[str]) -> int          # 0-100, no bio needed; decides who gets a profile read
def rule_tags(person: dict, edges: list[dict], me: str|None) -> list[tuple[str,str]]   # [(tag, grp)]
def rule_verdict(person: dict, tags) -> dict                   # {'score','role','reason','tier'} with no model
def llm_verdict(person: dict, tags, edges) -> dict | None      # same keys + 'model'; None if model unavailable
def input_hash(person: dict, edges) -> str                     # cache key; unchanged hash = skip re-run
```
`person` dict = the `people` row. `edges` = list of `{seed, direction}`. Tiers: hot ≥70, warm 45–69, cold <45, `unread` when there is no bio yet (private or not read). `source` group tags are generated from edges: `via @seed`, `follows @seed`, `followed by @seed`, `in N lists` (N≥2), `knows you` when `me` is linked. LLM goes through the local OpenRouter proxy `http://127.0.0.1:18741/api/v1/chat/completions` (free models only), timeout-safe; server falls back to `rule_verdict`.

## Extension ↔ server

Extension is a paced executor. Server owns the queue; extension owns pacing, budgets and cooldowns.

- `GET  /api/ext/next` → `{"paused":bool, "job": null | {"id","kind":"list","seed","ig_id"|null,"direction","cursor"|null} | {"id","kind":"profile","handle","ig_id"|null}}`. Lists before profiles. Leases a job for 10 min.
- `POST /api/ext/list-page` `{"job_id","seed","ig_id","direction","users":[{"ig_id","handle","name","pic_url","is_private","is_verified"}],"next_cursor":str|null,"done":bool,"total":int|null}`
- `POST /api/ext/profile` `{"job_id":int|null,"profile":{"ig_id","handle","name","bio","website","category","followers","following","posts","is_private","is_verified","is_business","pic_url"}}` — `job_id:null` = passively captured while Michael browsed Instagram (free bio, no extra request).
- `POST /api/ext/error` `{"job_id","code":"rate_limit|soft_block|challenge|login|private|not_found|other","retry_at":iso|null,"message"}` — private/not_found finish the job; others release it.
- `POST /api/ext/heartbeat` `{"version","state":"running|idle|paused|cooldown","cooldown_until":iso|null,"today":{"list":n,"profile":n},"budget":{"list":n,"profile":n},"last_error":str|null,"rate"?:{"pages_hour":num,"people_hour":num,"last_hit_at":iso|null}}` every ≤30 s. `rate` is optional; bad values are stored as null.

All return `{"ok":true}` or `{"ok":false,"error":...}`. Server is idempotent: re-sent pages must not duplicate edges.

## UI API

- `GET /api/leads?tier=hot,warm&tags=a,b&status=&q=&sort=score|recent|followers|connected&min_lists=N&offset=0&limit=50` → `{"total", "rows":[{"id","handle","name","pic","bio","website","followers","following","tier","score","role","reason","tags":[{"tag","grp","source"}],"via":["seed",...],"lists":int,"status"}]}` — `pic` = `/img/{id}` or null. `lists` = distinct seeds the person is linked to by any edge (both directions count once). `sort=connected` = `lists` desc, then followers desc. `min_lists=N` keeps `lists ≥ N`.
- `GET /api/tags` → `[{"tag","grp","count","source":"auto"|"manual"}]` — one entry per (tag, source); a tag used both ways appears twice.
- `GET /api/counts` → `{"hot","warm","cold","unread","good","maybe","contacted","total","with_bio"}`
- `GET /api/person/{id}` → lead row + `{"edges":[{"seed","direction"}],"verdict":{...},"note"}`
- `POST /api/person/{id}/mark` `{"status":null|"good"|"maybe"|"no"|"contacted"|"client"|"known","note"?}`
- `POST /api/person/{id}/tags` `{"add":["..."],"remove":["..."]}` (manual tags)
- `POST /api/person/{id}/read` → queue a profile read now (priority)
- `GET /api/map?scope=leads|all&limit=400` → `{"nodes":[{"id":"s:handle"|"p:123","kind":"seed"|"lead","label","tier","score","pic","degree"}],"links":[{"source","target","direction"}],"rev":int}` — Lead `degree` = the same distinct-seed count as `lists`. `scope=leads`: seeds + top leads by score plus everyone linked to ≥2 seeds.
- `GET /api/scraper` → `{"ext":{"online","version","state","cooldown_until","today","budget","last_seen","activity","text","rate":{"pages_hour","people_hour","last_hit_at"}|null,"last_error"},"paused","qualify":bool,"qualify_auto":bool,"soak":{"1h":{"pages","people","new_people","profiles"},"6h":{...}},"people_today","lists":[{"seed","direction","state","received","total","updated_at","error"}],"queue":{"list":n,"profile":n}}` — `soak`: list pages ingested (retries not counted), edges added, people first seen, bios read in the window.
- `POST /api/scraper/seeds` `{"handles":["a","b"],"directions":["followers","following"]}`
- `POST /api/scraper/pause` `{"paused":bool}`
- `POST /api/scraper/budget` `{"list":n,"profile":n}`
- `POST /api/settings/qualify` `{"on":bool,"auto"?:bool}` → `{"ok":true,"qualify":bool}` — `on` starts/stops the bios planner and LLM verdicts (rule verdicts always run). `qualify_auto` (default true) switches qualification on by itself once lists exist and none is queued/running.
- `GET /img/{id}` → cached profile picture (downloaded once from the Instagram CDN into `data/pfp/`), 404 if none → UI shows initials.

## Rules

- Never loosen Instagram pacing to go faster. One request lane. Limits → cooldown, escalating, auto-resume bounded.
- No explanatory paragraphs in the UI, no emoji decoration, no gradients, no "AI-generated" fluff. Real data only.
- Old system stays untouched as backup: `~/ig-follower-export`, `~/Documents/Codex/2026-09-23/here-s-the-full-prompt-with-2/work/`.
