# Fortunate Leads extension (3.0.0)
Install: chrome://extensions → Developer mode → remove the old "Follower export" → Load unpacked → this folder (same extension id).
Needs one open instagram.com tab (logged in) and the server on http://127.0.0.1:8766. It never opens Instagram tabs itself.
What it does: asks the server for one job at a time (list page or profile read), runs it inside your Instagram tab, posts the result.
Passive: bios Instagram already loads while you browse are sent too (zero extra requests, once per handle per 6 h).
Pacing: list pages 7–14 s apart, a 3–6 min break every 20–30 pages; profile reads 35–70 s apart. One request lane.
Budgets per day (reset at local midnight): 500 list pages, 150 profile reads (server-adjustable, profile hard cap 300).
Limits: rate limit/soft block → cooldown 30 min doubling per hit in 24 h (cap 24 h, Retry-After honoured); 3 hits → stop until tomorrow.
Security check or logged out → paused with "!" badge until you press Resume in the popup (or Pause→Resume in the workspace).
Results wait in a local outbox until the server accepts them. Tests: node --test extension/test/*.test.mjs
