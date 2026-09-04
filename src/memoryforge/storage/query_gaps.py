"""Opt-in local query-gap recording."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memoryforge.compiler.redaction import redact_for_model
from memoryforge.storage.database import connect, connect_readonly

SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS query_gap_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1))
    )
    """,
    "INSERT OR IGNORE INTO query_gap_settings(id, enabled) VALUES (1, 0)",
    """
    CREATE TABLE IF NOT EXISTS query_gaps (
        question_sha256 TEXT NOT NULL,
        repository_scope TEXT NOT NULL DEFAULT '',
        question_preview TEXT NOT NULL,
        redaction_count INTEGER NOT NULL DEFAULT 0,
        occurrence_count INTEGER NOT NULL DEFAULT 1,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        PRIMARY KEY(question_sha256, repository_scope)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_query_gaps_priority
    ON query_gaps(occurrence_count DESC, last_seen_at DESC)
    """,
)


def query_gap_recording_enabled(database_path: Path) -> bool:
    with connect_readonly(database_path) as connection:
        row = connection.execute(
            "SELECT enabled FROM query_gap_settings WHERE id = 1"
        ).fetchone()
    return bool(row and row["enabled"])


def set_query_gap_recording(database_path: Path, *, enabled: bool) -> None:
    with connect(database_path) as connection:
        connection.execute(
            "UPDATE query_gap_settings SET enabled = ? WHERE id = 1",
            (int(enabled),),
        )


def record_query_gap_if_enabled(
    database_path: Path,
    question: str,
    *,
    evidence_status: str,
    repository_id: str | None = None,
    recorded_at: datetime | None = None,
) -> bool:
    if evidence_status != "no_local_evidence" or not query_gap_recording_enabled(database_path):
        return False
    redacted = redact_for_model(" ".join(question.split()))
    normalized = redacted.redacted_text.strip()
    if not normalized:
        return False
    digest = hashlib.sha256(normalized.casefold().encode()).hexdigest()
    scope = repository_id or ""
    now = (recorded_at or datetime.now(UTC)).isoformat()
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO query_gaps(
                question_sha256, repository_scope, question_preview, redaction_count,
                occurrence_count, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(question_sha256, repository_scope) DO UPDATE SET
                occurrence_count = query_gaps.occurrence_count + 1,
                last_seen_at = excluded.last_seen_at,
                redaction_count = MAX(query_gaps.redaction_count, excluded.redaction_count)
            """,
            (
                digest,
                scope,
                normalized[:500],
                redacted.redaction_count,
                now,
                now,
            ),
        )
    return True


def list_query_gaps(database_path: Path, *, limit: int = 20) -> list[dict[str, object]]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with connect_readonly(database_path) as connection:
        rows = connection.execute(
            """
            SELECT question_preview, repository_scope, redaction_count,
                   occurrence_count, first_seen_at, last_seen_at
            FROM query_gaps
            ORDER BY occurrence_count DESC, last_seen_at DESC, question_sha256
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "question": str(row["question_preview"]),
            "repository_id": str(row["repository_scope"]) or None,
            "redaction_count": int(row["redaction_count"]),
            "occurrences": int(row["occurrence_count"]),
            "first_seen_at": str(row["first_seen_at"]),
            "last_seen_at": str(row["last_seen_at"]),
        }
        for row in rows
    ]


def clear_query_gaps(database_path: Path, *, older_than_days: int | None = None) -> int:
    with connect(database_path) as connection:
        if older_than_days is None:
            cursor = connection.execute("DELETE FROM query_gaps")
        else:
            if older_than_days < 1:
                raise ValueError("older_than_days must be positive")
            cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
            cursor = connection.execute(
                "DELETE FROM query_gaps WHERE last_seen_at < ?",
                (cutoff,),
            )
    return int(cursor.rowcount)
