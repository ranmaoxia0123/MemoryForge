from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memoryforge.adapters.importer import import_local_file
from memoryforge.compiler.compiler import Compilation, compile_pending_sources
from memoryforge.compiler.lifecycle import apply_changeset, approve_changeset, review_changeset
from memoryforge.compiler.wiki_facts import parse_page_citations
from memoryforge.core.models import (
    CompilationPlan,
    PageChange,
    PageCitation,
    PlannedPage,
    Sensitivity,
)
from memoryforge.interface.cli import app
from memoryforge.query.query import _candidate_pages, answer_question
from memoryforge.query.support import _terms
from memoryforge.query.topic_context import current_topic_versions
from memoryforge.storage.changesets import ChangeSetStore
from memoryforge.storage.workspace import Workspace, read_source_excerpt


class ReferenceCompiler:
    """Hand-authored proposals exercise the pipeline, not model synthesis quality."""

    def __init__(self, change: PageChange) -> None:
        self.change = change
        self.messages: Sequence[Mapping[str, str]] = ()

    def plan_pages(self, messages: Sequence[Mapping[str, str]]) -> CompilationPlan:
        return CompilationPlan(
            pages=(
                PlannedPage(
                    path=self.change.path,
                    action="update",
                    source_ids=self.change.source_ids,
                    reason="Maintain one retry topic across design and incident evidence.",
                ),
            )
        )

    def compile_pages(self, messages: Sequence[Mapping[str, str]]) -> tuple[PageChange, ...]:
        self.messages = messages
        return (self.change,)


class CaptureAnswer:
    def __init__(self) -> None:
        self.payload: dict[str, Any] = {}

    def answer_with_evidence(
        self, messages: Sequence[Mapping[str, str]]
    ) -> tuple[str, tuple[int, ...]]:
        self.payload = json.loads(messages[1]["content"])
        facts = self.payload["facts"]
        return " ".join(fact["quote"] for fact in facts), tuple(range(len(facts)))


def _import(workspace: Workspace, name: str, text: str, *, public: bool = True) -> str:
    source_root = workspace.root.parent / "sources"
    source_root.mkdir(exist_ok=True)
    source = source_root / name
    source.write_text(text, encoding="utf-8")
    return import_local_file(
        workspace.root,
        source,
        source_root=source_root,
        sensitivity=Sensitivity.PUBLIC if public else Sensitivity.LOCAL_ONLY,
    ).source_id


def _publish(workspace: Workspace, compilation: Compilation | None) -> None:
    assert compilation is not None
    stored = ChangeSetStore(workspace).create(compilation.changeset, compilation.candidate_files)
    change_id = stored.changeset.changeset_id
    review_changeset(workspace.root, change_id)
    approve_changeset(workspace.root, change_id)
    apply_changeset(workspace.root, change_id)


def _topic(
    sources: dict[str, tuple[str, str]], *, draft: str = "Retry design evolved after an incident."
) -> PageChange:
    return PageChange(
        path="wiki/pages/retry.md",
        title="Retry design",
        page_type="synthesis",
        summary="Retry design rationale, incident history, and current rules.",
        body=draft,
        source_ids=tuple(sources),
        citations=tuple(
            PageCitation(
                source_id=source_id,
                locator=f"chars:{text.index(chr(10) + chr(10)) + 2}-{len(text)}",
                section=section,
            )
            for source_id, (text, section) in sources.items()
        ),
    )


def _setup_topic(tmp_path: Path) -> tuple[Workspace, str, dict[str, tuple[str, str]]]:
    workspace = Workspace.initialize(tmp_path / "wiki")
    design = "# Retry design\n\nRetry was introduced in January to tolerate temporary timeouts.\n"
    incident = (
        "# Retry incident\n\n"
        "Retry duplicated writes in February because requests lacked idempotency keys.\n"
    )
    sources = {
        _import(workspace, "design.md", design): (design, "Design rationale"),
        _import(workspace, "incident.md", incident): (incident, "Incident history"),
    }
    compilation = compile_pending_sources(workspace, provider=ReferenceCompiler(_topic(sources)))
    _publish(workspace, compilation)
    return workspace, workspace.page_paths_for_source(next(iter(sources)))[0], sources


