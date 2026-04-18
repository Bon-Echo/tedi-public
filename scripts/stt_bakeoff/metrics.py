"""Quality metrics for the STT bakeoff.

Word Error Rate (WER) and Character Error Rate (CER) are computed with a
small in-repo Levenshtein implementation — we intentionally avoid pulling
in `jiwer`/`Levenshtein` C extensions to keep the harness hermetic and
runnable in restricted environments.

Normalization follows conventions from Whisper / Deepgram evaluation
guides: lowercase, strip punctuation except apostrophes inside words, and
collapse whitespace. Spelling/phonetic variants (e.g. "Tedi" vs "Teddy")
are *not* normalized here — the memo explains why that is intentional.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_PUNCT_RE = re.compile(r"[^\w\s']", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _levenshtein(ref: list[str], hyp: list[str]) -> int:
    # O(len(ref) * len(hyp)) time, O(len(hyp)) space.
    if not ref:
        return len(hyp)
    if not hyp:
        return len(ref)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        curr = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            cost = 0 if r == h else 1
            curr[j] = min(
                curr[j - 1] + 1,      # insertion
                prev[j] + 1,          # deletion
                prev[j - 1] + cost,   # substitution
            )
        prev = curr
    return prev[-1]


@dataclass(frozen=True)
class ErrorRate:
    errors: int
    ref_len: int

    @property
    def rate(self) -> float:
        return self.errors / self.ref_len if self.ref_len else 0.0


def wer(reference: str, hypothesis: str) -> ErrorRate:
    ref_tokens = normalize(reference).split()
    hyp_tokens = normalize(hypothesis).split()
    return ErrorRate(_levenshtein(ref_tokens, hyp_tokens), len(ref_tokens))


def cer(reference: str, hypothesis: str) -> ErrorRate:
    ref_chars = list(normalize(reference))
    hyp_chars = list(normalize(hypothesis))
    return ErrorRate(_levenshtein(ref_chars, hyp_chars), len(ref_chars))


def aggregate(rates: list[ErrorRate]) -> float:
    """Corpus-level error rate — sum of errors divided by sum of ref lengths.

    This matches the industry-standard "micro" WER used by Deepgram's public
    benchmarks and the Whisper paper. Averaging per-utterance rates biases
    toward short utterances; don't do that.
    """
    total_errors = sum(r.errors for r in rates)
    total_ref = sum(r.ref_len for r in rates)
    return total_errors / total_ref if total_ref else 0.0
