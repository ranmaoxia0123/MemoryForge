#!/usr/bin/env python3
"""Compare raw, compiled Wiki, and hybrid evidence under one character budget.

This is a retrieval representation ablation, not an answer-accuracy benchmark.
It runs no model. Cases contain a question and expected evidence snippets.
Only current, applied, public sources are eligible. Exact evidence coverage is
measured on quotes, never on the unverified compiled explanation.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from memoryforge.compiler.compiler import _load_current_sources
from memoryforge.compiler.index_rendering import _parse_page_summary
from memoryforge.compiler.source_rendering import _read_source_text
from memoryforge.compiler.wiki_facts import parse_page_citations
from memoryforge.core.models import Sensitivity
from memoryforge.query.query import _candidate_pages, _safe_wiki_page
from memoryforge.query.support import _content_question_terms, _terms
from memoryforge.query.topic_context import (
    current_topic_versions,
    is_synthesis_question,
    topic_draft,
)
from memoryforge.storage.database import connect_readonly
from memoryforge.storage.folder_dependencies import stale_folder_source_versions
from memoryforge.storage.workspace import Workspace


def _pack(units: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for unit in units:
        key = (unit["source_id"], unit["locator"])
        if key in seen:
            continue
        if len(json.dumps([*result, unit], ensure_ascii=False)) > budget:
            continue
        result.append(unit)
        seen.add(key)
    return result


def run(workspace_root: Path, cases: list[dict[str, Any]], budget: int) -> dict[str, Any]:
    workspace = Workspace.open_readonly(workspace_root)
    with connect_readonly(workspace.index_path) as connection:
        applied = dict(
            connection.execute(
                "SELECT source_id, source_version_id FROM applied_source_versions"
            ).fetchall()
        )
        stale = stale_folder_source_versions(connection)
    sources = [
        source
        for source in _load_current_sources(workspace, set())
        if source.sensitivity is Sensitivity.PUBLIC
        and applied.get(source.source_id) == source.source_version
        and (source.source_id, source.source_version) not in stale
    ]
    raw_units = []
    for source in sources:
        text = _read_source_text(workspace, source)
        # Fixed raw chunks, independent of the compiled page's choice of evidence.
        for start in range(0, len(text), 448):
            quote = text[start : start + 512]
            raw_units.append(
                {
                    "source_id": source.source_id,
                    "locator": f"chars:{start}-{start + len(quote)}",
                    "quote": quote,
                    "title": source.title,
                }
            )
    database = sqlite3.connect(":memory:")
    try:
        database.execute("CREATE VIRTUAL TABLE chunks USING fts5(title, quote)")
        database.executemany(
            "INSERT INTO chunks(title, quote) VALUES (?, ?)",
            [(unit["title"], unit["quote"]) for unit in raw_units],
        )
        results = []
        for case in cases:
            question = case["question"]
            query = " OR ".join(
                '"' + term.replace('"', '""') + '"'
                for term in sorted(_content_question_terms(question))
            )
            raw_ranked = (
                [
                    raw_units[row[0] - 1]
                    for row in database.execute(
                        "SELECT rowid FROM chunks WHERE chunks MATCH ? "
                        "ORDER BY bm25(chunks), rowid",
                        (query,),
                    )
                ]
                if query
                else []
            )
            wiki_units = []
            visible_versions = {source.source_id: source.source_version for source in sources}
            for page in _candidate_pages(
                workspace.root,
                question,
                _terms(question),
                max_pages=10,
                trace=[],
                repository_id=None,
                public_topics_only=True,
            ):
                if _safe_wiki_page(workspace.root, page) is None:
                    continue
                path = str(page.relative_to(workspace.root))
                content = page.read_text(encoding="utf-8")
                citations = parse_page_citations(content)
                if not citations or any(
                    visible_versions.get(citation["source_id"]) != citation["source_version"]
                    for citation in citations
                ):
                    continue
                summary = _parse_page_summary(path, content)
                fresh_topic = current_topic_versions(
                    workspace.root, path, content, public_only=True
                )
                for index, citation in enumerate(citations):
                    unit = {
                        "source_id": citation["source_id"],
                        "locator": citation["locator"],
                        "quote": citation["quote"],
                        "section": citation.get("section_path", ""),
                        "title": summary.title if summary else "",
                        "wiki_page": path,
                    }
                    if index == 0 and fresh_topic and is_synthesis_question(question):
                        unit["unverified_draft"] = topic_draft(content)
                    wiki_units.append(unit)
            hybrid = (
                [*wiki_units, *raw_ranked]
                if is_synthesis_question(question)
                else [*raw_ranked, *wiki_units]
            )
            variants = {}
            for name, units in (("raw_bm25", raw_ranked), ("wiki", wiki_units), ("hybrid", hybrid)):
                context = _pack(units, budget)
                evidence = "\n".join(unit["quote"] for unit in context)
                expected = case["expected_evidence"]
                covered = [snippet for snippet in expected if snippet in evidence]
                variants[name] = {
                    "evidence_coverage": len(covered) / len(expected) if expected else None,
                    "covered": covered,
                    "context_characters": len(json.dumps(context, ensure_ascii=False)),
                    "context": context,
                }
            results.append({"id": case["id"], "question": question, "variants": variants})
    finally:
        database.close()
    return {
        "measurement": "evidence coverage; no model answers or answer accuracy",
        "character_budget": budget,
        "raw_chunk_characters": 512,
        "raw_overlap_characters": 64,
        "source_count": len(sources),
        "case_count": len(cases),
        "cases": results,
        "limitations": [
            "Character budgets are not token budgets.",
            "Hybrid is an experimental context assembler, not the production ask command.",
            "Compile quality and cost depend on how the input Wiki was produced.",
            "Evidence coverage does not measure reasoning, contradictions, or reading effort.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--max-characters", type=int, default=3000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.workspace, json.loads(args.cases.read_text()), args.max_characters)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
