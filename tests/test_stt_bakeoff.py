"""Unit tests for the STT bakeoff harness.

These tests deliberately avoid any real network calls — provider clients
are exercised via httpx's `MockTransport` so the harness can be validated
in CI without live API keys.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from scripts.stt_bakeoff.metrics import aggregate, cer, normalize, wer
from scripts.stt_bakeoff.providers import (
    DeepgramNova3,
    OpenAIGpt4oTranscribe,
    SpeechmaticsEnhanced,
    TranscriptionResult,
)
from scripts.stt_bakeoff.run import (
    ManifestPathError,
    Row,
    build_providers,
    load_manifest,
    summarize,
    to_row,
    write_outputs,
)


# ── metrics ─────────────────────────────────────────────────────────────

def test_normalize_strips_punctuation_and_casing():
    assert normalize("Hey, Tedi!") == "hey tedi"
    assert normalize("  we're   here.  ") == "we're here"
    assert normalize("") == ""


def test_wer_exact_match_is_zero():
    assert wer("we dispatch trucks", "we dispatch trucks").rate == 0.0


def test_wer_one_substitution_in_three_words():
    r = wer("we dispatch trucks", "we dispatch vans")
    assert r.errors == 1
    assert r.ref_len == 3
    assert r.rate == pytest.approx(1 / 3)


def test_wer_insertion_and_deletion():
    r = wer("hello world", "hello there world")
    assert r.errors == 1
    r2 = wer("hello there world", "hello world")
    assert r2.errors == 1


def test_cer_counts_character_edits():
    r = cer("tedi", "teddy")  # insert 'd', substitute i->y → lev distance 2
    assert r.errors == 2
    assert r.ref_len == 4


def test_aggregate_is_micro_weighted():
    rates = [wer("a b", "x b"), wer("one two three four five six seven eight nine ten",
                                     "one two three X X X seven eight nine ten")]
    assert aggregate(rates) == pytest.approx(4 / 12)


# ── providers (mocked) ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deepgram_parses_transcript_from_nested_shape(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Token fake"
        assert request.headers["Content-Type"] == "audio/wav"
        assert request.url.params["model"] == "nova-3"
        return httpx.Response(200, json={
            "results": {"channels": [{"alternatives": [{"transcript": "hello world"}]}]}
        })

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("scripts.stt_bakeoff.providers.httpx.AsyncClient", patched)
    client = DeepgramNova3(api_key="fake")
    result = await client.transcribe(b"RIFF....", "audio/wav")
    assert result.provider == "deepgram"
    assert result.model == "nova-3"
    assert result.transcript == "hello world"
    assert result.error is None
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_deepgram_missing_key_returns_error_row():
    client = DeepgramNova3(api_key="")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.error and "DEEPGRAM_API_KEY" in result.error
    assert result.transcript == ""


@pytest.mark.asyncio
async def test_openai_parses_text_field(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake"
        return httpx.Response(200, json={"text": "dispatch trucks every morning"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("scripts.stt_bakeoff.providers.httpx.AsyncClient", patched)
    client = OpenAIGpt4oTranscribe(api_key="fake")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.transcript == "dispatch trucks every morning"
    assert result.error is None


@pytest.mark.asyncio
async def test_deepgram_returns_error_on_non_json_200(monkeypatch):
    # Deepgram has occasionally returned HTML error pages with status 200
    # behind some edge locations. The harness must not crash the run.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>",
                              headers={"content-type": "text/html"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("scripts.stt_bakeoff.providers.httpx.AsyncClient", patched)
    client = DeepgramNova3(api_key="fake")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.error is not None
    assert "invalid JSON body" in result.error
    assert "<html>oops</html>" in result.error
    assert result.transcript == ""


@pytest.mark.asyncio
async def test_deepgram_returns_error_on_unexpected_shape(monkeypatch):
    # 200 + valid JSON but no `results.channels[0].alternatives[0].transcript`.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": {"channels": []}})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("scripts.stt_bakeoff.providers.httpx.AsyncClient", patched)
    client = DeepgramNova3(api_key="fake")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.error is not None
    assert "unexpected response shape" in result.error


@pytest.mark.asyncio
async def test_openai_returns_error_on_non_json_200(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json-at-all",
                              headers={"content-type": "text/plain"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("scripts.stt_bakeoff.providers.httpx.AsyncClient", patched)
    client = OpenAIGpt4oTranscribe(api_key="fake")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.error is not None
    assert "invalid JSON body" in result.error
    assert result.transcript == ""


@pytest.mark.asyncio
async def test_openai_returns_error_on_missing_text_field(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"segments": [{"text": "hi"}]})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("scripts.stt_bakeoff.providers.httpx.AsyncClient", patched)
    client = OpenAIGpt4oTranscribe(api_key="fake")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.error is not None
    assert "unexpected response shape" in result.error


def _patch_httpx_transport(monkeypatch, handler, *, speed_up_sleep: bool = False):
    """Install an httpx.MockTransport that responds to every request via
    `handler`. Optionally replace `asyncio.sleep` with a no-op so polling
    loops (Speechmatics) don't stall the test."""
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("scripts.stt_bakeoff.providers.httpx.AsyncClient", patched)
    if speed_up_sleep:
        async def _no_sleep(_s: float) -> None:
            return None
        monkeypatch.setattr("scripts.stt_bakeoff.providers.asyncio.sleep", _no_sleep)


