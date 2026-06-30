"""Signal 1 — Stylometric heuristics (local, deterministic).

Estimates the probability that a piece of text is AI-generated using three
model-free, structural sub-metrics (see planning.md S1, Signal 1):

  * Burstiness          — coefficient of variation of sentence lengths.
                          Humans alternate long and terse sentences (high CV);
                          models are metronomic (low CV).
  * Lexical diversity   — length-normalized type/token ratio. AI reuses a
                          smaller, safer vocabulary (lower diversity).
  * Connective density  — rate of polished transitions/hedges per 100 words.
                          AI over-produces these.

Output: a single probability-of-AI in [0, 1]. Higher = more likely AI.
Fully deterministic and reproducible — no randomness, no network.
"""

import re
import statistics

# --- Tunable reference points -------------------------------------------------
# Each reference marks the value at which a sub-metric reads as "clearly AI"
# (score -> 1.0) or "clearly human" (score -> 0.0). They are intentionally
# simple and transparent rather than tuned to a benchmark (planning.md S"Decisions").

_BURSTINESS_REF = 0.80   # CV at/above which sentence variation reads fully human
_DIVERSITY_REF = 0.70    # MATTR at/above which vocabulary reads fully human
_CONNECTIVE_REF = 4.0    # connectives per 100 words at which density reads fully AI
_MATTR_WINDOW = 50       # token window for length-normalized lexical diversity

# Equal weighting keeps the blend transparent and easy to reason about.
_WEIGHTS = (1 / 3, 1 / 3, 1 / 3)

# Polished transitions / hedging phrases that AI over-produces. Longer phrases
# are listed so they can be matched before their single-word substrings.
_CONNECTIVES = (
    "it's important to note",
    "it is important to note",
    "it's worth noting",
    "it is worth noting",
    "on the other hand",
    "as a result",
    "in conclusion",
    "in summary",
    "to summarize",
    "in addition",
    "for instance",
    "for example",
    "moreover",
    "furthermore",
    "additionally",
    "however",
    "therefore",
    "consequently",
    "nevertheless",
    "nonetheless",
    "thus",
    "notably",
    "importantly",
    "ultimately",
    "overall",
    "delve",
)


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _split_sentences(text):
    """Split into sentences on terminal punctuation; drop empties."""
    parts = re.split(r"[.!?]+", text)
    return [p.strip() for p in parts if p.strip()]


def _tokens(text):
    """Lowercased word tokens (alphanumeric/apostrophe runs)."""
    return re.findall(r"[a-z0-9']+", text.lower())


def burstiness(text):
    """Coefficient of variation (stdev/mean) of sentence word-lengths."""
    sentences = _split_sentences(text)
    lengths = [len(_tokens(s)) for s in sentences]
    lengths = [n for n in lengths if n > 0]
    if len(lengths) < 2:
        return None  # need >= 2 sentences for variance to mean anything
    mean = statistics.mean(lengths)
    if mean == 0:
        return None
    return statistics.pstdev(lengths) / mean


def lexical_diversity(text):
    """Length-normalized type/token ratio via moving-average TTR (MATTR).

    Plain TTR sinks as texts grow longer; MATTR averages the TTR over a sliding
    window so the measure stays comparable across lengths.
    """
    toks = _tokens(text)
    if not toks:
        return None
    if len(toks) <= _MATTR_WINDOW:
        return len(set(toks)) / len(toks)
    ratios = []
    for i in range(len(toks) - _MATTR_WINDOW + 1):
        window = toks[i:i + _MATTR_WINDOW]
        ratios.append(len(set(window)) / _MATTR_WINDOW)
    return statistics.mean(ratios)


def connective_density(text):
    """Connective/hedging phrases per 100 words."""
    toks = _tokens(text)
    if not toks:
        return None
    lowered = text.lower()
    hits = sum(lowered.count(phrase) for phrase in _CONNECTIVES)
    return hits / len(toks) * 100


def score_stylometric(text):
    """Return p_style in [0, 1]: estimated probability the text is AI-generated.

    Higher = more likely AI. Empty/whitespace input returns the neutral 0.5;
    sub-metrics that cannot be computed (too short to estimate) are skipped and
    the remaining ones are re-weighted, so the function always returns a value
    in [0, 1].
    """
    if not text or not text.strip():
        return 0.5

    cv = burstiness(text)
    div = lexical_diversity(text)
    dens = connective_density(text)

    # Map each sub-metric to a probability-of-AI contribution in [0, 1].
    sub_scores = []
    weights = []

    if cv is not None:
        # Low variation -> AI. High variation (>= ref) -> human.
        sub_scores.append(_clamp(1 - cv / _BURSTINESS_REF))
        weights.append(_WEIGHTS[0])

    if div is not None:
        # Low diversity -> AI. High diversity (>= ref) -> human.
        sub_scores.append(_clamp(1 - div / _DIVERSITY_REF))
        weights.append(_WEIGHTS[1])

    if dens is not None:
        # High connective density -> AI.
        sub_scores.append(_clamp(dens / _CONNECTIVE_REF))
        weights.append(_WEIGHTS[2])

    if not sub_scores:
        return 0.5

    total_w = sum(weights)
    p_style = sum(s * w for s, w in zip(sub_scores, weights)) / total_w
    return _clamp(p_style)
