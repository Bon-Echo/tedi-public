# Tedi STT bakeoff — decision memo

_Status: draft with a blocking gap. The harness is in-repo and tested, but
no scored numbers are available yet because the production Tedi path never
captures server-side audio. Fix the capture gap first, then populate the
results tables below by running `python -m scripts.stt_bakeoff.run`._

## 1. Current transcription path

Browser-side only. The live path is:

1. `static/room.js:452-493` creates a `webkitSpeechRecognition` /
   `SpeechRecognition` instance — i.e. the browser's built-in Web Speech
   API. Chrome proxies this through Google's cloud STT; Safari uses
   on-device ASR. Firefox returns nothing.
2. On every finalized segment (`recognition.onresult` with `result.isFinal`)
   the browser sends `{"type":"speech_final","transcript":"..."}` over the
   session WebSocket (`static/room.js:472`).
3. The server receives that message at `app/routers/ws.py:95-105` and
   forwards the string into `orchestrator.on_speech_final(...)`.

### Instrumentation that already exists

- **Structured WS log** (`app/routers/ws.py:98`): `ws_speech_final`
  records the first 80 chars of every accepted transcript.
- **Turn persistence** (`app/services/turn_persistence.py`) fire-and-forget
  inserts `user` and `agent` turns into `session_turns` for the admin UI.
- **System prompt workaround** (`prompts/discovery_system.txt:23`):
  explicitly tells the model to tolerate common STT mishearings of "Tedi"
  (e.g. "Teddy", "Daddy", "Telly") without correcting the user. This is a
  self-documenting signal that STT quality is already known to be a drag
  on session quality.

### Instrumentation that does **not** exist

- **No server-side audio.** The browser never ships raw audio to the
  server — VAD (`room.js:408-446`) exists but is only used to drive the
  orb visualization and speech recognition start/stop. `MediaRecorder` is
  never instantiated.
- **No STT provider code on the production path.** Nothing under `app/`
  imports `deepgram`, `openai`, `whisper`, or any STT SDK; the running
  service does not call a hosted transcriber. The repo has exactly one
  provider-adjacent production reference:
  `infrastructure/terraform/variables.tf:94` and
  `infrastructure/terraform/locals.tf:21` declare and inject
  `DEEPGRAM_API_KEY` into the ECS task env, but no application code
  reads it. The offline harness introduced by this change
  (`scripts/stt_bakeoff/providers.py`) does import and call Deepgram +
  OpenAI, but it lives outside `app/`, is never imported from the
  service, and runs only when invoked manually via its CLI — so
  production behavior is unchanged.
- **No quality signal.** `session_turns.text` is the already-decoded
  Web Speech string — there is no ground truth to compare against, so the
  existing persistence cannot retroactively score the current STT.

## 2. The blocking gap

> **To run a bakeoff on real user-spoken audio you need real user audio,
> and the production pipeline currently captures none.**

Two ways to close this — pick one before running the harness:

### Option A — collect audio out-of-band (fastest; no prod change)

Record 15–30 clips (5–15 s each) from representative speakers reading a
scripted mix of Tedi-style utterances: a short greeting, a business
description, a numeric answer ("we run forty-two trucks"), and the
brand-name stressor ("hey Tedi"/"thanks Tedi"). Have each speaker wear a
close-mic (AirPods / laptop mic) so audio quality is representative of
what the browser would otherwise have captured. Transcribe them by hand
for the reference column.

Target speaker coverage that matches Tedi's actual customer base — at
minimum: one US-General accent, one South-Asian English accent, one
non-native English accent from a region with active pipeline interest.
Aim for 30+ utterances total so corpus WER is not dominated by a single
bad clip.

### Option B — add audio capture to room.js behind a feature flag

Minimal patch (~30 lines, not included in this PR to keep scope tight):

1. In `static/room.js::startSpeechRecognition`, when a `?record=1` URL
   param is present, also open a `MediaRecorder(vadStream, {mimeType:
   'audio/webm;codecs=opus'})`.
2. On PTT release (`stopSpeechRecognition`), finalize the blob and `POST`
   it to a new `/internal/audio-upload` endpoint, keyed by `call_id` + a
   sequential `turn_seq` and paired with the already-transmitted
   `speech_final` string (which becomes the "candidate A" transcript).
3. Server side: write to an S3 prefix with a short TTL and a separate
   IAM scope. Do **not** enable this by default.

