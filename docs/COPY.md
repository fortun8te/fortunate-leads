# Copy style guide

Every user-visible string follows this. Plain, specific, calm. No one should need to know how the system works to read it.

## Terms

| Use | Means | Not |
|---|---|---|
| lead | a person saved in the workspace | prospect, contact, record |
| person / people | someone seen on the map or in a list, not yet a lead | user |
| account | an Instagram login connected through the extension | lane, profile (for logins) |
| profile | an Instagram page we read | account (for pages) |
| follower list | a collected list of followers or following | list page (in prose), cursor |
| tag | a label you add to a lead | label (except an account's name) |
| workspace | the Fortunate Leads web app | app, dashboard |
| collection | scraping Instagram lists | scraping, crawling |
| paused | stopped by you | held, parked |
| limited | Instagram is slowing this account | soft block, rate limited, cooldown |

## Rules

1. Sentence case everywhere. Buttons, headings, tabs, menu items.
2. Say what happened, then what to do. "Couldn't save. Try again." Never "Error".
3. Never show codes: no HTTP numbers, snake_case, ids, lease, lane, hold. Raw detail goes in a "Copy diagnostics" area only.
4. No filler: "Let's", "Simply", "Oops", "Please", "Successfully". No exclamation marks. No emoji.
5. Use contractions in errors ("Couldn't", "Can't"). Keep "Could not" out of the UI.
6. Buttons are verbs that name the result: "Remove account", "Add tag". Never OK or Cancel alone.
7. Destructive actions state the consequence and use the verb on the button. Two-step buttons ask "Remove account?".
8. Plurals always go through `plural(n, 'lead')`. "1 lead", "2 leads", never "1 leads" or "lead(s)".
9. Numbers use thousands separators (`int()`). Percent has no space in prose: 40%.
10. Relative time: "moments ago", "5m ago", "3h ago", "2d ago". Clock times are 24-hour HH:MM.
11. Ellipsis (…) only for work in progress ("Saving…"). Never to trail off. Don't truncate names without a title.
12. Toasts are one short sentence with a period only when there are two sentences.
13. Placeholders show an example, not an instruction. Labels stay visible; placeholders never replace them.
14. Every icon-only control has an aria-label in the same wording as its tooltip.
15. Tab title is "Fortunate Leads". The favicon is the square mark.
