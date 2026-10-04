# Screenshot regression

Captures the Leads page, the other pages and every map mode at 1280 and 1440 px, compares them with stored
baselines, and checks that nothing changes while the screen is idle (which catches flicker).

```sh
# one-off: a clone of the real-data snapshot (APFS clone, no extra disk)
cp -c work/oct4-reports/data/snapshot/leads.sqlite /tmp/vis/leads.sqlite && cp -cR work/oct4-reports/data/snapshot/pfp /tmp/vis/pfp

node tests/visual/run.mjs --db /tmp/vis/leads.sqlite --update   # on a known-good branch: record baselines
node tests/visual/run.mjs --db /tmp/vis/leads.sqlite            # on the branch under test: compare
```

- Exit code 1 on any changed, missing, flickering or never-settling screen, or a page wider than the window.
- Images, `*.diff.png` (changed pixels in red) and `report.json` go to `tests/visual/out/`.
- A pixel counts as changed above `--channel 12` (0-255); a screen fails above `--tolerance 0.003` (0.3% of pixels); idle frames fail above `--idle-tolerance 0.0001`.
- Map modes are found on the page (the mode select, or the mode buttons), so the list follows the branch.
- Baselines contain real names and photos: they live in `tests/visual/baselines/` (git-ignored) or `--baselines DIR`.
- The browser is a headless Chromium (Playwright cache, or `FL_CHROMIUM`) with a throwaway profile, driven over DevTools by `cdp.mjs` with no dependencies. `--cdp-url` attaches to a browser you started instead. It only loads the local test server; the harness refuses port 8777 and the live database.
- Ports: server 9148, DevTools 9147 (`--port`, `--cdp-port`).
- Pixel logic is unit tested in `tests/ui/visual-compare.test.mjs`.
