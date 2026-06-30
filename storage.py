"""Storage (SQLite, stdlib) — persists every decision and its audit trail.

Holds the `content` row (the decision + its current status) and an `audit_log`
entry beside it (planning.md "Architecture", submission flow). The `appeals`
table belongs to the appeal workflow and is not created here yet.

SQLite is chosen over a flat file because appeals must later mutate a content
row's status by content_id while leaving the original decision intact
(planning.md "Decisions / trade-offs").
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
