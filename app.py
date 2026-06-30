"""Provenance Guard — Flask app (full API surface).

Implements the API contract (planning.md Appendix):

  Submission flow:  POST /submit, GET /content/<id>, GET /log, GET /health
  Appeal flow:      POST /appeal, GET /appeals

Detection runs both signals: Signal 1 (stylometric heuristics, local) and
Signal 2 (Groq LLM judge), blended by scoring.combine into p_final/confidence.
If the Groq judge is unavailable (no key / timeout / error), Signal 2 is skipped
and the system degrades gracefully to the single-signal path: p_llm = null,
p_final = p_style, confidence capped at 0.70 (planning.md §1/§2).

The appeal flow touches Storage only — it never re-classifies. It writes an
appeals row, flips the content status analyzed -> under review, and appends an
"appeal" audit entry beside the original decision (planning.md §4).
"""

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import storage
from detector import score_stylometric
from labels import make_label
from llm_judge import score_llm
from scoring import combine

load_dotenv()

app = Flask(__name__)

# /submit is the only rate-limited route: 10 requests/min, 100/day (Appendix).
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
)

storage.init_db()


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"error": "rate limit exceeded", "detail": str(e.description)}), 429


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute")
@limiter.limit("100 per day")
def submit():
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "missing or empty 'text'"}), 400

    # Two independent signals. Signal 2 returns (None, None) when the Groq judge
    # is unavailable, in which case combine() runs the single-signal path (cap 0.70).
    p_style = score_stylometric(text)
    p_llm, rationale = score_llm(text)
    scored = combine(p_style, p_llm)
    label = make_label(scored["result"], scored["confidence"])

    decision = {
        "result": scored["result"],
        "confidence": scored["confidence"],
        "p_style": p_style,
        "p_llm": p_llm,
        "rationale": rationale,
        "p_final": scored["p_final"],
        "label_text": label["label_text"],
        "label_variant": label["label_variant"],
    }
    content_id = storage.create_content(text, decision)

    return jsonify(
        {
            "content_id": content_id,
            "result": scored["result"],
            "confidence": round(scored["confidence"], 2),
            "label_text": label["label_text"],
            "label_variant": label["label_variant"],
            "signals": {
                "p_style": round(p_style, 4),
                "p_llm": round(p_llm, 4) if p_llm is not None else None,
                "p_final": round(scored["p_final"], 4),
            },
            "status": "analyzed",
        }
    )


@app.route("/content/<content_id>", methods=["GET"])
def get_content(content_id):
    row = storage.get_content(content_id)
    if row is None:
        return jsonify({"error": "unknown content_id"}), 400
    return jsonify(
        {
            "content_id": row["content_id"],
            "result": row["result"],
            "confidence": round(row["confidence"], 2),
            "status": row["status"],
            "label_text": row["label_text"],
        }
    )


@app.route("/appeal", methods=["POST"])
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = data.get("content_id")
    reasoning = data.get("reasoning")
    if not isinstance(content_id, str) or not content_id:
        return jsonify({"error": "missing 'content_id'"}), 400
    if not isinstance(reasoning, str) or not reasoning.strip():
        return jsonify({"error": "missing or empty 'reasoning'"}), 400

    created = storage.create_appeal(content_id, reasoning)
    if created is None:
        return jsonify({"error": "unknown content_id"}), 400

    appeal_id, _submitted_at = created
    return jsonify(
        {
            "content_id": content_id,
            "status": "under review",
            "appeal_id": appeal_id,
            "message": (
                "Your appeal has been received. This content is now under review; "
                "the original automated estimate is preserved but no longer "
                "presented as final."
            ),
        }
    )


@app.route("/appeals", methods=["GET"])
def appeals():
    status = request.args.get("status", "open")
    return jsonify({"appeals": storage.get_appeals(status)})


@app.route("/log", methods=["GET"])
def get_log():
    content_id = request.args.get("content_id")
    return jsonify({"entries": storage.get_log(content_id)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
