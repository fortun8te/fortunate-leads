# UI polish checks

The smoke checks use the existing sample backend and an isolated Chromium browser. They do not read or modify real leads, browser sessions or Instagram accounts. No new application dependency is needed.

1. Serve the `web/` directory on localhost:

   ```sh
   python3 -m http.server 8879 --bind 127.0.0.1 --directory web
   ```

2. Start an isolated Chrome/Chromium instance with a fresh temporary user-data directory and a loopback debugging port. Do not attach these tests to a personal browser profile. The machine used for this review had an installed Chromium headless shell listening on port 9338.

3. With [browser-harness](https://github.com/browser-use/browser-harness) installed, run from the repository root:

   ```sh
   BU_NAME=fortunate-ui-test BU_CDP_URL=http://127.0.0.1:9338 UI_OUTPUT=/tmp/fortunate-ui-checks browser-harness <<'PY'
   exec(open('tests/ui/polish.py').read())
   PY
   ```

`UI_BASE_URL` can override the localhost preview address. `UI_OUTPUT` receives screenshots and `checks.json`. Failed assertions exit nonzero and preserve all completed checks.

The checks use real browser keyboard events and accessibility-tree click targets. DOM reads inspect layout and state; direct focus/scroll setup makes keyboard and virtualization regressions repeatable. The fixed mock dataset keeps screenshots comparable, although the simulated progress counters advance with time.

The UI checks supplement the existing server and extension suites. They do not emulate Instagram network collection; use `tests/e2e/` for that behavior.
