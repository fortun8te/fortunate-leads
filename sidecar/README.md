# Laya decision sidecar (optional)

A local HTTP service that answers Fortunate's ideal-client questions about a profile, using
[Laya](https://huggingface.co/convaiinnovations/laya) from ConvAI Innovations. Laya is a ModernBERT
"System-1" decision model (Apache 2.0). The qualifier calls it when it is reachable and skips it otherwise.

## Setup (macOS)

    cd sidecar
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python laya_server.py      # the first run downloads about 1.7 GB to ~/.cache/huggingface

    curl -s 127.0.0.1:18742/health       # {"ok": true, "model": "convaiinnovations/laya", "device": "mps"}

Options:
- `--model convaiinnovations/laya:multilingual` loads the 322M mmBERT checkpoint, which handles 100+ languages. Use it for Dutch bios.
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

If you leave out `questions`, the server uses `questions.json`. Each item needs a `text` or a `person`.
A `person` is rendered as `Handle: @x\nName: ...\nCategory: ...\nBio: ...\nWebsite: ...\nFollowers: ...`.

The response looks like this:

```json
{"results": [{"id": "u1", "answers": {
   "dtc_founder": {"p": 0.93, "confidence": 0.81},
   "size":  {"p": 0.61, "label": "large", "probs": {"small": 0.39, "large": 0.61}, "confidence": 0.4},
   "niche": {"p": 0.82, "label": "skincare", "probs": {"skincare": 0.82, "food": 0.05}}}}],
 "model": "convaiinnovations/laya", "device": "mps", "ms": 812.3}
```

- A question without labels is a yes/no question (Laya `noul`), and `p` is P(yes).
- A question with labels is a single choice (Laya `choice`). `label` is the argmax and `p` is its probability.
- `multi: true` runs one yes/no question per label, because Laya has no native multi-label type. The `probs` values are therefore independent and do not sum to 1.
- Errors: 400 for a bad request, 500 when the model fails. The caller should treat both as "skip".

## Tests

    python3 -m unittest sidecar/test_laya.py   # stubbed model, no torch, no download

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
  base checkpoints ship over-confident and need temperature calibration before the probabilities can be trusted.
  On one out-of-domain benchmark it scored below the majority baseline zero-shot. Treat `p` as a soft signal
  to combine with other rules, not as a verdict.
- The English checkpoint has a 512-token context. Bios fit easily.
- On an x86 CPU with 4 cores, one item with one question took about 0.2 s. Five items with the full default set
  took about 25 s, because each question is its own model row and `niche` expands into 9 rows. MPS or CUDA is much faster.
