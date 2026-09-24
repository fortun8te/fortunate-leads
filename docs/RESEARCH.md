# IG follower/bio collection: research (2026-09-24)

Confidence: H = read in source code or maintainer-measured, M = several user reports, L = vendor blog / single comment.

## 1. Follower / following lists
- **Endpoint (current):** `GET https://www.instagram.com/api/v1/friendships/{user_id}/followers/?count=N&max_id=CURSOR` (same with `/following/`). Header: `X-IG-App-ID: 936619743392459`, `credentials: same-origin`. Paginate on `next_max_id`; also check `has_more` (sometimes missing at the tail). **H**: [davidarroyo1234/InstagramUnfollowers](https://github.com/davidarroyo1234/InstagramUnfollowers) `src/utils/utils.ts`, MIT, 4.9k stars, commit 2026-09-09.
- **Old GraphQL `query_hash` for edges is dead:** returns 200, correct count, **zero edges, has_next_page:false**. Treat "count>0 but 0 users" as a block. **H**, same commit.
- **Page sizes:** following honours `count` of about 50. Followers are **server-capped at ~15-25 per page, whatever `count` you send** (that repo's safety caps: 60 pages for following, 250 for followers). **M-H**. instagrapi asks for `count=200` plus `search_surface=follow_list_page` and `rank_token` (mobile API). **H**, [instagrapi user.py](https://github.com/subzeroid/instagrapi/blob/master/instagrapi/mixins/user.py).
- **Capped lists:** some accounts (seen on verified ones, from 156K to 686M followers) return `should_limit_list_of_followers: true`, `has_more:false`, no cursor, ~49 users. Nothing gets past this, so record it and move on. **H**: maintainer measured it live, [instagrapi #2811](https://github.com/subzeroid/instagrapi/issues/2811) (2026-09-19). They later said the badge alone may not be the cause. Following lists are not capped. **M**, [Apify steadyfetch](https://apify.com/steadyfetch/instagram-followers-scraper).

## 2. Bio / external_url / category / counts in one call
- `GET /api/v1/users/web_profile_info/?username=X`: has `biography`, `external_url`, `category_name`, `edge_followed_by`. **Since about Aug 2026 it returns 429 on the first request** for scripts, even with cookies or on another network. One commenter says it is "effectively retired"; another fixed it with a browser TLS fingerprint (curl_cffi). **M**, [instaloader #2726](https://github.com/instaloader/instaloader/issues/2726) (open). Inside a real Chrome tab the TLS is genuine, so it may still work. Test it, but don't rely on it.
- `GET /api/v1/users/{pk}/info/`: full user object (bio, external_url, category, follower/following counts, is_business). instagrapi's `user_info_v1`. **H** for the fields; **M** that it works from web with the app-id plus CSRF.
- GraphQL `POST https://www.instagram.com/graphql/query/` with `doc_id=28036671149327607` (PolarisProfilePageContentQuery, variables `{id, render_surface:"PROFILE", enable_integrity_filters:true, ...}`). This is the page's own query, so it is the **least unusual** option. doc_ids rotate every 2-4 weeks. **H** for the id today; **L-M** for the rotation ([Scrapfly, 2026-09-23](https://scrapfly.io/blog/posts/how-to-scrape-instagram)).
- **Headers:** `X-IG-App-ID: 936619743392459`, `X-CSRFToken` (from the `csrftoken` cookie; needed on POST), `X-ASBD-ID: 129477` (instagrapi constant), `X-IG-WWW-Claim` (echo the `x-ig-set-www-claim` response header, `0` at first), `X-Requested-With: XMLHttpRequest`. GraphQL POST also sends `lsd`/`fb_dtsg` from page HTML. **H**.

## 3. Observed limits
- instaloader's built-in throttle: **75 req / 11 min** for www `api/v1` ("other"), **200 / 11 min** for each GraphQL type, **275 / 10 min** for all GraphQL, **199 / 30 min** for the mobile API. **H**, [instaloadercontext.py](https://github.com/instaloader/instaloader/blob/master/instaloader/instaloadercontext.py). Treat these as ceilings, not targets.
- Follower/following enumeration: "please wait a few minutes" after **a few hundred calls in a short window**. Profile calls last **about 10x longer** before the same wall. **Separate buckets per call type.** Usually **HTTP 200 with an error body**, sometimes 429 or 401. Cooldown **5-15 min**, can run to hours. **3+ hits in an hour = escalate.** **M**, [instagrapi guide](https://instagrapi.com/guides/errors/please-wait-a-few-minutes/) (2026-05-01); [instaloader #2532](https://github.com/instaloader/instaloader/issues/2532) (401 "Please wait", 2025).
- Vendor figures: about 200 req/h per IP logged out; about 4,800 profiles/day per residential IP; 500-1,000 followers/h per account. **L**.
- **Soft-block signs:** 200 with `users: []` while the count is above 0; `should_limit_list_of_followers`; `require_login: true`; `message: "Please wait a few minutes..."`; `feedback_required`; a `checkpoint_required`/challenge redirect.

## 4. Practice (synthesis)
- One tab, one request at a time, no parallel tabs. Keep lists and bios in separate queues and budgets.
- Default pacing (InstagramUnfollowers): 1 s between pages, **10 s after every 5**. For a followers crawl, use **3-8 s with jitter** plus a **2-5 min break every 150-200 requests**.
- Bios: 1 every **20-45 s**, jittered. Suggested cap about **150-300 per day**, spread across the waking hours of the account's own timezone.
- On any soft block: stop that bucket, wait **at least 10 min** and double each time (10 → 20 → 40 min, max 4 h). After 3 hits, stop for the day. Never retry in a tight loop.
- Save the cursor (`next_max_id`) after every page so the crawl resumes and never starts over.

## 5. UI/graph ideas to borrow (MIT)
- [InstagramUnfollowers](https://github.com/davidarroyo1234/InstagramUnfollowers): filter chips, whitelist, configurable timings panel, partial-scan recovery.
- [sigma.js](https://github.com/jacomyal/sigma.js) (12k★) and [force-graph](https://github.com/vasturiano/force-graph) (2k★) for network views. [cytoscape.js](https://github.com/cytoscape/cytoscape.js) (11k★) for layouts and filtering.
- [twenty](https://github.com/twentyhq/twenty) (57k★) as a CRM table/kanban reference. **It is AGPL-style (NOASSERTION), so borrow ideas only, not code.**
