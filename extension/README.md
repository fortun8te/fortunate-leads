# Fortunate Leads extension (3.3.0)
Install: chrome://extensions → Developer mode → remove the old "Follower export" → Load unpacked → this folder (same extension id).
Needs a logged-in Instagram session and the server on http://127.0.0.1:8777. It uses an open instagram.com tab; if none is left while there is work it reopens one as a pinned background tab (at most once per 10 min), and it wakes a discarded/frozen Instagram tab by reloading it (at most once per 3 min, never the tab you are looking at). The tab it uses is marked not auto-discardable.
What it does: asks the server for one job at a time (list page or profile read), runs it inside the Instagram tab, posts the result. One request lane; a stored lane marker stops a restarted worker from firing while an earlier request may still be running.
Lists: cursor saved after every page (server + local progress), followers pages ~25, following 50. `should_limit_list_of_followers` = list done and marked limited. Empty pages (users:[] while more is promised, or page 1 empty while the list has people) = soft block; the same empty page again after the cooldown = error for that list only.
Bios: /api/v1/users/{pk}/info/ from the tab. No pk: pk cache from passive capture, else a profile page load (never web_profile_info). If /info/ refuses web clients it switches to page loads for 6 h.
Passive: bios Instagram already loads while you browse are sent too (zero extra requests, once per handle per 6 h).
Pacing: list pages 7–12 s apart, a 90–180 s break every 40–60 pages; profile reads 35–70 s apart. Unknown answers back off 2→30 min, tab/network trouble 30 s→10 min.
Budgets per day (reset at local midnight): 2000 list pages, profile reads unlimited by default (server-adjustable; 0 = no daily number, bios are paced by the 35-70 s gap and cooldowns).
Limits: lists and bios have separate cooldowns. A hit cools that bucket 10 min, doubling per hit in 24 h (cap 24 h, Retry-After honoured), 3 hits = that bucket stops until midnight; every hit pauses both 5 min; 3 hits within an hour across both = everything stops until midnight.
Security check or logged out → paused with "!" badge until you press Resume in the popup (or Pause→Resume in the workspace).
Results wait in a local outbox until the server accepts them (survives restarts). Popup: state, last hour, per-bucket status, last error with the raw Instagram answer, "Copy debug" (state + samples + recent events as JSON).
Tests: node --test extension/test/*.test.mjs
