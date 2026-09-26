# Laya decision sidecar (optional)

A local HTTP service that answers Fortunate's ideal-client questions about a profile, using
[Laya](https://huggingface.co/convaiinnovations/laya) from ConvAI Innovations. The default
checkpoint uses mmBERT for multilingual text (Apache 2.0). The qualifier calls it when it is reachable and skips it otherwise.

## Setup (macOS)

    sh sidecar/setup.sh
    sidecar/.venv/bin/python sidecar/laya_server.py  # cached files only

    curl -s 127.0.0.1:18742/health       # reports model, deployment_version and device

The setup script resolves its own directory, installs `laya==0.3.20`, and does not fetch weights or start a service. Dependency installation requires network access. The startup check rejects a different installed Laya version. Transitive dependencies are not fully locked.

Runtime uses `HF_HOME` when set, otherwise `sidecar/.cache/huggingface`, independent of the working directory. The default sets `HF_HUB_OFFLINE=1` before importing the model library. Missing cached weights cause startup to fail and the main server skips this optional signal. To deliberately fetch the checkpoint, run the same command once with `--allow-download` and keep the same `HF_HOME` for later starts. If your shell already sets `HF_HUB_OFFLINE=1`, unset it for that explicit download. The unit tests below do not download weights or run inference.

Options:
- The default `convaiinnovations/laya:multilingual` uses the multilingual checkpoint inside the bundled `convaiinnovations/laya` repository. It is intended for Dutch and other non-English bios. Its language coverage does not guarantee accurate classification of a given profile. The `:multilingual` suffix is interpreted by this sidecar as `subfolder="multilingual"`; the standalone `convaiinnovations/laya-multilingual` repository is also supported when cached separately.
- `--model convaiinnovations/laya` selects the English checkpoint. Set the same `LAYA_MODEL` in the main server so mismatched sidecars are rejected.
- Set `LAYA_DEPLOYMENT_VERSION` to the same value for both processes, and change it when the checkpoint weights change. Stored answers are keyed by this value, model name, questions and local scoring version.
- `--device cpu` or `LAYA_DEVICE` sets the device. By default it tries MPS, then CUDA, then CPU.
- `--port` or `LAYA_PORT` sets the port. Set `LAYA_PORT` for the main server too. `LAYA_BATCH_SIZE` defaults to 16.

The server binds to 127.0.0.1 only and rejects any non-loopback peer with a 403.

## API

`POST /decide`

```json
{"items": [{"id": "u1", "text": "..."}, {"id": "u2", "person": {"handle": "...", "name": "...", "category": "...", "bio": "...", "website": "...", "followers": 1234}}],
 "questions": [{"key": "dtc_founder", "q": "Is this ...?"},
               {"key": "size", "q": "How large?", "labels": ["small", "large"]},
               {"key": "niche", "q": "Which category?", "labels": ["skincare", "food"], "multi": true}]}
```

If you leave out `questions`, the server uses `questions.json`. Explicit empty or null questions are rejected. Each item needs a `text` or a `person`, and a unique nonblank string or integer ID. Integer `1` and string `"1"` count as a duplicate. IDs are returned in request order with their original types. Question keys and labels must be unique and nonblank; expanded multi-label keys must not collide.
A `person` is rendered as `Handle: @x\nName: ...\nCategory: ...\nBio: ...\nWebsite: ...\nFollowers: ...`.

The response looks like this:

```json
{"results": [{"id": "u1", "answers": {
   "dtc_founder": {"p": 0.93, "confidence": 0.81},
   "size":  {"p": 0.61, "label": "large", "probs": {"small": 0.39, "large": 0.61}, "confidence": 0.4},
   "niche": {"p": 0.82, "label": "skincare", "probs": {"skincare": 0.82, "food": 0.05}}}}],
 "model": "convaiinnovations/laya:multilingual", "deployment_version": "laya-0.3.20-checkpoint-1", "device": "mps", "ms": 812.3}
```

- A question without labels is a yes/no question (Laya `noul`), and `p` is P(yes).
- A question with labels is a single choice (Laya `choice`). `label` is the argmax and `p` is its probability.
- `multi: true` runs one yes/no question per label, because Laya has no native multi-label type. The `probs` values are therefore independent and do not sum to 1.
- Health and successful answers report `model`, `deployment_version`, and an optional `questions_signature` SHA256 of the canonical question list. Health hashes the default list; answers hash the requested list.
- All expected answer keys and label probabilities must be present. Probabilities must be finite numbers between zero and one. A malformed row, incomplete batch or invalid answer rejects the whole batch.
- Laya batch output is positional. When a backend includes a `state` echo, its order is also verified. Backends that omit it must honor the positional API contract.
- Errors: 400 for a bad request, 500 with a generic unavailable error when the model fails. The caller should treat both as "skip". Raw model errors are not returned to clients.

## Tests

    python3 -m unittest discover -s sidecar -p 'test_*.py'   # fake backend/service, no torch, download or inference

For a separate real-checkpoint smoke test without starting a service, pass the path to a local
`convaiinnovations/laya` snapshot containing `multilingual/`:

    sidecar/.venv/bin/python sidecar/smoke_cached.py /path/to/cached/snapshot --device cpu

The script uses synthetic profiles, a temporary copy of mutable metadata, and temporary scratch/cache paths.
On September 27, 2026 local CPU runs, the checkpoint loaded in 4.7–8.0 s; 64 profiles took
4.6–5.0 s and 256 took 18.1–19.5 s, about 13–14 profiles/s, with 2.4–2.7 GB peak process RSS.
These are throughput checks, not accuracy
checks. An MPS attempt in the test sandbox fell back to CPU, so MPS performance is unmeasured.

## Run at login (LaunchAgent)

The service is opt-in. From the **deployed checkout**, after `sidecar/setup.sh` and a separate
cached-weight download, run:

    python3 sidecar/laya_service.py install          # checks the pinned package/cache; writes a plist only
    python3 sidecar/laya_service.py start            # loads the cached model; waits up to 180 s for matching health
    python3 sidecar/laya_service.py health           # checks model and deployment version
    python3 sidecar/laya_service.py stop             # stops this LaunchAgent

`install` writes `~/Library/LaunchAgents/com.fortunate.laya.plist` with absolute checkout paths,
offline mode, a 30-second restart throttle, and logs at `~/Library/Logs/FortunateLeads/laya.log`.
It does not launch the model. Once started, the LaunchAgent is configured to run at login.
To force CPU, run `LAYA_DEVICE=cpu python3 sidecar/laya_service.py install`; the device choice is
saved in the plist and checked by `health`. Without `LAYA_DEVICE`, the sidecar selects a device
automatically.
`start` stops a failing service after the health deadline so it cannot keep restarting with a
bad cache. An existing plist is preserved unless you explicitly run `install --replace` while
the service is stopped. If the model or deployment version changes, update both the main server
and sidecar environment before starting; otherwise the client rejects the response.

For an older two-argument `com.fortunate.laya.plist` from this checkout, use `stop`, then
`install --replace`, then `start`. The manager checks the plist label and executable paths before
stopping or replacing it, and refuses to manage a service from another checkout.

## Caveats

- Laya is zero-shot: you write the questions in plain English and there is no training step. The vendor says that
  both checkpoints ship uncalibrated. The main server uses `p` only as a ranking hint, not as a confidence guarantee or standalone tag.
- The multilingual checkpoint has a 1024-token per-question budget; the English checkpoint has 512. Long profile text may be truncated.
- The checkpoint loaded and answered the synthetic smoke test offline. LaunchAgent startup and long-running behavior still need a reviewed deployment check. The synthetic smoke test does not establish classification accuracy on real leads.
