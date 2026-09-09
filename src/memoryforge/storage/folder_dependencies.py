"""Version-pinned dependencies from explicit links within imported folders."""

from __future__ import annotations

import sqlite3


def stale_folder_source_versions(connection: sqlite3.Connection) -> frozenset[tuple[str, int]]:
    """Find direct and transitive dependents of replaced or deleted source versions."""
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='folder_source_dependencies'"
        ).fetchone()
        is None
    ):
        return frozenset()
    rows = connection.execute(
        """WITH RECURSIVE stale(id) AS (
               SELECT dependencies.source_version_id
               FROM folder_source_dependencies AS dependencies
               JOIN source_versions AS target ON target.id=dependencies.target_source_version_id
               WHERE target.is_current=0
               UNION
               SELECT dependencies.source_version_id
               FROM folder_source_dependencies AS dependencies
               JOIN stale ON stale.id=dependencies.target_source_version_id
           )
           SELECT sources.source_id, versions.id
           FROM stale
           JOIN source_versions AS versions ON versions.id=stale.id
           JOIN sources ON sources.id=versions.source_id"""
    ).fetchall()
    return frozenset((str(row[0]), int(row[1])) for row in rows)
