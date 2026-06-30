"""Provenance Guard — Flask app (submission flow, Signal 1 only).

Implements the submission half of the API contract (planning.md Appendix):
POST /submit, GET /health, GET /content/<id>, GET /log. Detection currently
runs Signal 1 (stylometric heuristics) only; Signal 2 (Groq LLM judge) is not
wired in yet, so every decision degrades gracefully to the single-signal path:
p_llm = null, p_final = p_style, confidence capped at 0.70 (planning.md §2).

The appeal workflow (POST /appeal, GET /appeals) is a separate milestone and is
not included here.
"""

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import storage
from detector import score_stylometric
from labels import make_label
from scoring import combine

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

    # Signal 1 only — Signal 2 (Groq LLM) is not built yet, so p_llm stays None
    # and combine() runs the single-signal path (cap 0.70).
    p_style = score_stylometric(text)
    p_llm = None
    scored = combine(p_style, p_llm)
    label = make_label(scored["result"], scored["confidence"])

    decision = {
        "result": scored["result"],
        "confidence": scored["confidence"],
        "p_style": p_style,
        "p_llm": p_llm,
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
                "p_llm": p_llm,
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


@app.route("/log", methods=["GET"])
def get_log():
    content_id = request.args.get("content_id")
    return jsonify({"entries": storage.get_log(content_id)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
