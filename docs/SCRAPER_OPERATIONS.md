# Public bio collector operations

The optional public bio collector enriches people already known to Fortunate Leads with publicly observable
profile fields. It does not read follower or following lists, log into Instagram, or use the extension's
account cookies. The Chrome extension still collects lists and may also read bios. Both readers use the
server's profile queue, so a profile job has one lease holder at a time.

## Set up the integrated worker

Run these commands from the repository root. The server itself remains Python standard library only;
the scraper needs Python 3.10 or newer and the packages in `scraper/requirements.txt`.

```sh
python3.12 -m venv data/scraper-venv
data/scraper-venv/bin/python -m pip install -r scraper/requirements.txt
```

Use another Python 3.10+ executable if `python3.12` is unavailable. Keep the environment at exactly
`data/scraper-venv`, where the server looks for it. `scraper/install.sh` installs the separate `igscrape`
app under `~/ig-scraper`; it does not set up this integrated environment.

Configure one or more permitted connections in the gitignored data directory. The supplied example uses
the locally managed Tor SOCKS port. Adjust it to the connections actually available to you; circuit count
does not equal independent exits or proven throughput.

```sh
cp scraper/workspace-egresses.example.yaml data/scraper-egresses.yaml
ops/tor-start.sh
python3 ops/tor-status.py
```

Tor is optional when you configure another supported egress type. The managed Tor commands only control
the Tor process they started; `ops/tor-stop.sh` stops that process. Keep proxy credentials in `data/`,
which is gitignored, and avoid pasting the configuration into logs or issues.

Start the local server as in [SETUP.md](SETUP.md). Its UI and API remain on `127.0.0.1:8777`; the public
worker is a child process, not a second web service. Check readiness and enable it with a same-origin API
request (or the equivalent Settings control when present):

```sh
curl http://127.0.0.1:8777/api/public-bios
curl -X POST http://127.0.0.1:8777/api/settings/public-bios \
  -H 'Origin: http://127.0.0.1:8777' -H 'Content-Type: application/json' \
  -d '{"on":true,"scope":"queued"}'
```

The default is **off**, 16 concurrent profiles and `scope=queued`. Queued scope reads existing profile
jobs; `scope=missing` also plans people whose bio has not been observed, up to a bounded active queue.
That broader scope includes seeds and skips people marked Not a fit unless they are seeds. Set
`"concurrency"` to an integer from 1 to 32 if needed. `"egress_file"` must point inside `data/`.
The collector does not inherit the extension's daily bio budget or prefilter threshold. Pause the Bios
stage to pause both bio readers, or set `"on":false` to stop only the public collector.

## Read status and recover

`GET /api/public-bios` reports setup readiness, running state, work remaining, successes, cached results,
deferred results, terminal errors, current worker health and recent fresh bios per minute. The ETA appears
only while running with enough recent successes. A cache hit is not a newly fetched bio. A genuinely empty
bio counts when the source confirms that it was observed; an incomplete profile does not.

The server stores jobs, lease tokens, retry times, results and connection cooldowns in SQLite. A worker
restart releases its leases; expired leases can be retried. A requested cooldown survives a worker restart,
so restarting will not make a blocked connection ready. If all connections are resting, wait for their
cooldowns and check the egress health. Avoid repeatedly toggling the worker to chase rate limits.

An older cached observation cannot overwrite a newer bio or profile. If a public response lacks a
confirmed bio, has the wrong identity, or is otherwise incomplete, it does not mark the bio as read.
Errors and deferred attempts remain visible in status; a terminal error needs an explicit retry or other
reader to resolve it. Use the server's normal database backup before changing configuration at scale.

## Standalone `igscrape` and measurement

The standalone CLI/web UI in `scraper/QUICKSTART.md` accepts pasted handles and exports JSON/CSV. It has
its own cache and job state; it does not feed the Fortunate Leads database or take leases from the integrated
worker. The older HTTP `/runs` API keeps job metadata only in memory, so use the integrated worker for
durable app enrichment.

Measure **fresh, confirmed bios per minute**, separately from cache hits, partial profiles, failures and
unfinished inputs. A live five-minute measurement on the operator's Mac saved **1,229 fresh complete bios
in 301.2 seconds, or 244.8 bios/min**, with 32 profile tasks and 36 Tor circuits. It recorded 88 error/retry
results, excluded from that rate, and no cache hits. The report is `data/reports/live-worker-5min.json`.
This reaches the lower part of the 200–500 target in this environment; 500/min is unverified. Tor circuits can share exits;
adding circuits or concurrency alone does not establish a higher successful rate. The offline benchmark
in `scraper/benchmark.py` measures local processing under simulated latency, not Instagram throughput.

Qualification is separate from collection. The app can assign rule verdicts without AI; optional model
calls use only IDs ending in `:free` and may be unavailable. A profile read or model verdict alone does
not establish that someone is a suitable lead.