Option B gives you paired audio + Web-Speech-API transcript, which is the
cleanest setup for the bakeoff: you can score the three candidates
against human-corrected truth **and** directly measure the delta vs. the
current browser STT on the same utterances.

## 3. What the harness does

`scripts/stt_bakeoff/` is a standalone, provider-agnostic evaluator. Key
design choices:

- **Async, parallel per-clip calls.** Every provider sees the same bytes
  for a given clip; we do not let one provider's latency inflate another's.
- **Thin REST clients, not SDKs.** Keeps dependency surface minimal and
  makes the code easy to audit for what's actually sent (headers, params).
  httpx is already pinned in `requirements.txt`.
- **Micro-WER and micro-CER corpus aggregation** (`metrics.aggregate`) —
  each row stores integer edit counts and the per-row reference length
  (words for WER, characters for CER) so corpus-level rates are computed
  as `sum(errors) / sum(ref_len)`. Deepgram's public benchmarks and the
  Whisper paper both report micro rates; mean-of-means is misleading on
  short utterances.
- **Stable per-clip ID.** Each row's `audio` column is the clip's
  POSIX-relative path under the corpus root, so two clips named
  `clip_001.wav` in different subdirectories remain distinguishable end
  to end.
- **Path-containment check.** `load_manifest` rejects absolute paths and
  any audio reference that resolves (via `..` or symlinks) outside the
  corpus root. The corpus root defaults to the manifest's parent; pass
  `--corpus-root` to override.
- **Speechmatics is gated.** The CLI refuses to run `speechmatics` unless
  `--enable-speechmatics` is passed. The flag mirrors the §4.3 criterion
  below so the team has a deliberate opt-in step, not a dropdown.
- **Normalization** lowercases, strips punctuation (except intra-word
  apostrophes), NFKC-normalizes, and collapses whitespace. We
  intentionally do **not** normalize spelling variants of "Tedi"/"Teddy" —
  the bakeoff must surface that distinction so we can decide whether to
  (a) rely on a provider that gets it right or (b) keep the prompt-level
  tolerance workaround.
- **Cost estimates** use published per-minute list prices stored in
  `run.py::COST_USD_PER_MINUTE`. These are point-in-time — re-check before
  quoting in any forward-looking plan.

### Files added

| path | purpose |
|---|---|
| `scripts/stt_bakeoff/__init__.py` | package marker |
| `scripts/stt_bakeoff/metrics.py` | WER/CER, normalization, micro aggregation |
| `scripts/stt_bakeoff/providers.py` | Deepgram Nova-3, OpenAI gpt-4o-transcribe, optional Speechmatics |
| `scripts/stt_bakeoff/run.py` | CLI: load manifest, run providers, emit `results.{jsonl,csv}` + `summary.md` |
| `scripts/stt_bakeoff/README.md` | operator runbook |
| `tests/fixtures/audio/manifest.example.jsonl` | manifest schema example |
| `tests/fixtures/audio/.gitkeep` | reserved directory for real clips |
| `tests/fixtures/audio/.gitignore` | blocks accidental commits of real recordings / local manifests |
| `tests/test_stt_bakeoff.py` | unit tests: metrics, mocked provider roundtrip for Deepgram, OpenAI, and Speechmatics (happy path + malformed / unexpected-shape / polling failure modes), runner-level exception tolerance, runner glue, manifest path-safety, Speechmatics gate |
| `docs/stt-bakeoff.md` | this memo |

## 4. Candidates, in priority order

### 4.1 Deepgram Nova-3 (primary candidate)

- **Why first**: published English WER is leading among batch providers
  for conversational speech, and Deepgram is the only provider already
  partway-wired into our infra (`DEEPGRAM_API_KEY` in Terraform).
- **Latency profile**: sub-realtime for prerecorded; supports WebSocket
  streaming at ~300 ms added latency for interim results — matches our
  push-to-talk UX.
- **Published list price** (2026-04): $0.0043/min prerecorded,
  $0.0077/min streaming nova-3 (re-verify at pricing.deepgram.com
  before quoting).
- **Bakeoff command**:
  ```sh
  python -m scripts.stt_bakeoff.run \
    --manifest tests/fixtures/audio/manifest.jsonl \
    --providers deepgram
  ```

### 4.2 OpenAI gpt-4o-transcribe

- **Why second**: GPT-4o-transcribe handles heavy accents and casual
  speech notably better than Whisper-v3 in OpenAI's own benchmarks, and
  it's plausibly the most robust option for the non-US-accent slice.
