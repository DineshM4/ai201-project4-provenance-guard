"""Storage (SQLite, stdlib) — persists every decision and its audit trail.

Holds the `content` row (the decision + its current status), the `appeals` row
(one per submitted appeal), and an `audit_log` entry beside each event
(planning.md "Architecture").

SQLite is chosen over a flat file because appeals must mutate a content row's
status by content_id (`analyzed → under review`) while leaving the original
decision and its audit-log entry intact beside it (planning.md "Decisions /
trade-offs").
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone

DB_PATH = "provenance.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content (
                content_id     TEXT PRIMARY KEY,
                text           TEXT NOT NULL,
                result         TEXT NOT NULL,
                confidence     REAL NOT NULL,
                p_style        REAL,
                p_llm          REAL,
                p_final        REAL NOT NULL,
                label_text     TEXT NOT NULL,
                label_variant  TEXT NOT NULL,
                status         TEXT NOT NULL,
                created_at     TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appeals (
                appeal_id     TEXT PRIMARY KEY,
                content_id    TEXT NOT NULL,
                reasoning     TEXT NOT NULL,
                submitted_at  TEXT NOT NULL,
                status        TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                content_id  TEXT,
                details     TEXT
            )
            """
        )


def create_content(text, decision):
    """Insert a content row (status="analyzed") plus a "submit" audit entry.

    `decision` carries result, confidence, p_style, p_llm, p_final, label_text,
    label_variant. Returns the generated content_id.
    """
    content_id = uuid.uuid4().hex
    created_at = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO content (
                content_id, text, result, confidence, p_style, p_llm, p_final,
                label_text, label_variant, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_id,
                text,
                decision["result"],
                decision["confidence"],
                decision["p_style"],
                decision["p_llm"],
                decision["p_final"],
                decision["label_text"],
                decision["label_variant"],
                "analyzed",
                created_at,
            ),
        )
        _append_audit(
            conn,
            event_type="submit",
            content_id=content_id,
            details={
                "result": decision["result"],
                "confidence": decision["confidence"],
                "p_style": decision["p_style"],
                "p_llm": decision["p_llm"],
                "rationale": decision.get("rationale"),
                "p_final": decision["p_final"],
                "label_variant": decision["label_variant"],
            },
        )
    return content_id


def _append_audit(conn, event_type, content_id, details):
    conn.execute(
        "INSERT INTO audit_log (ts, event_type, content_id, details) VALUES (?, ?, ?, ?)",
        (_now(), event_type, content_id, json.dumps(details)),
    )


def get_content(content_id):
    """Return the content row as a dict, or None if unknown."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM content WHERE content_id = ?", (content_id,)
        ).fetchone()
    return dict(row) if row else None


def get_log(content_id=None):
    """Return audit-log entries (optionally filtered by content_id), oldest first."""
    with _connect() as conn:
        if content_id is None:
            rows = conn.execute(
                "SELECT ts, event_type, content_id, details FROM audit_log ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, event_type, content_id, details FROM audit_log "
                "WHERE content_id = ? ORDER BY id",
                (content_id,),
            ).fetchall()
    return [
        {
            "ts": r["ts"],
            "event_type": r["event_type"],
            "content_id": r["content_id"],
            "details": json.loads(r["details"]) if r["details"] else None,
        }
        for r in rows
    ]


def create_appeal(content_id, reasoning):
    """Record an appeal (Storage only — no re-classification; planning.md §4).

    Atomically: validate the content exists, insert an "open" appeals row, flip
    the content status analyzed -> under review, and append an "appeal" audit
    entry beside the original decision. Returns (appeal_id, submitted_at), or
    None if the content_id is unknown (caller returns 400).
    """
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM content WHERE content_id = ?", (content_id,)
        ).fetchone()
        if exists is None:
            return None

        appeal_id = uuid.uuid4().hex
        submitted_at = _now()
        conn.execute(
            "INSERT INTO appeals (appeal_id, content_id, reasoning, submitted_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (appeal_id, content_id, reasoning, submitted_at, "open"),
        )
        # Original verdict is preserved; only the status changes (planning.md §4).
        conn.execute(
            "UPDATE content SET status = ? WHERE content_id = ?",
            ("under review", content_id),
        )
        _append_audit(
            conn,
            event_type="appeal",
            content_id=content_id,
            details={"appeal_id": appeal_id, "content_id": content_id,
                     "submitted_at": submitted_at},
        )
    return appeal_id, submitted_at


def get_appeals(status="open"):
    """Reviewer queue: one row per appeal with the given status (planning.md §4).

    Joins the appeal with its content row (original verdict + current status)
    and the original "submit" audit entry (both signal scores + LLM rationale),
    so the reviewer sees the full evidence without re-running detection.
    """
    with _connect() as conn:
        appeals = conn.execute(
            "SELECT * FROM appeals WHERE status = ? ORDER BY submitted_at", (
                status,)
        ).fetchall()

        queue = []
        for a in appeals:
            content = conn.execute(
                "SELECT * FROM content WHERE content_id = ?", (a["content_id"],)
            ).fetchone()
            audit = conn.execute(
                "SELECT details FROM audit_log WHERE content_id = ? AND event_type = 'submit' "
                "ORDER BY id LIMIT 1",
                (a["content_id"],),
            ).fetchone()
            details = json.loads(
                audit["details"]) if audit and audit["details"] else {}

            text = content["text"] if content else ""
            excerpt = text if len(text) <= 240 else text[:240].rstrip() + "…"

            queue.append(
                {
                    "appeal_id": a["appeal_id"],
                    "content_id": a["content_id"],
                    "text_excerpt": excerpt,
                    "result": content["result"] if content else None,
                    "confidence": round(content["confidence"], 2) if content else None,
                    "label_variant": content["label_variant"] if content else None,
                    "p_style": details.get("p_style"),
                    "p_llm": details.get("p_llm"),
                    "rationale": details.get("rationale"),
                    "reasoning": a["reasoning"],
                    "status": content["status"] if content else None,
                    "submitted_at": a["submitted_at"],
                }
            )
    return queue
