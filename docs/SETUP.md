# Setup

Fortunate Leads runs on one Mac: a local Python server (the database and the web app on
http://127.0.0.1:8777) and a Chrome extension in one Chrome profile per Instagram account. The extension
reads lists and can read bios from a logged-in tab; the server decides what to fetch next.

## New Mac: three steps

1. **Get the code.** Needs macOS, Google Chrome and git (Python 3.9+ is already on a Mac with the Command Line Tools).
   ```sh
   git clone https://github.com/fortun8te/fortunate-leads.git ~/fortunate-leads
   ```
2. **Run the installer.** It sets up the server at login, a daily backup and a health check, then opens the web app.
   ```sh
   ~/fortunate-leads/ops/install.sh
   ```
   Safe to re-run. Later, double-click `ops/start-all.command` to start everything (it runs the installer first if needed).
3. **Follow Get started in the web app.** It checks the server, the extension in each Chrome profile, the Instagram
   login, optional AI keys and backups, and gives one action per item. Load the extension once per profile
   (`chrome://extensions`, Developer mode, Load unpacked, the `extension` folder; the installer copies the path),
   log in to instagram.com there, then paste an account handle and press Start. Lists and bios collect at the safe
   pace and leads rank as they arrive. Get started disappears from the sidebar once everything is in place.

The sections below are the detail behind those steps.

## 1. Prerequisites

- macOS with Google Chrome.
- Python 3.9 or later with SQLite 3.35 or later. The macOS `/usr/bin/python3` is fine. Check with
  `python3 -c "import sqlite3, sys; print(sys.version.split()[0], sqlite3.sqlite_version)"`.
- git. Node 18+ only if you want to run the extension and end-to-end tests.
- No pip packages for the server: it uses the standard library. The optional Laya sidecar has its own
  Python environment.

## 2. Clone

```sh
git clone https://github.com/fortun8te/fortunate-leads.git ~/fortunate-leads
cd ~/fortunate-leads
```

Everything below runs from the repo folder.

## 3. Run the server

Try it by hand first:

```sh
python3 server/server.py            # http://127.0.0.1:8777, database data/leads.sqlite (created on first start)
```

Open http://127.0.0.1:8777. Stop it with Ctrl-C. `--port` and `--db` change the defaults.

Then install it as a LaunchAgent, so it starts at login and restarts if it dies:

```sh
ops/install-launchagent.sh --with-backup   # server agent + a daily 03:30 database backup; safe to re-run
```

It stops any other server on :8777 (and the old :8766 one) first. Log: `~/Library/Logs/fortunate-leads.log`.
`ops/uninstall-launchagent.sh` removes both agents; data and backups stay.

## 4. Load the extension (once per Chrome profile)

1. In the Chrome profile you want to use, open `chrome://extensions`.
2. Turn on Developer mode (top right), click Load unpacked and pick the `extension` folder of this repo.
3. Keep one instagram.com tab open, logged in to the account this profile should use. The extension pins its own
   background tab if none is open.

After a `git pull` that changes `extension/`, click the reload icon on the extension card in each profile.
The popup shows the version, the state and the next request; Resume there clears a login or security-check hold.

For this Mac, double-click `ops/start-all.command` to start the installed local services.
It does not open Chrome profiles or resume collection. Accounts shows connected profiles; Collection
controls resume lists and bios explicitly when there is no Instagram hold. External AI remains opt-in.
Local processing can work with collection paused. Stop all also turns local processing off.

Local note interpretation uses the installed Ollama `llama3.2:3b` model through loopback only.
Install it once with `ollama pull llama3.2:3b` if absent. The download is about 2 GB.
The reader shows suggestions with exact quotes and never changes a relationship automatically.
Original notes remain saved if the model is unavailable. Notes are excluded from external model prompts.

## 5. Add accounts

Open **Accounts** in the sidebar and click **Add account**. The wizard walks through the three steps (new
Chrome profile, load the extension, log in to Instagram) and ticks each one as the new profile checks in.

Per account you can set:
- **Role**: Lists, Bios or Both.
- **Main account**: your own Instagram account. It reads bios only, unless Settings gives it a share of the
  list pages (Main account list share).
- **Budget**: pages and bios per day. Empty uses the default of 3000 list pages and 300 bios a day per account.
  0 bios means no daily number (the pacing still applies).
- **Rename** gives the account a label; **Pause** stops it; **Remove** forgets it (its list moves on).

Each account keeps its own pacing: 7 to 12 s between list pages with a longer break every 40 to 60 pages,
35 to 70 s between bios, and never more than 72 Instagram requests in any 11 minutes. Any rate limit cools that
account down on its own; the other accounts keep going.

## 6. OpenRouter keys (optional, for the LLM verdicts)

Qualification works without any key (rule verdicts). With keys, a free model also reads the bios.

Open **Settings**, paste a key from https://openrouter.ai/keys and click **Add key**. Test checks the key with one
small request. Keys are saved in `data/openrouter.json` (mode 600, gitignored) and are never shown again in full.
Only `:free` models are eligible for qualification. Available free models are tried in the configured order;
a refused key stays off until a Test passes.

Keys can also come from the environment (`OPENROUTER_API_KEYS=sk-or-...,sk-or-...`); those show as
Environment in Settings and can only be removed where they are set.

The same page sets model calls at once, the model threshold, the bio floor (planned bio reads skip people whose
prefilter score is below it; Read now always works) and whether qualification starts by itself once the lists are done.

## 7. Laya sidecar (optional)

A local model that adds one soft signal to the ranking. The server uses it when it answers and skips it otherwise.

```sh
cd sidecar
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # torch + transformers, about 2 GB with the model
.venv/bin/python laya_server.py             # first run downloads the model to ~/.cache/huggingface
```

Settings, Services shows whether it is running. More options: `sidecar/README.md`.

## 8. First run checklist

1. `ops/doctor.sh` prints PASS for the server, the database and the LaunchAgent.
2. http://127.0.0.1:8777 opens; the status strip at the top says Idle or Running, not Extension offline.
3. Accounts lists every Chrome profile with its @handle.
4. Scraper, Add seeds: paste a few @handles and click Queue. Within a minute the top strip shows the list being read.
5. Leave it running. Following lists go first; bios of people in 2+ lists are read in the list gaps.

## 9. Health, backups and data

- `ops/doctor.sh` checks the server, the database, the agents, the log size and the backups
  (`--fix` restarts the agent and stops stray servers first).
- `ops/backup.sh` makes a consistent backup now: `data/backups/leads-YYYYmmdd-HHMM.sqlite`, 14 kept.
  Restore steps are at the top of that script.

Where things live:

| what | where |
|---|---|
| database | `data/leads.sqlite` (plus `-wal`, `-shm` while running) |
| backups | `data/backups/` |
| profile pictures | `data/pfp/` |
| OpenRouter keys and models | `data/openrouter.json` |
| server log | `~/Library/Logs/fortunate-leads.log` |
| extension state | inside each Chrome profile (chrome.storage) |

`data/` is gitignored. Profile requests contact Instagram through the configured route; optional LLM
qualification sends selected profile evidence to the configured model provider. Do not put API keys or
proxy credentials in tracked files.

## 10. Quitting and resuming

Everything picks up where it stopped. Closing Chrome, putting the Mac to sleep or restarting it is safe:
leased work goes back in the queue after 10 minutes, lists resume from their saved cursor, and results the
extension could not deliver wait in its outbox. After a restart, open Chrome with each profile and one
Instagram tab; the LaunchAgent has already started the server.

To stop collection on purpose, pause lists and bios in **Controls**, or pause an individual account.

## 11. Troubleshooting

| symptom | what to do |
|---|---|
| Extension offline | Chrome is closed, or the extension is off in that profile. Open the profile; check `chrome://extensions`. |
| Needs login / Security check on an account | Collection pauses across all accounts. Resolve the warning in that Chrome profile, resume its extension, then explicitly resume lists and bios in Controls after the shared wait ends. |
| Shared wait | An Instagram limit or unexpected list redirect stops collection across accounts until the displayed wait ends. A login or security warning also keeps lists and bios paused until you explicitly resume them. |
| Page does not load | `ops/doctor.sh --fix`, then check the log. By hand: `python3 server/server.py` shows errors directly. |
| Port 8777 in use | `ops/doctor.sh --fix` stops stray servers and restarts the agent. |
| A key shows Refused | The key is wrong or revoked. Replace it in Settings. |
| Bios are not being read | For the extension, check its Bios/Both role, daily budget and bio floor. |

## 12. Tests (for development)

```sh
python3 -m unittest discover -s server/tests
python3 -m unittest sidecar/test_laya.py
node --test extension/test/*.test.mjs
node --test tests/web/*.test.mjs          # note-save races, filters, exports and demo workflows
node tests/e2e/driver.mjs                 # 8 simulated hours against a fake Instagram, about a minute
node tests/e2e/driver.mjs --lanes 1,2,4   # several accounts
python3 tests/bench.py                    # timings on a synthetic 100k-people database
```

The web app also runs without a server at `web/index.html?mock=1` (served from any static server).
