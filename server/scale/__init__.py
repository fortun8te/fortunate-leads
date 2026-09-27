"""Scale-out collection: per-account sticky egress, adaptive pacing, global dedupe and a
logged-out enrichment pool. Stdlib only, like the rest of the server.

Hard rules enforced in code (see docs/scaling-plan.md):
  * No module in this package opens a direct connection to Instagram. Every request goes
    through an Egress (SOCKS5 / HTTP proxy / explicit non-home source address), and the
    HomeGuard refuses any egress whose observed exit address is the home address.
  * Logged-in work (follower/following pages) only runs on an egress dedicated to that one
    account (sticky, never Tor, never shared with another account).
  * The main account is disabled unless explicitly enabled, and then hard-capped.
"""
