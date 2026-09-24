# Handoff — state on 2026-09-24

Read CONTRACT.md (API + schema) and RESEARCH.md (Instagram endpoints, limits) first.

## Run locally
- Server: `python3 server/server.py` → http://127.0.0.1:8777 (UI at `/`). DB `data/leads.sqlite` (gitignored; rebuild with `python3 server/migrate.py` from the old ledger on Michael's Mac).
- Extension: `extension/` loaded unpacked in Chrome (id fgdbghllamedgihmdcolaggnbhnakjnf; `~/ig-follower-export` is a symlink to it). Needs one logged-in instagram.com tab. Reload after edits from a page on 127.0.0.1:8777: `chrome.runtime.sendMessage('fgdbghllamedgihmdcolaggnbhnakjnf', {type:'RELOAD'})`. Bump `manifest.json` version so the heartbeat proves which build is live.
- Tests: `node --test extension/test/*.test.mjs` and `python3 -m unittest discover -s server/tests` (server needs Python 3.9+ and SQLite 3.35+).
- Scraping can only run on Michael's Mac (his Chrome session). Cloud work = code, tests, UI against `web/?mock=1`.

## Works (live-verified)
- Following lists: @tbmango following 681/2061 at ~50 people / ~10 s, no limits hit.
- Heartbeat, job queue, outbox, passive bio capture while browsing, pfp cache, monochrome UI with live status.
- Seed ig_id lookup via loading the profile page (web_profile_info 429s for scripts — never use it).

## Open, in order
1. **Followers lists fail on page 1**: `/api/v1/friendships/{id}/followers/?count=25` returns HTTP 200 but `classify()` in `extension/lib/core.js` returns `other` (no `users` array or non-JSON). Capture the raw body in the IG tab, fix parsing/params (maybe `search_surface=follow_list_page`, see RESEARCH.md).
2. Soak test: run 10–15 of the 48 queued accounts back to back; confirm hours without 429 at 7–12 s gaps. Checklist + numbers: `ops/soak.md` (`/api/scraper` → `soak`, `ext.rate`).
3. Extension: send `rate` {pages_hour, people_hour, last_hit_at} in the heartbeat (server accepts and exposes it; see CONTRACT). UI: use `lists`, `sort=connected`, `min_lists`, `/api/tags` facets (`count`/`total`/`source`), tag rules, saved views, bulk edit, map `seed_links` (all done server-side).
4. A Cursor session in the old project (`~/Documents/Codex/2026-09-23/...`) keeps restarting the old server on 8766 — close it. Then on the Mac: `ops/install-launchagent.sh` (kills :8766/:8777, installs `com.fortunate.leads`, log `~/Library/Logs/fortunate-leads.log`). Not yet run on the Mac.
5. Qualification is off by default. Bio reads have no daily cap (budget 0 = unlimited, paced by the gap). `POST /api/settings/qualify {"on":true}` starts the bios planner + LLM verdicts (fallback: rule verdict, retried after 30 min). With `qualify_auto` (default true) it switches on by itself once every queued list is done.

## Server changes 2026-09-24 (later)
- Fixed: profile posts could mark a *list* job done when given its id; a late `/api/ext/error` could requeue an already finished job; `get_setting` returned the shared default dict (budget edits mutated defaults); LLM step could starve behind 50 failing rows.
- `pages` table gained `at` (auto-migrated on start).

