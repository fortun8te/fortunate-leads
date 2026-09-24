# Handoff — state on 2026-09-24

Read CONTRACT.md (API + schema) and RESEARCH.md (Instagram endpoints, limits) first.

## Run locally
- Server: `python3 server/server.py` → http://127.0.0.1:8777 (UI at `/`). DB `data/leads.sqlite` (gitignored; rebuild with `python3 server/migrate.py` from the old ledger on Michael's Mac).
- Extension: `extension/` loaded unpacked in Chrome (id fgdbghllamedgihmdcolaggnbhnakjnf; `~/ig-follower-export` is a symlink to it). Needs one logged-in instagram.com tab. Reload after edits from a page on 127.0.0.1:8777: `chrome.runtime.sendMessage('fgdbghllamedgihmdcolaggnbhnakjnf', {type:'RELOAD'})`. Bump `manifest.json` version so the heartbeat proves which build is live.
- Tests: `node --test extension/test/*.test.mjs` and `python3 -m unittest discover -s server/tests`.
- Scraping can only run on Michael's Mac (his Chrome session). Cloud work = code, tests, UI against `web/?mock=1`.

## Works (live-verified)
- Following lists: @tbmango following 681/2061 at ~50 people / ~10 s, no limits hit.
- Heartbeat, job queue, outbox, passive bio capture while browsing, pfp cache, monochrome UI with live status.
- Seed ig_id lookup via loading the profile page (web_profile_info 429s for scripts — never use it).

## Open, in order
1. **Followers lists fail on page 1**: `/api/v1/friendships/{id}/followers/?count=25` returns HTTP 200 but `classify()` in `extension/lib/core.js` returns `other` (no `users` array or non-JSON). Capture the raw body in the IG tab, fix parsing/params (maybe `search_surface=follow_list_page`, see RESEARCH.md).
2. Soak test: run 10–15 of the 48 queued accounts back to back; confirm hours without 429 at 7–12 s gaps.
3. Server fields the UI wants: `/api/leads?sort=connected` (distinct seeds), `min_lists=N`, `source` on `/api/tags`, a speed/rate in the extension `view` for the popup.
4. A Cursor session in the old project (`~/Documents/Codex/2026-09-23/...`) keeps restarting the old server on 8766 — close it. Add a LaunchAgent for the new server.
5. Qualification is off (`settings.qualify=false`): bios planner + LLM verdicts stay off until lists are collected. Turn on by setting it true.

## Rules
- Honour Instagram's own limits (429 / "please wait") with backoff; never retry through them.
- UI: monochrome, square, no score/reason/AI elements for now.
