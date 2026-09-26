# Instagram Profile Scraper: Quick Start

Paste Instagram usernames and export public profile fields as JSON/CSV using Apify-style column names. Runs locally; availability depends on what Instagram returns. It enriches bios and profile details, not follower/following lists.

## 1. Install (once, about 1 minute)

Open **Terminal** (press Cmd+Space, type "Terminal", press Enter) and run:

```
cd ~/Downloads
tar -xzf ig-scraper-release.tar.gz
bash ig-scraper-release/install.sh
```

- If it says you need a newer Python, answer **Y** (it installs a small tool called `uv` that brings its own Python).
- When it's done, **close Terminal and open a new window**.
- It needs about 60 MB of disk space (about 160 MB if it has to download Python).

## 2. Use it

**The easy way (a web page):**

```
igscrape ui
```

Your browser opens a page. Paste usernames, click **Run**, watch the progress bar, then click **Download JSON** or **Download CSV**.
Leave the Terminal window open while you use the page. Press Ctrl-C in Terminal to quit.

**From Terminal:**

```
igscrape nasa natgeo
```

This saves `ig-results-<date>.json` and `.csv` in the folder you're in and prints where they are.
These files belong to the standalone `igscrape` app; the Fortunate Leads dashboard has no export
button or export endpoint. The files contain every result from that scrape run, including error rows;
they do not come from the dashboard's paginated lead list. A stopped run contains only the results
completed before it stopped, so compare the saved row count with the run's `done/total` progress.
JSON keeps the original values. CSV keeps Unicode, line breaks and numeric zero, but shows missing
values as blank cells and adds a leading apostrophe to text that a spreadsheet might treat as a formula.

With a list of usernames in a text file (one per line):

```
igscrape -f usernames.txt -o out.json --csv out.csv
```

While it runs you see one line like:

```
[#########---------------] 38/100 done | 22.3 fresh bios/min | egresses 1/1 healthy | ETA 1m11s
```

- **done**: inputs processed, including failed attempts; check the error count too
- **fresh bios/min**: successfully fetched biographies per minute, excluding cached results, resumed results and failed attempts; a genuinely empty bio is a valid observation
- **egresses healthy**: how many connections are working right now (a "resting" one comes back by itself)
- **ETA**: estimated time to process the remaining attempts, not a promise of full data coverage; cooldowns may extend it

Usernames can be `nasa`, `@nasa` or `https://www.instagram.com/nasa/`.

## 3. Good to know

- **Stopped halfway?** With caching enabled (the default), each result is written to a durable job before progress advances. Run the same set of usernames again with the same about-section option to resume. Transient failures and unavailable bios are retried; successful observations are reused for 24 hours, then refreshed. `--no-cache` disables persistent resume. Keep the cache database; exports alone do not hold the resume queue.
- **Private profile?** Public header fields and an observed bio can still succeed. Private posts are not exposed. An unavailable bio has `error=incomplete_profile`; `biography=""` with `_scrape.bioStatus=empty` means an actual empty bio. Uncertain upstream responses remain retryable rather than being labelled permanently missing.
- **Want it faster?** Inspect working connections in `~/ig-scraper/egresses.yaml` and check them with `igscrape egresses`. More connections do not guarantee more successful bios. Tor circuits can share exits. Respect cooldowns and measure fresh successful bios rather than requests, failures or cached rows. 200–400 fresh bios/min has not been verified for this installation.
- **Is it working?** `igscrape selftest` checks the install without contacting Instagram.
- **Update:** download the new `ig-scraper-release.tar.gz`, then run steps 1 again. Your `egresses.yaml` is kept.
- **Remove everything:** `bash ~/ig-scraper/uninstall.sh`

## If something goes wrong

| You see | Do this |
|---|---|
| `command not found: igscrape` | Open a new Terminal window. Still broken? Run `~/.local/bin/igscrape` |
| `needs Python 3.10+` | Re-run `bash install.sh` and answer Y |
| lots of `rate_limited`, `login_required` or `no_proxies_left` failures | Instagram is restricting requests. Let the reported cooldown expire; security or login restrictions may need manual attention |
| `egresses 0/1 healthy` | All connections are resting. By default queued work waits; a configured maximum wait may end those attempts with retryable errors |

Your files: `~/ig-scraper/` (web-page results are saved to `~/ig-scraper/results/`).

## Repository benchmark

From the fortunate-leads repository, test the engine without making network requests:

```
data/scraper-venv/bin/python scraper/benchmark.py --seconds 20 --latency-ms 400 --inflight 2 --output data/benchmark-offline.json
```

The JSON report separates fresh successful biographies, cached observations, partial profiles,
errors and unfinished inputs. Offline responses have a configurable delay and deterministic failures.
Its timing measures local processing under those assumptions; it does not establish Instagram or Tor speed.

For a deliberately bounded live measurement using your supplied handles:

```
data/scraper-venv/bin/python scraper/benchmark.py --live --handles-file data/benchmark-handles.txt --profiles 32 --seconds 120 --egresses scraper/egresses.yaml --output data/benchmark-live.json
```

Live mode keeps the existing rate limits, connection limits and cooldowns. It uses no cached results,
does not rotate blocked exits to bypass waits, and cannot exceed 32 input profiles in this initial check.
Passing a short sample is not proof of sustained 200–500 fresh bios/min.

The older `igscraper.api` HTTP `/runs` endpoints retain job metadata only in memory and lose it on restart.
Use the fortunate-leads integrated durable queue for production scraping; the legacy API is for development.