@pytest.mark.asyncio
async def test_speechmatics_submit_invalid_json_returns_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>",
                              headers={"content-type": "text/html"})

    _patch_httpx_transport(monkeypatch, handler)
    client = SpeechmaticsEnhanced(api_key="fake")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.error is not None
    assert result.error.startswith("submit: invalid JSON body")
    assert result.transcript == ""


@pytest.mark.asyncio
async def test_speechmatics_submit_missing_id_returns_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not_an_id": "whatever"})

    _patch_httpx_transport(monkeypatch, handler)
    client = SpeechmaticsEnhanced(api_key="fake")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.error is not None
    assert result.error.startswith("submit: unexpected response shape")


@pytest.mark.asyncio
async def test_speechmatics_status_invalid_json_returns_error(monkeypatch):
    # Submit succeeds, first status poll returns non-JSON.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job-42"})
        return httpx.Response(200, text="not-json",
                              headers={"content-type": "text/plain"})

    _patch_httpx_transport(monkeypatch, handler, speed_up_sleep=True)
    client = SpeechmaticsEnhanced(api_key="fake")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.error is not None
    assert result.error.startswith("status: invalid JSON body")


@pytest.mark.asyncio
async def test_speechmatics_status_missing_status_field_returns_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job-42"})
        return httpx.Response(200, json={"job": {"not_status": "running"}})

    _patch_httpx_transport(monkeypatch, handler, speed_up_sleep=True)
    client = SpeechmaticsEnhanced(api_key="fake")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.error is not None
    assert result.error.startswith("status: unexpected response shape")