- **Latency profile**: batch-only today; realtime API exists
  (`gpt-4o-realtime-preview`) but with a different commercial model. For
  the initial bakeoff we use the batch endpoint only — streaming is a
  later evaluation.
- **Published list price** (2026-04): ~$0.006/min audio input on the
  batch endpoint (re-verify at openai.com/api/pricing before quoting).
- **Bakeoff command**:
  ```sh
  python -m scripts.stt_bakeoff.run \
    --manifest tests/fixtures/audio/manifest.jsonl \
    --providers openai
  ```

### 4.3 Speechmatics Enhanced (conditional)

Only add this candidate if, after running the first two, **accent
robustness is the measurable root cause** of the remaining WER gap (i.e.
non-US-General slices dominate the error budget and neither Deepgram
Nova-3 nor gpt-4o-transcribe is within tolerance on those slices).
Speechmatics advertises strong accent coverage but their list price is
~2–4× the cheaper candidates and onboarding adds a polling batch loop
(see `SpeechmaticsEnhanced` in `providers.py`).

Evidence required before enabling:
- corpus micro-WER on accented slice > 15% for **both** Deepgram and
  OpenAI; and
- the failures are phonetically plausible accent errors, not mic/noise
  issues, confirmed by listening to the worst 5 clips.

To enable, pass both the provider name and the explicit `--enable-speechmatics`
flag (the runner refuses to launch Speechmatics without it):
```sh
export SPEECHMATICS_API_KEY=...
python -m scripts.stt_bakeoff.run \
  --manifest tests/fixtures/audio/manifest.jsonl \
  --providers deepgram,openai,speechmatics \
  --enable-speechmatics
```

## 5. Scoring rubric

Four dimensions, weighted by how often they bite Tedi's session quality:

| dimension | measure | weight | notes |
|---|---|---|---|
| Quality | corpus micro-WER on real-user clips | 0.45 | single biggest driver of agent misunderstanding |
| Quality (brand) | exact-match rate of "Tedi" across clips containing it | 0.10 | directly related to the prompt-level workaround |
| Reliability | error-row rate (HTTP failure, timeout, empty transcript) | 0.15 | at PTT scale a 1% failure rate is user-visible |
| Speed | p95 latency on clips ≤ 10 s | 0.20 | PTT UX budget is ~600 ms to "thinking" state |
| Cost | USD per session at 40 turns × 5 s avg | 0.10 | bounded — any candidate under ~$0.02/session is acceptable |

Decision thresholds (apply after running the harness):

- **Ship if** a candidate's weighted score beats Web-Speech-API baseline
  by ≥ 25% AND its worst per-accent slice WER ≤ 12%.
- **Re-open** if two candidates tie within 5% — decide on operational
  factors (existing Terraform wiring favors Deepgram; SDK ergonomics and
  in-house OpenAI billing favor gpt-4o-transcribe).

## 6. Results

_This section is intentionally empty._ It will be populated by running
the harness once audio fixtures + API keys are in place. The harness
writes `summary.md` in a format that can be pasted directly below.

```
<!-- paste var/bakeoff/summary.md here after running -->
```

## 7. Next steps (exact commands)

1. Decide between **Option A** (out-of-band recording) and **Option B**
   (room.js audio-capture patch). Option A is faster to pilot.
2. Populate `tests/fixtures/audio/` with ≥ 30 clips and the matching
   `manifest.jsonl`. The harness only reads clips from the local corpus
   root (the manifest's parent by default, or `--corpus-root PATH`) —
   there is no remote-fetch path, pre-signed or otherwise. To keep clips
   off an engineer's dev laptop, stage them in S3 and `aws s3 sync` to a
   local corpus directory before running. Do **not** commit
   customer-identifying audio; the `tests/fixtures/audio/.gitignore`
   added in this change blocks that by default.
3. Export `DEEPGRAM_API_KEY` and `OPENAI_API_KEY`.
4. Run:
   ```sh
   python -m scripts.stt_bakeoff.run \
     --manifest tests/fixtures/audio/manifest.jsonl \
     --out-dir var/bakeoff \
     --providers deepgram,openai
   ```
5. Paste `var/bakeoff/summary.md` into section 6 of this memo.
6. Only if the results justify it (per §4.3), re-run with
   `--providers deepgram,openai,speechmatics` and update the tables.
7. Write the production integration ticket: feature-flag a server-side
   STT service behind a new `app/services/stt.py` module, wired into the
   same WS message that `room.js` already sends. Keep Web Speech API as
   the default fallback until the chosen provider has shipped with
   observability for a week.
