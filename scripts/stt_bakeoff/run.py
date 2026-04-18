"""CLI runner for the Tedi STT bakeoff.

Usage
-----
    python -m scripts.stt_bakeoff.run \\
        --manifest tests/fixtures/audio/manifest.jsonl \\
        --out-dir var/bakeoff \\
        --providers deepgram,openai

Manifest format (JSONL — one object per line)
---------------------------------------------
    {
      "audio":   "clip_001.wav",       # path relative to corpus root (see below)
      "reference": "we dispatch trucks every morning",
      "content_type": "audio/wav",     # optional; inferred from ext if absent
      "speaker_accent": "south_asian", # optional metadata used for slicing
      "duration_s": 4.2                # optional; used for realtime factor
    }

Corpus root and path safety
---------------------------
By default the corpus root is the manifest's parent directory. All audio
paths in the manifest are treated as relative to that root; absolute paths
and any path that resolves outside the root (e.g. via `..`, symlinks) are
rejected. Override with `--corpus-root` if you want a different root, but
the same containment rule always applies.

Outputs
-------
    {out_dir}/results.jsonl     — per (audio, provider) raw result
    {out_dir}/results.csv       — flat spreadsheet for reviewers
    {out_dir}/summary.md        — corpus-level WER/CER/latency/cost per provider

The runner refuses to fabricate numbers: if a provider's API key is missing
or a request fails, the row is still emitted with the error string so the
memo can show exactly which cells were unreachable.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Make the repo root importable when the script is executed directly.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.stt_bakeoff.metrics import ErrorRate, aggregate, cer, wer  # noqa: E402
from scripts.stt_bakeoff.providers import (  # noqa: E402
    DeepgramNova3,
    OpenAIGpt4oTranscribe,
    Provider,
    SpeechmaticsEnhanced,
    TranscriptionResult,
)

# Published list prices in USD / minute of audio, as of model release.
# Source each number from the provider's pricing page before committing
# any cost claim in the decision memo.
COST_USD_PER_MINUTE = {
    ("deepgram", "nova-3"): 0.0043,          # https://deepgram.com/pricing (pay-as-you-go)
    ("openai", "gpt-4o-transcribe"): 0.006,  # https://openai.com/api/pricing/ (audio in)
    ("speechmatics", "enhanced"): 0.0167,    # https://www.speechmatics.com/pricing (est.)
}

_EXT_TO_CONTENT_TYPE = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}

PROVIDERS: dict[str, type[Provider]] = {
    "deepgram": DeepgramNova3,
    "openai": OpenAIGpt4oTranscribe,
    "speechmatics": SpeechmaticsEnhanced,
}

# Speechmatics is gated behind an explicit CLI flag because the product
# requirement is to add it only if accent robustness is the measurable
# root cause (see docs/stt-bakeoff.md §4.3).
GATED_PROVIDERS = frozenset({"speechmatics"})


class ManifestPathError(ValueError):
    """Raised when a manifest references an audio path that escapes the
    corpus root or is otherwise unsafe."""


@dataclass
class ManifestEntry:
    audio_id: str              # stable POSIX-style relpath from corpus root
    audio_path: Path           # absolute resolved path on disk
    reference: str
    content_type: str
    duration_s: float | None
    meta: dict


@dataclass
class Row:
    audio: str                 # relative path (stable ID — does NOT collapse same basenames)
    provider: str
    model: str
    reference: str
    hypothesis: str
    wer: float
    cer: float
    ref_words: int             # normalized reference word count (WER denominator)
    ref_chars: int             # normalized reference character count (CER denominator)
    wer_errors: int            # integer edit count — used for correct corpus aggregation
    cer_errors: int            # integer edit count — used for correct corpus aggregation
    latency_ms: float
    realtime_factor: float | None
    est_cost_usd: float | None
    error: str | None
    accent: str | None


def _resolve_audio_path(raw: str, corpus_root: Path, *, lineno: int,
                        manifest_path: Path) -> tuple[Path, str]:
    """Resolve a manifest audio reference under `corpus_root`.

    Returns (absolute_path, posix_relative_id). Raises ManifestPathError if
    the reference is absolute or would resolve outside the corpus root.
    """
    raw_path = Path(raw)
    if raw_path.is_absolute():
        raise ManifestPathError(
            f"{manifest_path}:{lineno}: absolute audio path not allowed: {raw!r}"
        )
    candidate = (corpus_root / raw_path).resolve()
    try:
        rel = candidate.relative_to(corpus_root)
    except ValueError as e:
        raise ManifestPathError(
            f"{manifest_path}:{lineno}: audio path escapes corpus root "
            f"{str(corpus_root)!r}: {raw!r}"
        ) from e
    # Posix form keeps the ID stable across OSes and in CSV output.
    return candidate, rel.as_posix()


def load_manifest(
    manifest_path: Path, *, corpus_root: Path | None = None
) -> list[ManifestEntry]:
    manifest_path = manifest_path.resolve()
    root = (corpus_root or manifest_path.parent).resolve()
    entries: list[ManifestEntry] = []
    with manifest_path.open() as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            obj = json.loads(line)
            raw = obj["audio"]
            audio_path, audio_id = _resolve_audio_path(
                raw, root, lineno=lineno, manifest_path=manifest_path
            )
            if not audio_path.exists():
                raise FileNotFoundError(
                    f"{manifest_path}:{lineno}: audio file not found: {audio_path}"
                )
            content_type = obj.get("content_type") or _EXT_TO_CONTENT_TYPE.get(
                audio_path.suffix.lower(), "application/octet-stream"
            )
            entries.append(
                ManifestEntry(
                    audio_id=audio_id,
                    audio_path=audio_path,
                    reference=obj["reference"],
                    content_type=content_type,
                    duration_s=obj.get("duration_s"),
                    meta={k: v for k, v in obj.items()
                          if k not in {"audio", "reference", "content_type", "duration_s"}},
                )
            )
    return entries


def build_providers(
    names: list[str], *, enable_speechmatics: bool = False
) -> list[Provider]:
    if not names:
        raise ValueError(
            "no providers selected; pass --providers with at least one of "
            f"{sorted(PROVIDERS)}"
        )
    out: list[Provider] = []
    for name in names:
        cls = PROVIDERS.get(name)
        if cls is None:
            raise ValueError(
                f"unknown provider {name!r}; choose from {sorted(PROVIDERS)}"
            )
        if name in GATED_PROVIDERS:
            if name == "speechmatics" and not enable_speechmatics:
                raise ValueError(
                    "speechmatics is gated: pass --enable-speechmatics to run it. "
                    "Only add this candidate if accent robustness is the measurable "
                    "root cause (see docs/stt-bakeoff.md §4.3)."
                )
        out.append(cls())
    return out


async def _safe_transcribe(
    provider: Provider, audio_bytes: bytes, content_type: str
) -> TranscriptionResult:
    """Invoke a provider and convert any unexpected exception into an error
    row. Provider clients own their normal error paths (missing API key,
    HTTP failure, malformed JSON); this wrapper is the last line of defence
    so that one misbehaving provider never aborts the whole clip or run.
    """
    try:
        return await provider.transcribe(audio_bytes, content_type)
    except Exception as e:  # noqa: BLE001 — deliberately broad
        return TranscriptionResult(
            provider=provider.name,
            model=provider.model,
            transcript="",
            latency_ms=0.0,
            error=f"provider exception: {type(e).__name__}: {e}",
        )


async def run_entry(
    entry: ManifestEntry, providers: list[Provider]
) -> list[tuple[ManifestEntry, TranscriptionResult]]:
    audio_bytes = entry.audio_path.read_bytes()
    results = await asyncio.gather(
        *(_safe_transcribe(p, audio_bytes, entry.content_type) for p in providers)
    )
    return [(entry, r) for r in results]


def summarize(rows: list[Row]) -> str:
    by_provider: dict[str, list[Row]] = {}
    for r in rows:
        by_provider.setdefault(r.provider, []).append(r)
    lines = ["# STT bakeoff summary", ""]
    lines.append(f"- clips: {len({r.audio for r in rows})}")
    lines.append(f"- providers: {', '.join(sorted(by_provider))}")
    lines.append("")
    lines.append("| provider | model | clips | micro WER | micro CER | "
                 "p50 latency ms | p95 latency ms | avg cost/clip (USD) | errors |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for provider, group in sorted(by_provider.items()):
        model = group[0].model
        clip_count = len(group)
        ok = [r for r in group if r.error is None]
        error_count = clip_count - len(ok)
        if not ok:
            lines.append(
                f"| {provider} | {model} | {clip_count} | n/a | n/a | n/a | n/a | n/a | {error_count} |"
            )
            continue
        micro_wer = aggregate(
            [ErrorRate(errors=r.wer_errors, ref_len=r.ref_words) for r in ok]
        )
        micro_cer = aggregate(
            [ErrorRate(errors=r.cer_errors, ref_len=r.ref_chars) for r in ok]
        )
        latencies = sorted(r.latency_ms for r in ok)
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        costs = [r.est_cost_usd for r in ok if r.est_cost_usd is not None]
        avg_cost = f"{sum(costs)/len(costs):.5f}" if costs else "n/a"
        lines.append(
            f"| {provider} | {model} | {clip_count} | "
            f"{micro_wer:.3f} | {micro_cer:.3f} | "
            f"{p50:.0f} | {p95:.0f} | {avg_cost} | {error_count} |"
        )
    lines.append("")
    lines.append("Per-accent slice (if accent labels present):")
    lines.append("")
    accents = sorted({r.accent for r in rows if r.accent})
    if not accents:
        lines.append("_no accent labels in manifest — skipping slice_")
    else:
        lines.append("| provider | accent | clips | micro WER |")
        lines.append("|---|---|---|---|")
        for provider, group in sorted(by_provider.items()):
            for accent in accents:
                slice_rows = [r for r in group if r.accent == accent and r.error is None]
                if not slice_rows:
                    continue
                micro = aggregate(
                    [ErrorRate(errors=r.wer_errors, ref_len=r.ref_words) for r in slice_rows]
                )
                lines.append(
                    f"| {provider} | {accent} | {len(slice_rows)} | {micro:.3f} |"
                )
    return "\n".join(lines) + "\n"


def to_row(entry: ManifestEntry, result: TranscriptionResult) -> Row:
    w = wer(entry.reference, result.transcript)
    c = cer(entry.reference, result.transcript)
    rtf = None
    if entry.duration_s and entry.duration_s > 0:
        rtf = (result.latency_ms / 1000.0) / entry.duration_s
    cost = None
    if entry.duration_s is not None:
        per_min = COST_USD_PER_MINUTE.get((result.provider, result.model))
        if per_min is not None:
            cost = (entry.duration_s / 60.0) * per_min
    return Row(
        audio=entry.audio_id,
        provider=result.provider,
        model=result.model,
        reference=entry.reference,
        hypothesis=result.transcript,
        wer=w.rate,
        cer=c.rate,
        ref_words=w.ref_len,
        ref_chars=c.ref_len,
        wer_errors=w.errors,
        cer_errors=c.errors,
        latency_ms=result.latency_ms,
        realtime_factor=rtf,
        est_cost_usd=cost,
        error=result.error,
        accent=entry.meta.get("speaker_accent"),
    )


def write_outputs(rows: list[Row], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(asdict(r)) for r in rows) + "\n"
    )
    with (out_dir / "results.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()) if rows else [])
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))
    (out_dir / "summary.md").write_text(summarize(rows))


async def main_async(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        print(f"manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    corpus_root = Path(args.corpus_root).resolve() if args.corpus_root else None
    try:
        entries = load_manifest(manifest_path, corpus_root=corpus_root)
    except ManifestPathError as e:
        print(f"manifest path error: {e}", file=sys.stderr)
        return 2
    if not entries:
        print("manifest is empty — nothing to evaluate", file=sys.stderr)
        return 2
    provider_names = [n.strip() for n in args.providers.split(",") if n.strip()]
    try:
        providers = build_providers(
            provider_names, enable_speechmatics=args.enable_speechmatics
        )
    except ValueError as e:
        print(f"provider config error: {e}", file=sys.stderr)
        return 2
    rows: list[Row] = []
    for i, entry in enumerate(entries, start=1):
        print(
            f"[{i}/{len(entries)}] {entry.audio_id} "
            f"({len(providers)} providers)",
            file=sys.stderr,
        )
        pairs = await run_entry(entry, providers)
        for e, result in pairs:
            rows.append(to_row(e, result))
    out_dir = Path(args.out_dir).resolve()
    write_outputs(rows, out_dir)
    print(f"wrote {len(rows)} rows to {out_dir}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Tedi STT bakeoff runner")
    parser.add_argument(
        "--manifest", required=True,
        help="Path to a JSONL manifest file (see module docstring)",
    )
    parser.add_argument(
        "--corpus-root", default=None,
        help="Root directory that audio paths are resolved against. "
             "Defaults to the manifest's parent directory. Paths that "
             "resolve outside this root are rejected.",
    )
    parser.add_argument(
        "--out-dir", default="var/bakeoff",
        help="Directory to write results.{jsonl,csv} and summary.md",
    )
    parser.add_argument(
        "--providers", default="deepgram,openai",
        help="Comma-separated provider names. Choices: "
             f"{','.join(sorted(PROVIDERS))}",
    )
    parser.add_argument(
        "--enable-speechmatics", action="store_true",
        help="Explicit opt-in required to run the speechmatics provider. "
             "Only add this candidate if accent robustness is the measurable "
             "root cause (see docs/stt-bakeoff.md §4.3).",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
