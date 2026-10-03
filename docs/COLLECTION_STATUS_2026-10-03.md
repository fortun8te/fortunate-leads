# Collection check, 3 October 2026

Read-only baseline at 13:57 Europe/Amsterdam, before deployment or restart.

| Direction | Proven complete | Partial | Waiting | Blocked |
|---|---:|---:|---:|---:|
| Following | 29 | 29 | 18 | 1 |
| Followers | 3 | 27 | 28 | 0 |

Collection was stopped. New profiles, list entries, and bios collected today and in the previous hour were all zero. The last saved list page was 1 October at 03:12 Amsterdam.

61,537 current-attempt memberships have tracked run evidence, covering 53,674 distinct people. Another 217 legacy memberships lack current-run proof. The database contains 122,161 profiles, 14,534 observed bios, and 144,828 historical edges. These are different measurements; historical edges do not prove current coverage or personal relationships.

26 follower lists reached Instagram caps. Partial following lists contain 34,593 of 40,017 entries. Some queued following lists retain private-access denials. A queued state does not promise that the account can be collected.

The sole candidate collection account was offline with a stale heartbeat and no recorded account hold. The warned account remained paused with a challenge hold and an unreviewed scraping warning. The main account was online and protected for personal use.

Evidence: fresh local /api/control and /api/scraper responses and read-only SQLite queries. The saved target-coverage-current.json report dated 26 September is stale and must not be used for current numbers.

## Verification boundaries

A passing simulator verifies synthetic interruption and recovery cases. It does not prove live Instagram collection, unrestricted follower access, a clean multi-account soak, or qualification quality. Live monitoring must report actual pages, people, warning events, stop state, and elapsed duration separately.

## Executed checks

The settled backend suite passed 1,258 tests with three optional skips. Combined interface and extension checks passed 603 tests. The repaired root Python suite passed 21 checks; sidecar/service checks passed 27. Ten rendered browser checks passed, and desktop/mobile controls were visually inspected using an isolated fixture.

Earlier interruption runs passed 82 checks across 24 simulated hours and 40 checks in a two-account scenario completing after 4.58 simulated hours. The checkout was still changing during those runs, so final release verification reruns the settled simulator rather than attributing them to exact final code.