@pytest.mark.asyncio
async def test_speechmatics_happy_path_returns_transcript(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job-42"})
        if request.url.path.endswith("/transcript"):
            return httpx.Response(200, text="hello tedi\n")
        return httpx.Response(200, json={"job": {"status": "done"}})

    _patch_httpx_transport(monkeypatch, handler, speed_up_sleep=True)
    client = SpeechmaticsEnhanced(api_key="fake")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.error is None
    assert result.transcript == "hello tedi"


@pytest.mark.asyncio
async def test_provider_surfaces_http_errors(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("scripts.stt_bakeoff.providers.httpx.AsyncClient", patched)
    client = DeepgramNova3(api_key="fake")
    result = await client.transcribe(b"x", "audio/wav")
    assert result.error is not None


# ── manifest loader ─────────────────────────────────────────────────────

def _write_audio(path: Path, data: bytes = b"RIFF....") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_load_manifest_reads_entries(tmp_path: Path):
    _write_audio(tmp_path / "clip_001.wav")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "\n".join([
            "# header comment — ignored",
            "",
            json.dumps({
                "audio": "clip_001.wav",
                "reference": "hello tedi",
                "duration_s": 1.5,
                "speaker_accent": "us_general",
            }),
        ]) + "\n"
    )
    entries = load_manifest(manifest)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.audio_id == "clip_001.wav"
    assert entry.reference == "hello tedi"
    assert entry.content_type == "audio/wav"
    assert entry.duration_s == 1.5
    assert entry.meta == {"speaker_accent": "us_general"}


def test_load_manifest_rejects_missing_audio(tmp_path: Path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps({"audio": "nope.wav", "reference": "hi"}) + "\n")
    with pytest.raises(FileNotFoundError):
        load_manifest(manifest)


def test_load_manifest_rejects_parent_traversal(tmp_path: Path):
    # Set up a sibling directory outside the corpus root that still exists
    # on disk — resolve() won't help if we didn't enforce containment.
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "outside.wav"
    _write_audio(outside)
    manifest = corpus / "manifest.jsonl"
    manifest.write_text(json.dumps({"audio": "../outside.wav", "reference": "x"}) + "\n")
    with pytest.raises(ManifestPathError, match="escapes corpus root"):
        load_manifest(manifest)


def test_load_manifest_rejects_absolute_path(tmp_path: Path):
    outside = tmp_path / "outside.wav"
    _write_audio(outside)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    manifest = corpus / "manifest.jsonl"
    manifest.write_text(
        json.dumps({"audio": str(outside.resolve()), "reference": "x"}) + "\n"
    )
    with pytest.raises(ManifestPathError, match="absolute audio path not allowed"):
        load_manifest(manifest)


def test_load_manifest_rejects_symlink_escape(tmp_path: Path):
    # Symlink inside corpus pointing outside it — resolve() will follow the
    # link; the containment check must still reject it.
    outside = tmp_path / "outside.wav"
    _write_audio(outside)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    link = corpus / "linked.wav"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks in this sandbox")
    manifest = corpus / "manifest.jsonl"
    manifest.write_text(json.dumps({"audio": "linked.wav", "reference": "x"}) + "\n")
    with pytest.raises(ManifestPathError, match="escapes corpus root"):
        load_manifest(manifest)


def test_load_manifest_honors_custom_corpus_root(tmp_path: Path):
    corpus = tmp_path / "audio_corpus"
    _write_audio(corpus / "clip_001.wav")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps({"audio": "clip_001.wav", "reference": "hi"}) + "\n"
    )
    entries = load_manifest(manifest, corpus_root=corpus)
    assert entries[0].audio_id == "clip_001.wav"


def test_load_manifest_preserves_distinct_subdir_paths(tmp_path: Path):
    # Same basename in two subdirs must stay distinguishable end-to-end.
    _write_audio(tmp_path / "a" / "clip_001.wav", b"aaaa")
    _write_audio(tmp_path / "b" / "clip_001.wav", b"bbbb")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("\n".join([
        json.dumps({"audio": "a/clip_001.wav", "reference": "alpha"}),
        json.dumps({"audio": "b/clip_001.wav", "reference": "bravo"}),
    ]) + "\n")
    entries = load_manifest(manifest)
    ids = [e.audio_id for e in entries]
    assert ids == ["a/clip_001.wav", "b/clip_001.wav"]
    # Rows derived from these entries must also retain the relative path.
    rows = [
        to_row(e, TranscriptionResult(
            provider="deepgram", model="nova-3",
            transcript=e.reference, latency_ms=1.0,
        ))
        for e in entries
    ]
    assert {r.audio for r in rows} == {"a/clip_001.wav", "b/clip_001.wav"}


# ── runner glue ─────────────────────────────────────────────────────────

