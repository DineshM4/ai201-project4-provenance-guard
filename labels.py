"""Transparency labels — the three reader-facing variants (planning.md §3).

A variant is chosen by (result, confidence) against the 0.75 threshold. Every
variant states it is an estimate, shows the numeric confidence as a whole
percent, and surfaces the appeal route on any result that could harm a creator.
"""

_HIGH_CONFIDENCE = 0.75


def make_label(result, confidence):
    """Return {label_text, label_variant} for the given decision.

    label_variant in {high_confidence_ai, high_confidence_human, uncertain}.
    """
    pct = f"{confidence:.0%}"

    if confidence >= _HIGH_CONFIDENCE and result == "AI-generated":
        variant = "high_confidence_ai"
        text = (
            f"Likely AI-generated. Our analysis indicates this text was probably "
            f"produced by an AI system (confidence: {pct}). This is an automated "
            f"estimate, not proof. If you wrote this yourself, you can appeal this result."
        )
    elif confidence >= _HIGH_CONFIDENCE and result == "Human-written":
        variant = "high_confidence_human"
        text = (
            f"Likely human-written. Our analysis indicates this text was probably "
            f"written by a person (confidence: {pct}). This is an automated "
            f"estimate, not a guarantee of authorship."
        )
    else:
        variant = "uncertain"
        text = (
            f"Uncertain. Our signals disagree or are too weak to call this one "
            f"(confidence: {pct}, leaning {result}). Please do not treat this as a "
            f"definitive judgment of authorship. You can appeal if needed."
        )

    return {"label_text": text, "label_variant": variant}
