# Product

## Goal

Fortunate is Michael's studio. It makes ad creatives for physical-product (DTC) brands. This software
finds the founders and brands who could hire Fortunate, ranks them, and tells Michael who to message next.

It is a private, single-user tool that runs on one Mac. It sends no messages. Michael does the outreach.

## Who counts as a lead

- Founder, owner or decision-maker of a physical-product brand that sells online, or the brand's own account.
- Sells in the US, NL, UK, EU, Canada or Australia.
- Big enough to pay about EUR 2,000+ for ads (real shop, reviews, team, retail, press), but not a household
  name with an in-house agency. Tagged `Too big` when past about 300k followers.
- Reachable: a normal founder or small brand, not a celebrity or mega-creator.

Not leads: coaches, course sellers, agencies, freelancers, creators without a product brand, SaaS, crypto,
personal accounts, brands outside the markets above (`Other market`).

## How it works

1. Michael picks seed accounts: people and brands whose audience overlaps with good leads.
2. The Chrome extension, one per Instagram account, reads their follower and following lists and, more slowly,
   individual bios, at a conservative pace.
3. The local server stores everything and ranks people: 60% network strength (how many seeds link to them,
   links to Michael and his clients), 40% profile evidence (rules first, then free AI models with web research,
   then the `leadscout` agent for the final verdict on the top few).
4. Michael works the list: statuses (Interested, Contacted, Talking, Client, Not a fit), notes, follow-ups.

## What "done" looks like

- A new Mac goes from clone to collecting in three steps (`docs/SETUP.md`), and Accounts shows missing connection steps.
- Paste a handle, press Start, leads appear ranked, nothing else to configure.
- It looks and reads like software Apple would ship: short plain English, one accent, keyboard friendly, no jargon.
- Collection stops when Instagram warns or limits an account. The account matters more than speed; no collection rate guarantees freedom from restrictions.

## Principles

- **Safe pace over speed.** Never loosen pacing. Three 429s in an hour stop everything until midnight.
  A warning stops the workspace. Only explicit operator authorization may resume two already healthy, identity-bound accounts while the warned account remains blocked. New warnings or cooldowns stop the workspace again; never rotate identities or clear a warning automatically.
- **No evasion.** No proxies, Tor or block dodging. The old Tor collector is archived and deliberately unmerged.
- **Free by default.** No paid scraping services (Apify was rejected). AI uses `:free` OpenRouter models and local models.
- **Plain, monochrome UI.** Short product language, neutral grey, no filler. See `docs/COPY.md`.
- **Honest numbers.** Never present stale or synthetic figures as fact.

## Known limits

Instagram caps follower lists at about 25-50 per target, so following lists are the productive route. No
Instagram endpoint returns bios in bulk. See `docs/OPEN_ITEMS.md` for the current backlog.