def test_to_row_computes_metrics_and_cost(tmp_path: Path):
    from scripts.stt_bakeoff.run import ManifestEntry
    entry = ManifestEntry(
        audio_id="clip_001.wav",
        audio_path=tmp_path / "clip_001.wav",
        reference="we dispatch trucks",
        content_type="audio/wav",
        duration_s=6.0,  # 0.1 minutes
        meta={"speaker_accent": "us_general"},
    )
    result = TranscriptionResult(
        provider="deepgram", model="nova-3",
        transcript="we dispatch vans", latency_ms=320.0,
    )
    row = to_row(entry, result)
    assert row.audio == "clip_001.wav"
    assert row.wer == pytest.approx(1 / 3)
    assert row.ref_words == 3
    assert row.wer_errors == 1
    # cer: normalize → "we dispatch trucks" (18 chars incl. spaces) vs
    # "we dispatch vans" (16 chars). Metric is stable; just assert the
    # denominators are recorded and the errors count is positive.
    assert row.ref_chars == len("we dispatch trucks")
    assert row.cer_errors > 0
    assert row.accent == "us_general"
    assert row.realtime_factor == pytest.approx(0.32 / 6.0)
    # 0.1 minute * $0.0043/min = $0.00043
    assert row.est_cost_usd == pytest.approx(0.00043)


def _make_row(
    *, provider: str = "deepgram", audio: str = "clip_001.wav",
    ref: str = "we dispatch trucks", hyp: str | None = None,
    error: str | None = None, accent: str | None = "us_general",
    latency_ms: float = 200.0, duration_s: float = 6.0,
) -> Row:
    from scripts.stt_bakeoff.run import ManifestEntry
    entry = ManifestEntry(
        audio_id=audio, audio_path=Path(audio),
        reference=ref, content_type="audio/wav",
        duration_s=duration_s, meta={"speaker_accent": accent} if accent else {},
    )
    result = TranscriptionResult(
        provider=provider,
        model="nova-3" if provider == "deepgram" else "gpt-4o-transcribe",
        transcript=ref if hyp is None else hyp,
        latency_ms=latency_ms, error=error,
    )
    return to_row(entry, result)


def test_write_outputs_produces_summary(tmp_path: Path):
    rows = [
        _make_row(provider="deepgram", hyp="we dispatch trucks"),
        _make_row(provider="openai", hyp="we dispatch vans", latency_ms=900.0),
    ]
    write_outputs(rows, tmp_path)
    assert (tmp_path / "results.csv").exists()
    assert (tmp_path / "results.jsonl").exists()
    summary = (tmp_path / "summary.md").read_text()
    assert "deepgram" in summary and "openai" in summary
    assert "micro WER" in summary


def test_summary_micro_cer_uses_char_denominator():
    # Two clips with dramatically different reference lengths; the short
    # clip has a lot of character errors. With a correct char-level
    # aggregator the long clip dominates; with the old (word-denominator)
    # bug the corpus CER blew past 1.0.
    short_ref = "hi"                     # 2 chars
    long_ref = "we dispatch many trucks" # 23 chars
    short_hyp = "by"                     # 2 char subs → 2 errors
    long_hyp = long_ref                  # 0 errors
    rows = [
        _make_row(provider="deepgram", audio="clip_short.wav",
                  ref=short_ref, hyp=short_hyp, duration_s=1.0),
        _make_row(provider="deepgram", audio="clip_long.wav",
                  ref=long_ref, hyp=long_hyp, duration_s=4.0),
    ]
    assert rows[0].cer_errors == 2
    assert rows[0].ref_chars == 2
    assert rows[1].cer_errors == 0
    assert rows[1].ref_chars == len(long_ref)

    out = summarize(rows)
    # Expected corpus micro CER = 2 / (2 + 23) = 0.08
    assert "0.080" in out
    # Guard against the regression where the row stored the wer denominator
    # for cer, which would produce 2 / (1 + 4) = 0.400 here.
    assert "0.400" not in out