def test_reorganize_published_sources_and_incrementally_extend_the_same_topic(
    tmp_path: Path,
) -> None:
    workspace = Workspace.initialize(tmp_path / "wiki")
    design = "# Retry design\n\nRetry was introduced in January to tolerate temporary timeouts.\n"
    incident = (
        "# Retry incident\n\n"
        "Retry duplicated writes in February because requests lacked idempotency keys.\n"
    )
    sources = {
        _import(workspace, "design.md", design): (design, "Design rationale"),
        _import(workspace, "incident.md", incident): (incident, "Incident history"),
    }
    unrelated = _import(
        workspace, "invoice.md", "# Invoice\n\nInvoices are retained for seven years.\n"
    )
    _publish(workspace, compile_pending_sources(workspace))
    unrelated_path = workspace.page_paths_for_source(unrelated)[0]
    before_unrelated = (workspace.root / unrelated_path).read_bytes()
    provider = ReferenceCompiler(_topic(sources))
    compiled = compile_pending_sources(
        workspace,
        source_ids=tuple(sources),
        provider=provider,
        reorganize_existing=True,
    )
    assert compiled is not None
    assert len([op for op in compiled.changeset.operations if op.type.value == "ARCHIVE_PAGE"]) == 2
    _publish(workspace, compiled)
    topic_path = workspace.page_paths_for_source(next(iter(sources)))[0]
    assert (workspace.root / unrelated_path).read_bytes() == before_unrelated
    assert "Invoices are retained" not in json.dumps(provider.messages)

    # One old input changes in the same batch as a genuinely new source arrives.
    first_id = next(iter(sources))
    updated = design.replace("January", "January 2025")
    assert _import(workspace, "design.md", updated) == first_id
    sources[first_id] = (updated, "Design rationale")
    current = (
        "# Retry current rule\n\n"
        "Retry now requires an idempotency key and stops after three attempts.\n"
    )
    sources[_import(workspace, "current.md", current)] = (current, "Current rule")
    provider = ReferenceCompiler(
        _topic(sources, draft="Retry now uses idempotency keys after duplicate writes.")
    )
    compiled = compile_pending_sources(workspace, provider=provider)
    assert compiled is not None
    assert topic_path in compiled.candidate_files
    assert "Retry design evolved after an incident." in json.dumps(provider.messages)
    assert all(text in provider.messages[1]["content"] for text, _ in sources.values())
    _publish(workspace, compiled)
    assert all(workspace.page_paths_for_source(source_id) == (topic_path,) for source_id in sources)
    content = (workspace.root / topic_path).read_text()
    facts = parse_page_citations(content)
    assert {fact["section_path"] for fact in facts} == {section for _, section in sources.values()}
    for fact in facts:
        assert fact["quote"] in read_source_excerpt(
            workspace.root,
            source_id=fact["source_id"],
            source_version=fact["source_version"],
            locator=fact["locator"],
        )
    assert (workspace.root / unrelated_path).read_bytes() == before_unrelated


def test_synthesis_reuses_topic_with_evidence_but_details_and_stale_sources_do_not(
    tmp_path: Path,
) -> None:
    workspace, path, sources = _setup_topic(tmp_path)
    provider = CaptureAnswer()
    result = answer_question(
        workspace.root, "Why did retry design evolve?", provider=provider, debug=True
    )
    assert result["model_status"] == "used", result
    assert len(provider.payload["compiled_topics"]) == 1
    context = provider.payload["compiled_topics"][0]
    assert context["wiki_page"] == path
    assert context["evidence_indexes"] == [0, 1]
    assert {fact["section"] for fact in provider.payload["facts"]} == {
        "Design rationale",
        "Incident history",
    }
    assert any(item["artifact"] == "Compiled topic index" for item in result["trace"])

    detail_provider = CaptureAnswer()
    answer_question(workspace.root, "When was retry introduced?", provider=detail_provider)
    assert detail_provider.payload
    assert "compiled_topics" not in detail_provider.payload

    old_body = (workspace.root / path).read_bytes()
    first_id = next(iter(sources))
    _import(workspace, "design.md", sources[first_id][0].replace("January", "March"))
    assert not current_topic_versions(workspace.root, path, old_body.decode())
    stale_provider = CaptureAnswer()
    result = answer_question(
        workspace.root, "Why did retry design evolve?", provider=stale_provider
    )
    assert "compiled_topics" not in stale_provider.payload
    assert result["evidence_status"] != "grounded"
    with pytest.raises(ValueError, match="topic page requires model recompilation"):
        compile_pending_sources(workspace)
    assert (workspace.root / path).read_bytes() == old_body


