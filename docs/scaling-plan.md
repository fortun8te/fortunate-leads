# Scaling plan: from ~40k profiles/day toward 40k / 100k / 200k per hour

Status: design + first code (branch `feat/scale-scraping`, package `server/scale/`). Written 2026-09-27.
Hard rules, enforced in code, not just in this text:

1. **The home connection never carries scraping traffic.** No code path in `server/scale` can open a
   direct connection (there is no "direct" egress kind). Every non-Tor egress must pass the HomeGuard
   (observed exit IP ≠ home IPv4, ≠ home IPv6 /64) before it gets work; with no home address configured,
   non-Tor egress is refused (fail closed). For the Chrome profiles, `extension/lib/egress.js` routes
   only Meta hosts through the profile's own SOCKS5 egress with **no DIRECT fallback**.
2. **The main account is protected.** Off by default. It only runs when both `enabled` and
   `allow_main` are set; then: following lists only, ≤ 100 pages/day, 0.5 pages/min, and the first
   push-back holds it (48 h). Recommended: leave it off, and never let it share an IP with a bot.
3. **Free by default.** Paid options (residential/mobile proxy pools) are listed only as optional.

---

## 1. How follower/following collection works today (main @ d855a2d, after PR #59)

PR #59 (merged 2026-09-27 22:13 CEST) is map search / owner edits / K2 output. It does not touch
collection, so `main` is the base for this branch.

| Piece | Where | What it does |
|---|---|---|
| Collector | `extension/background.js`, `extension/lib/core.js` | One Chrome profile per Instagram account on the **Mac**, logged in. `runList` loads the seed's profile page once per run (id + count), then pages `GET www.instagram.com/api/v1/friendships/{id}/{followers,following}/?count=25` (followers, `search_surface=follow_list_page`, 50 in an experiment cohort) or `count=50` (following), cursor `max_id`, saved after every page. Bios: `GET /api/v1/users/{pk}/info/` from the tab. |
| Per-account pacing | `core.js` `PACE` | List pages 7–12 s apart, 90–180 s break every 40–60 pages; bios 35–70 s apart; any two requests ≥ 2–5 s; sliding window **72 requests / 11 min** (~390/h ceiling, lists + bios + lookups together); push-back → 10 min cooldown doubling to 6 h, 3 hits in 24 h → ≥ 2 h. Budgets 3,000 pages + 300 bios per account per day. |
| Queue / leasing | `server/accounts.py` `pick_job`, `/api/ext/next` | Server owns the queue; a list sticks to its lane, resumes from the saved cursor on another lane after logout/pause/cooldown. Following before followers. |
| **Global request gate** | `accounts.request_permit` | **One workspace-wide FIFO permit** for every Instagram request of every account, 2 s spacing, 90 s lease. An expired lease pauses lists and bios for everyone until the owner resumes. |
| **Shared cooldown** | `server.py` `ext_error`, `meta_network.blocked` | Any `rate_limit` / `soft_block` / `login` / `challenge` / followers home-redirect on **any** account sets one workspace `cooldown` (≥ 15 min, 30 min for a home redirect, longest wins). login/challenge also pause lists+bios for all until resumed. Followers home-redirects keep that account off followers 2–4 h (`follower_route_wait`). |
| Public bios (optional) | `server/biofetch.py` | Meta Graph `business_discovery`, needs a token; business/creator accounts only. |

### Where it rate-limits, in plain words

- **All accounts are one person to Instagram.** Every Chrome profile runs on the Mac, so every
  request (lists, bios, profile lookups) leaves from the **home IP**. That breaks rule 1, links the
  burners to the main account through the IP, and is exactly why the server serialises everything
  through one gate and one shared wait: with one IP, that is the correct safety call.
- **So 3 accounts ≈ 1 account.** One global permit plus one shared cooldown means a limit on any burner
  stops all of them for ≥ 15 min. Throughput does not scale with accounts at all.
- **The follower endpoint is the tightest bucket.** Enumeration trips "please wait" after "a few
  hundred calls in a short window", profile reads last ~10× longer (instagrapi guide). Web followers
  return ~15–25 users/page whatever `count` says (RESEARCH.md), so each page is expensive.
- **Bios through logged-in tabs are tiny:** 35–70 s apart and 300/day/account → ~900 bios/day with 3
  accounts. Every bio read also eats the same 72/11-min window the lists need.

## 2. Research: what works in 2026 (sources, confidence)

