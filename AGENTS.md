# Agent notes

Read `docs/PRODUCT.md` first: what this is, who a lead is, and the rules.

- Never loosen Instagram pacing or add proxy/Tor/evasion code.
- Never restart or edit the live server (LaunchAgent `com.fortunate.leads`, data in `data/`); test on a temp DB and another port.
- UI copy: sentence case, plain words, see `docs/COPY.md`.
- Tests: `python3 -m unittest discover -s server/tests`; `node --test extension/test/*.test.mjs tests/*.test.mjs tests/*.test.cjs tests/ui/*.test.* tests/web/*.test.*`; `node tests/e2e/driver.mjs`.
- Open backlog: `docs/OPEN_ITEMS.md`.