def test_topic_context_cannot_disclose_a_local_source_from_a_mixed_page(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "wiki")
    public = "# Retry public\n\nRetry handles temporary failures.\n"
    private = "# Retry private\n\nInternalCanary is the private retry design rationale.\n"
    sources = {
        _import(workspace, "public.md", public): (public, "Public rule"),
        _import(workspace, "private.md", private, public=False): (private, "Private rationale"),
    }
    _publish(
        workspace,
        compile_pending_sources(
            workspace,
            provider=ReferenceCompiler(_topic(sources, draft="InternalCanary explains retry.")),
            allow_local=True,
        ),
    )
    provider = CaptureAnswer()
    answer_question(workspace.root, "Why does retry handle temporary failures?", provider=provider)
    assert "compiled_topics" not in provider.payload
    assert "InternalCanary" not in json.dumps(provider.payload)


def test_reorganize_cli_requires_a_model_and_preserves_the_existing_review_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _path, sources = _setup_topic(tmp_path)
    runner = CliRunner()
    missing = runner.invoke(app, ["ingest", "--reorganize", "--workspace", str(workspace.root)])
    assert missing.exit_code != 0
    assert "--reorganize requires --llm" in missing.output
    provider = ReferenceCompiler(_topic(sources))
    monkeypatch.setattr("memoryforge.interface.cli.ProviderConfig.from_environment", lambda: None)
    monkeypatch.setattr("memoryforge.interface.cli.OpenAICompatibleProvider", lambda _: provider)
    before = workspace.current_commit()
    result = runner.invoke(
        app,
        [
            "ingest",
            "--llm",
            "--reorganize",
            "--workspace",
            str(workspace.root),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "PROPOSED"
    assert workspace.current_commit() == before


def test_topic_routing_does_not_override_an_explicit_version_lookup(tmp_path: Path) -> None:
    workspace, _path, _sources = _setup_topic(tmp_path)
    trace = []
    question = "Why did retry change in 1.2.3?"
    _candidate_pages(
        workspace.root,
        question,
        _terms(question),
        max_pages=3,
        trace=trace,
        repository_id=None,
    )
    assert all(item["artifact"] != "Compiled topic index" for item in trace)


def test_topic_section_cannot_inject_an_evidence_block() -> None:
    with pytest.raises(ValueError, match="one non-empty line"):
        PageCitation(source_id="a" * 64, locator="chars:0-10", section="Rationale\n## Sources")


def test_omitted_topic_detail_expands_current_applied_source_with_a_separate_citation(
    tmp_path: Path,
) -> None:
    workspace = Workspace.initialize(tmp_path / "wiki")
    fact = "Retry handles temporary failures."
    detail = "The parameter retry_jitter_ms is 250 to spread retries over time."
    text = f"# Retry design\n\n{fact}\n\n{detail}\n"
    source_id = _import(workspace, "retry.md", text)
    change = _topic({source_id: (text, "Current rule")}).model_copy(
        update={
            "citations": (
                PageCitation(
                    source_id=source_id,
                    locator=f"chars:{text.index(fact)}-{text.index(fact) + len(fact)}",
                    section="Current rule",
                ),
            ),
        }
    )
    _publish(workspace, compile_pending_sources(workspace, provider=ReferenceCompiler(change)))

    class DetailAnswer:
        def __init__(self) -> None:
            self.payloads: list[dict[str, Any]] = []

        def answer_with_evidence(
            self,
            messages: Sequence[Mapping[str, str]],
        ) -> tuple[str, tuple[int, ...]]:
            payload = json.loads(messages[1]["content"])
            self.payloads.append(payload)
            for fact in payload["facts"]:
                if "retry_jitter_ms" in fact["quote"]:
                    return fact["quote"], (fact["index"],)
            return "不知道", ()

    direct = DetailAnswer()
    answer = answer_question(
        workspace.root, "What is retry_jitter_ms?", provider=direct, verify=True
    )
    assert answer["status"] == "answered", answer
    assert len(direct.payloads) == 1
    assert answer["citations"][0]["evidence_origin"] == "source_passage"
    assert answer["evidence"][0]["text"] == detail
    assert all("compiled_topics" not in payload for payload in direct.payloads)

    synthesis = DetailAnswer()
    answer = answer_question(
        workspace.root,
        "Why is retry_jitter_ms required?",
        provider=synthesis,
        verify=True,
    )
    assert answer["status"] == "answered", answer
    assert len(synthesis.payloads) == 2
    assert synthesis.payloads[0]["compiled_topics"]
    assert "compiled_topics" not in synthesis.payloads[1]
    assert answer["evidence"][0]["text"] == detail
    with sqlite3.connect(workspace.index_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM disclosure_receipts").fetchone()[0] == 3

    _import(workspace, "retry.md", text.replace("250", "999"))
    unpublished = DetailAnswer()
    answer = answer_question(workspace.root, "What is retry_jitter_ms?", provider=unpublished)
    assert answer["status"] == "unknown"
    assert "999" not in json.dumps(unpublished.payloads)
