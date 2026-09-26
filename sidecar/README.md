# Laya decision sidecar (optional)

A local HTTP service that answers Fortunate's ideal-client questions about a profile, using
[Laya](https://huggingface.co/convaiinnovations/laya-multilingual) from ConvAI Innovations. The default
checkpoint uses mmBERT for multilingual text (Apache 2.0). The qualifier calls it when it is reachable and skips it otherwise.

## Setup (macOS)

    sh sidecar/setup.sh
    sidecar/.venv/bin/python sidecar/laya_server.py  # cached files only

    curl -s 127.0.0.1:18742/health       # reports model, deployment_version and device

The setup script resolves its own directory, installs `laya==0.3.20`, and does not fetch weights or start a service. Dependency installation requires network access. The startup check rejects a different installed Laya version. Transitive dependencies are not fully locked.

Runtime uses `HF_HOME` when set, otherwise `sidecar/.cache/huggingface`, independent of the working directory. The default sets `HF_HUB_OFFLINE=1` before importing the model library. Missing cached weights cause startup to fail and the main server skips this optional signal. To deliberately fetch the checkpoint, run the same command once with `--allow-download` and keep the same `HF_HOME` for later starts. If your shell already sets `HF_HUB_OFFLINE=1`, unset it for that explicit download. No model download or inference was performed for the offline tests below.

Options:
- The default `convaiinnovations/laya-multilingual` is intended for Dutch and other non-English bios. Its language coverage does not guarantee accurate classification of a given profile.
- `--model convaiinnovations/laya` selects the English checkpoint. Set the same `LAYA_MODEL` in the main server so mismatched sidecars are rejected.
- Set `LAYA_DEPLOYMENT_VERSION` to the same value for both processes, and change it when the checkpoint weights change. Stored answers are keyed by this value, model name, questions and local scoring version.
- `--device cpu` or `LAYA_DEVICE` sets the device. By default it tries MPS, then CUDA, then CPU.
- `--port` or `LAYA_PORT` sets the port. `LAYA_BATCH_SIZE` defaults to 16.

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
 "model": "convaiinnovations/laya-multilingual", "deployment_version": "laya-0.3.20-checkpoint-1", "device": "mps", "ms": 812.3}
```

- A question without labels is a yes/no question (Laya `noul`), and `p` is P(yes).
- A question with labels is a single choice (Laya `choice`). `label` is the argmax and `p` is its probability.
- `multi: true` runs one yes/no question per label, because Laya has no native multi-label type. The `probs` values are therefore independent and do not sum to 1.
- Health and successful answers report `model`, `deployment_version`, and an optional `questions_signature` SHA256 of the canonical question list. Health hashes the default list; answers hash the requested list.
- All expected answer keys and label probabilities must be present. Probabilities must be finite numbers between zero and one. A malformed row, incomplete batch or invalid answer rejects the whole batch.
- Laya batch output is positional. When a backend includes a `state` echo, its order is also verified. Backends that omit it must honor the positional API contract.
- Errors: 400 for a bad request, 500 with a generic unavailable error when the model fails. The caller should treat both as "skip". Raw model errors are not returned to clients.

## Tests

    python3 -m unittest sidecar/test_laya.py   # fake backend, no sockets, torch, model download or inference

## Run at login (LaunchAgent)

Save the following as `~/Library/LaunchAgents/com.fortunate.laya.plist`, and fix the paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.fortunate.laya</string>
  <key>ProgramArguments</key><array>
    <string>/Users/YOU/fortunate-leads/sidecar/.venv/bin/python</string>
    <string>/Users/YOU/fortunate-leads/sidecar/laya_server.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/laya-sidecar.log</string>
  <key>StandardErrorPath</key><string>/tmp/laya-sidecar.log</string>
</dict></plist>
```

    launchctl load ~/Library/LaunchAgents/com.fortunate.laya.plist     # start
    launchctl unload ~/Library/LaunchAgents/com.fortunate.laya.plist   # stop

## Caveats

- Laya is zero-shot: you write the questions in plain English and there is no training step. The vendor says that
  both checkpoints ship uncalibrated. The main server uses `p` only as a ranking hint, not as a confidence guarantee or standalone tag.
- The multilingual checkpoint has a 1024-token per-question budget; the English checkpoint has 512. Long profile text may be truncated.
- Real checkpoint loading, package compatibility, device behavior and throughput still need a separately authorized live check. Offline fake-backend tests do not establish classification accuracy or performance.
