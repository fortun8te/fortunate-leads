# Fortunate research harness

`server/external_harness.py` runs the installed Hermes runtime with this repository's
minimal configuration in a private temporary home. It never edits the user's Hermes
profile. Only credentials for the selected provider are copied; the temporary copy
and research session are deleted after the process exits. No provider fallback,
inherited memories, owner note text, MCP servers, shell tools or enabled user plugins.

The user explicitly selects external mode before any call. Broad research additionally
requires an unresolved local qualification question. Optional deep research has its own
switch. Both reuse the normal qualification rubric; positive and negative deep verdicts
need checked evidence. A failed or unsupported answer leaves the existing score intact.

Broad runs have four model/tool rounds and a 120-second agent budget. Deep runs have six
rounds and 180 seconds. The parent kills the entire process group after a ten-second
grace period. "Two searches and one page" is guidance, not a per-tool hard limit: Hermes
can issue several tools within a round. Citation verification is separately bounded to
two pages with ten-second per-page deadlines.

Only related website quotes verified by the server can augment broad business evidence.
Search snippets cannot establish identity or ownership. Cached research is supplied by
the caller and reused. A relationship proves history, not business fit or current contact.

Each isolated run records success/failure and reported session tokens in the existing
private usage ledger, under external_broad or external_deep. Missing/default-zero Hermes
counters are recorded as unknown, with missing_token_reports exposed in the report.
The historical Hermes profile reader remains explicitly labelled historical.

Verified against installed Hermes CLI/config loader on 2026-09-27; model inference was
not exercised externally because external AI is off. Official reference:
https://hermes-agent.nousresearch.com/docs/reference/cli-commands