## Server changes 2026-09-24 (tags, rules, views, map v2) — see CONTRACT.md for exact shapes
- One shared filter for `/api/leads`, `/api/tags`, `/api/map`: `tags` (all), `any`, `not`, `status` (`none`/`all`/csv), `q`, `min_lists`, `has_bio`, `seed`, `followers_min/max`.
- `/api/tags` is faceted: `count` within the filter, `total` overall (covering index; ~90 ms narrow / ~350 ms unfiltered on 100k people, 600k tags).
- Tag management: `/api/tags/rename` (merges), `/api/tags/delete` (manual only), `/api/people/bulk` (≤ 5000 ids: add/remove tags, status).
- Tag rules (`server/rules.py`, source `rule`): keywords or `/regex/` on bio/name/handle/category/website/any; applied to everyone on create, re-synced on every list page / profile / qualify batch; `/api/tag-rules/preview` counts hits without saving. Regexes are checked on the parsed tree, probed for worst-case time, scans capped at 160 chars per field and 20 s per full scan; the scan never holds the write lock.
- Saved views (`saved_views` table): `/api/views`.
- Map: filter + `seed_links` (top 50 audience overlaps between seeds), lead nodes carry `lists`, `status`, `followers`, `tags` (≤ 4, manual first), `seeds`; seed nodes also `is_me`.
- DB migration on start: `tags.source` CHECK gains `'rule'` (table rebuilt in place, data kept); new tables `tag_rules`, `saved_views`; index `tags_tag` replaced by covering `tags_tag_src` + `tags_person_src`.
- Qualifier review (`qualify.TAGS_VERSION = 't2'`; on first start every person's auto tags are re-derived once, LLM verdicts kept): new niches Coffee and Accessories, many more niche words (tallow, electrolytes, swaddles, handbags...), Ecom signal, Shopify/marketplace/link-in-bio hosts, US state abbreviations and more cities, more NL cities, IG category labels used when unambiguous. Removed false positives: "of course" (Coach), "dog owner"/"CEO of my life" (Founder), "packaging" in brand bios (Supplier), "available on Spotify" and creator promo codes (Brand), "model agency" (Agency), "sleep"/"kids"/"baby" in personal bios (niches), look-alike shop domains, affiliate storefronts. 40+ realistic bios in `server/tests/test_qualify.py`.
- Queue safety: profile planner orders by lists count, then prefilter. Expired leases are re-leased; a profile whose lease expired 5 times is parked (`error`). Rate-limit/soft-block/login errors no longer count toward the 5 'other' errors that park a job.
- `/api/ext/error` challenge/login no longer set the global `paused` (the popup Resume could not clear it and scraping stayed stuck); the extension's own hold stops requests.
- LLM: 90 s budget per verdict across models, any bad reply or exception falls back to the rule verdict (retried after 30 min).
- Security audit: UI POSTs require a same-origin `Origin` (**ops/soak.md curl POSTs need `-H 'Origin: http://127.0.0.1:8777'`**); frame/nosniff headers on every response; profile websites only `http(s)://`, counts coerced to ints; clearing a status keeps the note; a new account taking an old handle no longer inherits its marks/edges; profile pictures never follow redirects.

## LLM setup (OpenRouter, several keys)
- Order: local proxy `127.0.0.1:18741` first, then OpenRouter direct with rotating keys (`server/llm.py`).
- Keys: `export OPENROUTER_API_KEYS=sk-or-v1-aaa,sk-or-v1-bbb` before `python3 server/server.py`, or create `data/openrouter.json`
  (gitignored): `{"keys": ["sk-or-v1-aaa", "sk-or-v1-bbb"], "models": ["z-ai/glm-5.2:free", "google/gemma-4-31b-it:free"], "daily_limit": 1000}`.
  `models` and `daily_limit` are optional (defaults: the free models in `llm.MODELS`, 1000 requests per key and model per UTC day).
  For a LaunchAgent put the env var in the plist, or use the JSON file. Restart the server after editing either (a 401 key stays disabled until then).
- Check: `curl -s 127.0.0.1:8777/api/llm` — providers, masked keys (`sk-…abcd`), cooldowns, requests today, last error, pool size.
- Pool size / threshold: `POST /api/settings/qualify {"on":true,"workers":4,"llm_min":40}` (UI POSTs need the Origin header).
- Laya sidecar (optional): `python3 sidecar/laya_server.py` on 127.0.0.1:18742; the server uses it when `/health` answers, skips it otherwise.

## Server changes 2026-09-24 (review + staged qualifier)
- Fixed: negative Content-Length hung a handler thread; non-object JSON bodies gave 500; a malformed heartbeat `cooldown_until`
  broke `/api/scraper` until the next heartbeat; a malformed `retry_at` left the job leased; `q=` treated `_`/`%` as wildcards;
  parked `handle~id` rows were planned as profile reads (wasted Instagram requests on a non-existent handle); a late list page
  after `done` flipped the list back to `running` forever (blocking qualify_auto); unknown person gave 400 instead of 404; huge
  budget numbers gave 500; non-string profile fields gave 500 (outbox retried them 10x).
- Bio reads: no hard cap; default profile budget 0 = no daily number (pacing = 35–70 s gap + cooldown ladder, one lane).
- Qualifier: network signals (seed direction, seed yield from marks, links to Michael, closeness to clients) in the prefilter and
  LLM packet; planner orders by likely fit; optional Laya soft signal; LLM rubric with evidence quotes, few-shot from marks,
  batched calls, bounded worker pool; `POST /api/scraper/snowball` (opt-in, UI button still to add); `GET /api/llm`.

## Rules
- Honour Instagram's own limits (429 / "please wait") with backoff; never retry through them.
- UI: monochrome, square, no score/reason/AI elements for now.
