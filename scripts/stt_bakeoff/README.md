# STT bakeoff harness

Offline A/B harness for comparing speech-to-text providers on real
Tedi-session audio. See `docs/stt-bakeoff.md` for the decision memo and
methodology.

## Quick start

1. Drop audio clips into `tests/fixtures/audio/`. That directory ships
   with a `.gitignore` that excludes everything except `.gitkeep` and
   `manifest.example.jsonl`, so real recordings and any local
   `manifest.jsonl` cannot be committed by accident. Only commit
   sanitized samples, and only after explicitly allowlisting them in the
   directory's `.gitignore`.
2. Create a manifest at `tests/fixtures/audio/manifest.jsonl`. Each line:

   ```json
   {"audio":"clip_001.wav","reference":"we dispatch trucks every morning","duration_s":3.8,"speaker_accent":"us_general"}
   ```

   See `manifest.example.jsonl` for the full field set.
3. Export API keys for whichever providers you want to run:

   ```sh
   export DEEPGRAM_API_KEY=...
   export OPENAI_API_KEY=...
   # Only if you're enabling Speechmatics:
   export SPEECHMATICS_API_KEY=...
   ```
4. Run the harness:

   ```sh
   python -m scripts.stt_bakeoff.run \
     --manifest tests/fixtures/audio/manifest.jsonl \
     --out-dir var/bakeoff \
     --providers deepgram,openai
   ```

   Speechmatics is gated — add `--enable-speechmatics` **and** include
   `speechmatics` in `--providers` to run it. Don't enable it unless the
   memo's §4.3 criterion is met.

   The corpus root defaults to the manifest's parent directory. Audio
   paths in the manifest must resolve inside that root — absolute paths
   and `..` escapes are rejected. Pass `--corpus-root PATH` to point at a
   different directory (the containment rule still applies).

5. Review the outputs under `var/bakeoff/`:
   - `results.jsonl` — one row per (clip × provider), keyed by the
     audio's relative path from the corpus root (so same-basename clips
     in different subdirs stay distinct)
   - `results.csv` — same, flattened for spreadsheets
   - `summary.md` — corpus-level micro WER/CER, p50/p95 latency, cost

## Adding a provider

Implement the `Provider` protocol in `providers.py` (an async `transcribe`
method returning `TranscriptionResult`) and register it in
`run.py::PROVIDERS`. Costs go in `run.py::COST_USD_PER_MINUTE`.
