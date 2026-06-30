"""Confidence scoring — blend the signals into a calibrated decision.

Implements planning.md §1 ("Combining into one score") and §2 ("Uncertainty
representation"). Signal 2 (the Groq LLM judge) is not built yet, so callers
pass p_llm=None and the single-signal path runs: p_final = p_style with
confidence capped at 0.70 (we refuse to sound certain on half the evidence).
"""

_W_STYLE = 0.45
_W_LLM = 0.55
_SINGLE_SIGNAL_CAP = 0.70   # confidence ceiling when only Signal 1 is present
_HIGH_CONFIDENCE = 0.75     # line between "uncertain" and "likely" (§2)


def combine(p_style, p_llm=None):
    """Blend signal outputs into {p_final, result, confidence}.

    Both signals present:  p_final = 0.45*p_style + 0.55*p_llm
    LLM unavailable:        p_final = p_style, confidence capped at 0.70

    result is always the directional lean ("AI-generated" / "Human-written");
    uncertainty is carried by `confidence` (see labels.make_label).
    confidence = max(p_final, 1 - p_final): distance from the coin-flip, in
    [0.5, 1.0]. Agreement pushes it toward 1.0; disagreement collapses it to 0.5.
    """
    if p_llm is None:
        p_final = p_style
    else:
        p_final = _W_STYLE * p_style + _W_LLM * p_llm

    # <= 0.50 leans human, > 0.50 leans AI (§2). result never says "uncertain".
    result = "AI-generated" if p_final > 0.5 else "Human-written"

    confidence = max(p_final, 1 - p_final)
    if p_llm is None:
        confidence = min(confidence, _SINGLE_SIGNAL_CAP)

    return {
        "p_final": p_final,
        "result": result,
        "confidence": confidence,
    }
