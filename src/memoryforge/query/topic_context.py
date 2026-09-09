"""Reuse compiled topics only while their complete evidence set is current."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from memoryforge.compiler.index_rendering import _frontmatter_fields
from memoryforge.compiler.source_rendering import _markdown_facts
from memoryforge.compiler.wiki_facts import CitationPayload
from memoryforge.query.retrieval_v2 import _lexical_lane
from memoryforge.storage.database import connect_readonly
from memoryforge.storage.folder_dependencies import stale_folder_source_versions
from memoryforge.storage.workspace import DATABASE_RELATIVE_PATH, _fts_query


def is_synthesis_question(question: str) -> bool:
    """Route explanatory questions; leave version/parameter lookups on source retrieval."""
    if re.search(r"\b\d+\.\d+\.\d+\b", question):
        return False
    # ponytail: explicit intent words; evaluate routing errors before adding a model router.
    return bool(
        re.search(
            r"\b(why|compare|comparison|overview|rationale|tradeoffs?|evolved?|evolution)\b"
            r"|为什么|为何|设计原因|设计理由|设计思路|权衡|取舍|对比|比较|演变|来龙去脉|概览",
            question,
            re.IGNORECASE,
        )
    )


def current_topic_versions(
    workspace_root: Path,
    page_path: str,
    content: str,
    *,
    public_only: bool = False,
) -> dict[str, int]:
    """Check all topic inputs, including inputs not selected as answer citations."""
    fields = _frontmatter_fields(content)
    if fields.get("generated") != "topic_wiki":
        return {}
    database = workspace_root / DATABASE_RELATIVE_PATH
    if not database.is_file() or database.is_symlink():
        return {}
    try:
        versions = json.loads(fields.get("source_versions", "{}"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(versions, dict) or not versions:
        return {}
    with connect_readonly(database) as connection:
        rows = connection.execute(
            """SELECT s.source_id, v.id, a.source_version_id, v.sensitivity
               FROM page_sources AS ps
               JOIN sources AS s ON s.source_id = ps.source_id
               LEFT JOIN source_versions AS v ON v.source_id = s.id AND v.is_current = 1
               LEFT JOIN applied_source_versions AS a ON a.source_id = s.source_id
               WHERE ps.page_path = ?""",
            (page_path,),
        ).fetchall()
        if not rows or any(
            row[1] is None or row[1] != row[2] or (public_only and row[3] != "public")
            for row in rows
        ):
            return {}
        current = {str(row[0]): int(row[1]) for row in rows}
        if current != versions or set(current.items()) & stale_folder_source_versions(connection):
            return {}
    return current


def topic_draft(content: str) -> str:
    """Read the compiled explanation, never treating it as an exact source fact."""
    _, separator, body = content.partition("## Model summary (unverified)\n")
    if not separator:
        return ""
    return re.split(r"^## (?:Verified facts|Related pages)\s*$", body, maxsplit=1, flags=re.M)[
        0
    ].strip()


def source_passages(
    workspace_root: Path,
    question: str,
    page_versions: dict[str, dict[str, int]],
) -> list[tuple[str, CitationPayload]]:
    """Recover omitted details from the same current, applied topic inputs.

    The caller supplies topic versions after scope and sensitivity checks. Raw
    passages remain labelled; they are not promoted to published Wiki facts.
    """
    if not page_versions:
        return []
    placeholders = ",".join("?" for _ in page_versions)
    facts: list[dict[str, Any]] = []
    with connect_readonly(workspace_root / DATABASE_RELATIVE_PATH) as connection:
        rows = connection.execute(
            f"""SELECT ps.page_path, s.source_id, v.id, v.title, source_fts.content
                FROM source_fts
                JOIN source_versions AS v ON v.id = source_fts.rowid
                JOIN sources AS s ON s.id = v.source_id
                JOIN applied_source_versions AS a
                  ON a.source_id = s.source_id AND a.source_version_id = v.id
                JOIN page_sources AS ps ON ps.source_id = s.source_id
                WHERE source_fts MATCH ? AND v.is_current = 1
                  AND ps.page_path IN ({placeholders})
                ORDER BY bm25(source_fts), s.source_id""",
            (_fts_query(question, require_all_terms=False), *page_versions),
        )
        used_sources = 0
        for path, source_id, version, title, content in rows:
            if page_versions[path].get(source_id) != version:
                continue
            for fact in _markdown_facts(content):
                if len(fact.quote) > 4000:
                    continue
                facts.append(
                    {
                        "page_path": path,
                        "source_id": source_id,
                        "source_version": version,
                        "locator": f"chars:{fact.start}-{fact.start + len(fact.quote)}",
                        "quote": fact.quote,
                        "section_path": " / ".join((title, *fact.section_path)),
                    }
                )
            used_sources += 1
            if used_sources == 12:
                break
    return [
        (
            fact["page_path"],
            CitationPayload(
                source_id=fact["source_id"],
                source_version=fact["source_version"],
                locator=fact["locator"],
                quote=fact["quote"],
                section_path=fact["section_path"],
                grounding="exact",
                evidence_origin="source_passage",
            ),
        )
        for fact, _ in _lexical_lane(question, facts)[:6]
    ]