| Topic | Finding | Source |
|---|---|---|
| Per-account IP | "Keep one stable proxy/IP per account… Treat a shared IP pool as higher risk; reduce account count if you cannot dedicate an IP per account." Rotating per request is itself a flag. | [instagrapi best practices](https://github.com/subzeroid/instagrapi/blob/master/docs/usage-guide/best-practices.md), [instagrapi proxy_address_is_blocked](https://instagrapi.com/guides/errors/proxy-address-is-blocked) |
| Budgets are per call type | Follower/hashtag enumeration trips after "a few hundred calls in a short window"; profile-info runs "an order of magnitude longer". Usually HTTP 200 with an error body, lifts in 5–15 min, can be hours. | [instagrapi please_wait](https://instagrapi.com/guides/errors/please-wait-a-few-minutes) |
| Page sizes | Web followers ~15–25/page (count ignored); following ~50. Mobile API: instagrapi asks `count=200`; guide says Instagram serves "chunks of 50–100". Not measured here yet. | RESEARCH.md; [instagrapi followers guide](https://instagrapi.com/guides/get-instagram-followers-python) (2026-04-30) |
| Throttle ceilings | instaloader: 75 req/11 min www api/v1, 199/30 min mobile API. | [instaloadercontext.py](https://github.com/instaloader/instaloader/blob/master/instaloader/instaloadercontext.py) |
| Datacenter ranges | Instagram "pre-flags entire CIDR ranges" of AWS/GCP/Azure/Hetzner/OVH/DO; residential/mobile exits earn blocks individually. | [instagrapi guide](https://instagrapi.com/guides/errors/proxy-address-is-blocked) (vendor docs, M) |
| Logged-out per IP | ~200 req/h per IP logged-out (vendor figure, L). Our own Tor measurement is per circuit, not per IP (circuits rotate). | [Scrapfly 2026](https://scrapfly.io/blog/posts/how-to-scrape-instagram) |
| TLS fingerprint | `web_profile_info` 429s on the first request for scripts; browser TLS impersonation (curl_cffi) fixed it for users. **Confirmed live today, see §6.** | [instaloader #2726](https://github.com/instaloader/instaloader/issues/2726) |
| IPv6 | Serious rate limiters key IPv6 by /64 (one household = one /64), escalate to /56 and /48. Assume Instagram does at least /64. | [geoiphub](https://geoiphub.com/blog/rate-limit-and-block-ipv6-by-prefix), [UTwente thesis](https://essay.utwente.nl/fileshare/file/96014/van%20Heijningen_BA_EEMCS.pdf) |
| Oracle free VM IPv6 | IPv6 subnets are always /64; VCN gets a /56 (256 possible /64 subnets); up to 32 IPv6 objects per VNIC, CIDR objects for dense addressing. Datacenter ASN, so see "pre-flagged ranges" above: **untested for Instagram**. | [OCI IPv6 docs](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/ipv6.htm), [OCI VCN overview](https://docs.oracle.com/en-us/iaas/Content/Network/Tasks/Overview_of_VCNs_and_Subnets.htm) |
| Cloudflare Workers | Free plan 100k requests/day, 50 external subrequests/invocation; egress is Cloudflare's datacenter space, reported blocked/429 by Instagram. Not recommended. | [CF limits](https://developers.cloudflare.com/workers/platform/limits/) |
| Tor | ~1,400 exit relays (Sep 2026). `IsolateSOCKSAuth`: each SOCKS username gets its own circuit. | [Tor exit list 2026-09-04](https://jamesbrine.com.au/tor-exit-nodes-for-2026-09-04/), [Tor spec](https://spec.torproject.org/path-spec/stream-isolation.html) |
| Phones as egress | On-device SOCKS5 apps pin traffic to cellular (e.g. DataProxy, pivot-proxy-android); mobile CGNAT IPs have high trust. | [dataproxy](https://github.com/Sir-MmD/dataproxy), [pivot-proxy-android](https://github.com/dmatscheko/pivot-proxy-android) |
| Warm-up / account care | Reuse sessions, never re-login in loops, don't rotate IP mid-session or during a challenge, freeze accounts that keep getting anti-abuse responses. | instagrapi best practices (above) |

## 3. Target architecture

```
            seeds / snowball (existing server queue)
                         │
  TIER 1  DISCOVERY (logged-in, scarce)          one bot account ⇄ one sticky egress, 1:1, never Tor/home
     bot1 ── phone A (mobile data, Tailscale SOCKS5)      per-account Pacer (AIMD, window, budget, warm-up)
     bot2 ── friend box B (home broadband, Tailscale)     independent cooldowns; per-egress gate (gate.py)
     botN ── …                                            cursor saved every page; challenge = hold
                         │ usernames + pk (short user objects, no bio)
  TIER 2  GLOBAL DEDUPE  data/scale.sqlite (SeenStore)    new-only; priority by #lists seen in; TTL 30 d;
                         │                               people with a bio in leads.sqlite never refetched
  TIER 3  ENRICHMENT (logged-out, massively parallel)     one worker per egress unit, Pacer 10→15 req/min
     Tor: P processes × C circuits (IsolateSOCKSAuth)     push-back → new circuit at once; saturation → cool
     + optional IPv6 /64s on a free Oracle VM (bind)      fixed egress → escalating cooldown
     route: i.instagram.com web_profile_info, Android identity + Chrome TLS; HTML fallback
                         │
  TIER 4  SINK            leads.sqlite via FortunateDBSink (bio_src=public_android), rules re-applied,
                          qualification as today; metrics per account / egress / pipeline
```

Why this split: follower pages need a session (logged-out cannot list followers), sessions are the
scarce, fragile resource, and each page yields 20–100 usernames. Bios need no session at all, only
IPs, and IPs are cheap (Tor) where accounts are not. So accounts do **only** list pages, never bios,
and every bio moves to the logged-out tier. That alone frees the 72/11-min window for list pages and
removes the 300 bios/day/account ceiling.

## 4. Throughput model (`python3 server/scale/model.py`)

Unit: new unique profiles discovered **and** enriched per hour. "capacity with 3 accts" assumes each of
today's 3 burners has its own IP and the global gate is replaced (otherwise it is ~1 account).

| target/h | scenario | new/acct/h | accounts needed | enrich units | of which non-Tor | capacity/h with 3 accts (own IPs) | binding |
|---|---|---|---|---|---|---|---|
| 40,000 | conservative | 1,000 | 40 | 223 | 73 | 3,000 | bot accounts (list pagination) |
| 40,000 | central | 4,550 | 9 | 160 | 0 | 13,650 | bot accounts (list pagination) |
| 40,000 | optimistic | 21,000 | 2 | 100 | 0 | 63,000 | none at this target |
| 100,000 | conservative | 1,000 | 100 | 556 | 406 | 3,000 | bot accounts (list pagination) |
| 100,000 | central | 4,550 | 22 | 400 | 100 | 13,650 | bot accounts (list pagination) |
| 100,000 | optimistic | 21,000 | 5 | 250 | 0 | 63,000 | bot accounts (list pagination) |
| 200,000 | conservative | 1,000 | 200 | 1112 | 962 | 3,000 | bot accounts (list pagination) |
| 200,000 | central | 4,550 | 44 | 800 | 500 | 13,650 | bot accounts (list pagination) |
| 200,000 | optimistic | 21,000 | 10 | 500 | 0 | 63,000 | bot accounts (list pagination) |

Per tier (central numbers; conservative / optimistic in the model):

- **Tier 1, one bot account:** 200 pages/h × 35 users/page × 65 % new ≈ **4,550 new profiles/h**
  (conservative 1,000; optimistic 21,000 with the mobile API's larger pages). A 16–18 h day gives
  ~70–80k/day per account (central). These are **estimates to be replaced by soak data**
  (metrics `pipeline/lists users_new`, `account/*/pages`, push-backs).
- **Tier 3, one egress unit:** **~250 profiles/h per Tor circuit** (measured 2026-09-24: 36 circuits →
  ~152/min, near-linear from 24). 40k/h ≈ 160 circuits; 100k/h ≈ 400; 200k/h ≈ 800.
- **Binding constraint by level**
  - *Today:* shared IP + global gate: ~1 account's worth, and every hit stops everything.
  - *40k/h:* **bot accounts** (≈ 9 central, 2 optimistic, 40 conservative), each with its own IP.
    Enrichment is fine on Tor alone (≈ 160 circuits = 4–6 tor processes on one free VM).
  - *100k/h:* bot accounts (≈ 22 central) **and** enrichment starts to exceed what Tor gives
    usefully (≈ 400 units vs ~300 usable circuits, estimate) → add non-Tor units (IPv6 /64s if Oracle
    ranges work, spare phones, or an optional paid pool).
  - *200k/h:* ≈ 44 bot accounts (central) with 44 separate sticky IPs, and ≈ 800 enrichment units, of
    which ~500 non-Tor. This is the level where free sources run out; it needs either many more free
    /64s that actually pass Instagram, or the optional paid pool. It is possible, not free-and-easy.

The single biggest multiplier on the account side is page size: if the mobile API really serves
50–100 users/page to a bot session (vs ~20 on web followers), the account count for each target drops
~3×. Measure it on one burner first (see next steps).

## 5. Adding bot accounts safely

1. **Egress first.** Each new account gets its own sticky IP before it is ever logged in: a phone on
   mobile data (best: old Android + SIM, SOCKS5 app, Tailscale) or a consenting friend's home box
   running a SOCKS5 server bound to its Tailscale address. Never Tor, never a datacenter, never the
   home IP, never shared with another account. Add it to `data/scale.json` and run
   `python3 server/scale/cli.py check-egress --config data/scale.json` (exit ≠ home, else refused).
2. **Create and log in on that same IP** (the phone itself, or the Chrome profile routed through that
   egress). Complete the profile (photo, bio, a few posts/follows) like a person.
3. **Warm-up** (`pacing.warmup_budget`, practitioner guidance, not an Instagram rule): days 0–2 no
   list pages; then 20 → 50 → 100 → 200 → 400 → 800 → 1,500 pages/day at days 3/5/7/10/14/21/28,
   full budget from day 35. Any push-back halves the day's budget and restarts the ramp stage.
4. **Operate:** following lists first (smaller, less limited), the account's own list sticks to it,
   cursor saved each page. A challenge = hold until you clear it on that same device/IP. 3+ hits in 24 h
   → rest (Pacer strike rule). Freeze accounts that keep failing instead of pushing.
5. **Never** move an account between IPs casually, never log in to it from the home connection, never
   let the main account use a bot's IP (or vice versa).

## 6. Live checks done today (2026-09-27, own Tor on this box, logged-out only)

- `GET i.instagram.com/api/v1/users/web_profile_info/?username=…` with Android UA + app id
  567067343352427: **429 on first try with Python's stock TLS**, **200 with full profile when the TLS
  is Chrome's (curl_cffi impersonate="chrome")**. `www` + web app id: 401 "please wait" either way.
  GraphQL `PolarisProfilePageContentQuery` logged-out: 401 "please wait".
- Logged-out `i.instagram.com/api/v1/users/{pk}/info/`: 200 but only username/pk/picture (no bio).
- Many business profiles answer the Android route with 400 "Asset …ig_business_category_subvertical has
  been deleted" (all app versions tried); a few return `{"status":"ok"}` with no data. Fallback: the
  public profile HTML carries biography, pk and og counts; ~0.8 MB and often a login redirect per exit.
- Smoke test (not a benchmark): 158 brand handles, 2 tor processes × 12 circuits: 151 resolved (134
  enriched, 121 with a bio, 17 not found) in ~2.5 min. Android route: 201 requests; HTML fallback: 215
  requests for 67 profiles (~3.2/profile). Brand-heavy input makes the fallback look worse than it will
  be on ordinary follower lists; measure on real lists.
- Tor processes started for these tests were stopped afterwards.

## 7. What was built (this branch)

`server/scale/` (stdlib only; the enrichment worker optionally uses curl_cffi in its own venv):

| File | Purpose |
|---|---|
| `egress.py` | Egress kinds tor/socks5/http/bind (no direct), HomeGuard (IPv4 exact, IPv6 /64, fail closed), Registry with strict 1:1 account↔egress binding, account IPs never lent to enrichment. |
| `transport.py` | Stdlib HTTPS over SOCKS5 (remote DNS, auth for Tor isolation), HTTP CONNECT, or bound source address. No direct path. |
| `pacing.py` | Pacer: AIMD rate, sliding window, daily budget, escalating cooldowns, Retry-After, strike rule; presets for list pages, logged-out profiles, main account; warm-up ramp. |
| `signals.py` | Response classifier mirroring `core.js classify` (+ ip_blocked). |
| `dedupe.py` | SeenStore: global seen-cache in `data/scale.sqlite`, priority by lists seen in, leases, TTL, backoff/park, import of already-enriched people. |
| `enrich.py` | Logged-out EnrichmentPool: worker per egress, circuit swap on push-back, saturation cooldown, HTML fallback, JSONL and leads.sqlite sinks (honours the Bios pause). |
| `tor_pool.py` | N tor processes × M circuits (IsolateSOCKSAuth), own DataDirectories, bootstrap wait, stop. |
| `lists.py` | AccountScheduler: per-account egress + Pacer, independent cooldowns, challenge hold, main-account policy, warm-up, cursor-saving ListQueue, web/mobile request shapes, injected session client (no logins here). |
| `gate.py` | Per-egress replacement for the global request gate / shared cooldown (unrouted lanes keep today's behaviour). |
| `metrics.py` | Rolling per-account / per-egress / pipeline counters, JSON + Prometheus text. |
| `bridge.py` | Hooks for server.py: `on_list_page`, `import_known_from_leads`, `queue_unread_from_leads`. |
| `model.py`, `cli.py` | Throughput model; CLI (model, stats, queue, import-leads, enrich, check-egress). |

Also `extension/lib/egress.js` (PAC: Meta hosts → the profile's SOCKS5 egress, no DIRECT fallback;
home-IP check) with `extension/test/egress.test.mjs`, and `ops/scale.example.json` (no secrets).
Tests: `python3 -m unittest discover -s server/tests -p 'test_scale_*.py'` (55, mocked network) and
`node --test extension/test/egress.test.mjs` (3).

**Not changed yet (deliberately):** `server/accounts.py` / `server/server.py` gate and cooldown logic,
`extension/background.js` and the manifest. Those need the full test suite on the Mac; see §8.

## 8. Integration steps (next PRs)

1. **Enrichment now, zero account risk:** on a non-home machine (free Oracle VM, or this box):
   `python3 -m venv .venv-scale && .venv-scale/bin/pip install -r server/scale/requirements-worker.txt`,
   copy/sync leads.sqlite or run next to the server, then
   `.venv-scale/bin/python server/scale/cli.py import-leads --db data/leads.sqlite` and
   `.venv-scale/bin/python server/scale/cli.py enrich --db data/leads.sqlite --tor 4x40 --minutes 600`.
   Set every bot account's role to **Lists** in Accounts so the tabs spend their window on list pages
   only (note: a bio budget of 0 means "no daily number", not "none").
   (Running Tor on the Mac is possible too: Instagram then sees Tor exits, never the home IP, but the
   ISP sees Tor. A VM is cleaner.)
2. **Hook list pages into the seen-cache:** in the `/api/ext/list-page` handler call
   `scale.bridge.on_list_page(users, seed, direction)` after ingest (one line, wrapped in try/except).
3. **Egress per Chrome profile:** add `"proxy"` to the manifest permissions, `importScripts('lib/egress.js')`,
   and on start `chrome.proxy.settings.set(FLEgress.proxySettings(laneEgress))` with the lane's egress
   from the server (new `egress` field on the account row). Refuse to collect until the exit check
   passes (`FLEgress.isHome` against the configured home addresses).
4. **Replace the global gate for routed lanes:** in `accounts.request_permit` / `server.ext_error`,
   key the permit and the `cooldown` by `gate.group_of(lane, lane_egress)`; lanes without a verified
   egress stay in the shared `unrouted` group (today's behaviour). Only then do accounts scale linearly.
5. **Measure page sizes on one burner** with the mobile surface (`lists.mobile_list_url`,
   `MobileSessionClient`) using a session you create on that burner's own device/IP, and compare
   users/page and push-backs with the web route before moving more accounts over.

## 9. What Michael needs to provide

- **An egress per bot account** (the real unlock): old Android phones on mobile data with a SOCKS5 app
  + Tailscale, or friends' home connections via a small box + Tailscale. Needs your home IPv4/IPv6 in
  `data/scale.json` so the guard can refuse it.
- **More bot accounts** for 40k/h+: ≈ 9 (central estimate) with their own IPs, created and warmed up
  per §5; ≈ 22 for 100k/h; ≈ 44 for 200k/h.
- **A free Oracle Cloud VM** (Always Free ARM, up to 4 OCPU / 24 GB) to run the Tor enrichment pool
  24/7 away from home, and to test whether its IPv6 /64s pass Instagram logged-out (untested).
- A decision on the main account: recommended **off** for scraping entirely.
- Optional, paid, only if free sources run out at 100k–200k/h: a residential/mobile proxy pool for the
  enrichment tier. Flagged, not required for 40k/h.