def test_summarize_handles_all_errored_provider():
    rows = [
        _make_row(provider="deepgram", ref="hi", hyp="",
                  error="DEEPGRAM_API_KEY not set", accent=None),
    ]
    out = summarize(rows)
    assert "deepgram" in out
    assert "n/a" in out


# ── speechmatics gate ───────────────────────────────────────────────────

def test_build_providers_rejects_speechmatics_without_flag():
    with pytest.raises(ValueError, match="--enable-speechmatics"):
        build_providers(["speechmatics"])


def test_build_providers_allows_speechmatics_with_flag():
    providers = build_providers(["speechmatics"], enable_speechmatics=True)
    assert len(providers) == 1
    assert providers[0].name == "speechmatics"


def test_build_providers_allows_non_gated_providers_without_flag():
    providers = build_providers(["deepgram", "openai"])
    assert [p.name for p in providers] == ["deepgram", "openai"]


def test_build_providers_unknown_name_errors():
    with pytest.raises(ValueError, match="unknown provider"):
        build_providers(["whisper-xl"])


class _FlakyProvider:
    """Minimal provider double that always raises — used to verify the
    runner wraps unexpected provider exceptions into an error row."""
    name = "flaky"
    model = "v0"

    async def transcribe(self, audio_bytes: bytes, content_type: str):
        raise RuntimeError("provider went boom")


class _OkProvider:
    name = "ok"
    model = "v1"

    async def transcribe(self, audio_bytes: bytes, content_type: str):
        return TranscriptionResult(
            provider=self.name, model=self.model,
            transcript="fine", latency_ms=1.0,
        )


@pytest.mark.asyncio
async def test_run_entry_survives_provider_exception(tmp_path: Path):
    from scripts.stt_bakeoff.run import ManifestEntry, run_entry

    _write_audio(tmp_path / "clip_001.wav")
    entry = ManifestEntry(
        audio_id="clip_001.wav",
        audio_path=tmp_path / "clip_001.wav",
        reference="anything",
        content_type="audio/wav",
        duration_s=1.0,
        meta={},
    )
    pairs = await run_entry(entry, [_FlakyProvider(), _OkProvider()])
    assert len(pairs) == 2
    flaky_result = next(r for _, r in pairs if r.provider == "flaky")
    ok_result = next(r for _, r in pairs if r.provider == "ok")
    assert flaky_result.error is not None
    assert "provider exception" in flaky_result.error
    assert "RuntimeError" in flaky_result.error
    # The sibling provider's result must be preserved — one bad provider
    # cannot abort the clip.
    assert ok_result.error is None
    assert ok_result.transcript == "fine"


def test_build_providers_rejects_empty_selection():
    # Regression: `--providers '   '` or `,,` used to parse to [] and the
    # runner would silently exit 0 after writing a zero-row summary.
    with pytest.raises(ValueError, match="no providers selected"):
        build_providers([])


def test_cli_exits_non_zero_on_whitespace_providers(tmp_path: Path, capsys):
    import asyncio
    import argparse
    from scripts.stt_bakeoff.run import main_async

    _write_audio(tmp_path / "clip_001.wav")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps({"audio": "clip_001.wav", "reference": "hi"}) + "\n"
    )
    args = argparse.Namespace(
        manifest=str(manifest),
        corpus_root=None,
        out_dir=str(tmp_path / "out"),
        providers="   ",  # whitespace-only — must not proceed
        enable_speechmatics=False,
    )
    rc = asyncio.run(main_async(args))
    assert rc == 2
    # The runner must not have written a misleading zero-row summary.
    assert not (tmp_path / "out").exists()
    err = capsys.readouterr().err
    assert "no providers selected" in err


if __name__ == "__main__":
    asyncio.run  # silence unused-import lint noise on direct execution
    raise SystemExit(pytest.main([__file__, "-v"]))
