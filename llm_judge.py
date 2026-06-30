"""Signal 2 — Groq LLM judge (planning.md §1, Signal 2).

Reads meaning and texture rather than form: cliché density, generic "helpful
assistant" framing, absence of lived specificity, suspiciously even emotional
tone — semantic cues the stylometric statistics are blind to.

Model: llama-3.3-70b-versatile, temperature 0. Output is strict JSON
{ "p_ai": <float 0-1>, "rationale": "<one sentence>" }.

Graceful degradation: if Groq is unavailable for any reason (no API key,
timeout, network/API error, or malformed output), the signal is **skipped** and
score_llm returns (None, None). The caller then falls back to Signal 1 only.
"""

import json
import os

_MODEL = "llama-3.3-70b-versatile"
_TIMEOUT_SECONDS = 15.0

_SYSTEM_PROMPT = (
    "You are a forensic text analyst judging whether a passage was written by an "
    "AI language model or by a human. Focus on meaning and texture, not surface "
    "statistics: cliche density, generic 'helpful assistant' framing, absence of "
    "lived specificity, and a suspiciously even emotional tone all point toward AI. "
    "Genuine idiosyncrasy, concrete lived detail, and uneven emotional texture "
    "point toward a human. "
    "Respond with STRICT JSON only, no prose, in exactly this shape: "
    '{"p_ai": <float between 0 and 1>, "rationale": "<one sentence>"}. '
    "p_ai is your probability that the text is AI-generated (1 = certainly AI, "
    "0 = certainly human)."
)


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def score_llm(text):
    """Return (p_llm, rationale).

    p_llm is a float in [0, 1] (probability-of-AI) and rationale a one-sentence
    string when the Groq judge succeeds. Returns (None, None) if the judge is
    skipped for any reason — the system then degrades gracefully to Signal 1.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None, None

    try:
        # Imported lazily so a missing/old SDK never breaks the deterministic path.
        from groq import Groq

        client = Groq(api_key=api_key, timeout=_TIMEOUT_SECONDS)
        resp = client.chat.completions.create(
            model=_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        raw = resp.choices[0].message.content
        data = json.loads(raw)
        p_ai = float(data["p_ai"])
        rationale = data.get("rationale")
        if rationale is not None:
            rationale = str(rationale)
        return _clamp(p_ai), rationale
    except Exception:
        # No key / timeout / API error / malformed JSON -> skip this signal.
        return None, None
