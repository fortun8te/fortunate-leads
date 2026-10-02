# Getting more collection accounts

Researched 2 October 2026. Sources are listed at the end; vendor blogs sell scraping services, so treat their numbers as marketing, not measurements.

## What we know

- Every account that scrapes can be restricted. @dihfluencer was warned on 1 October. No pace guarantees safety.
- Instagram links risk to the login, the Chrome profile and the network. Accounts sharing one home IP share some of that risk. We do not use proxies or Tor (project rule), so this cannot be removed, only kept small.
- The extension reuses a real, logged-in Chrome session. Agent Reach (the repo Michael mentioned) does the same for Instagram, so it adds no safety and no new route. Its own docs warn about bans and recommend a secondary account.

## Safest ways to get accounts

1. **Real people's accounts, with consent.** Teammates, friends and clients who already use Instagram and agree to lend a Chrome profile. Aged accounts with a normal history get restricted far less than new ones.
2. **Your own extra accounts, aged.** Create them by hand, one phone number each, and use them like a person for 1 to 2 weeks (follow, like, post a story) before any collection.
3. **One Chrome profile per account.** Never log two accounts into one profile. Add the profile in `data/browser-startup.json`.

## What not to do

- Buying accounts, account farms and bulk-created accounts. They are the first to be restricted, and the sellers often keep the login.
- Running more accounts to go faster than the pace limits. More accounts add volume; the per-account pace stays the same.
- Using the main account.

## Ramp

1. One account for a week with no warning.
2. Three accounts for a week.
3. Six, then ten to twelve for 200k list entries a day.

The first warning on any account pauses the workspace. Michael reviews it himself.

## Sources

- [Agent Reach](https://github.com/Panniantong/Agent-Reach)
- [How to Scrape Instagram in 2026, Scrapfly](https://scrapfly.io/blog/posts/how-to-scrape-instagram)
- [How to Scrape Instagram in 2026, Olostep](https://www.olostep.com/blog/how-to-scrape-instagram)
