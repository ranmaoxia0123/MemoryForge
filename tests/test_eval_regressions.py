from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memoryforge.adapters.folder_adapter import sync_folder
from memoryforge.compiler.compiler import compile_pending_sources
from memoryforge.compiler.source_rendering import _meaningful_paragraphs
from memoryforge.core.errors import WorkspaceError
from memoryforge.core.models import Sensitivity
from memoryforge.interface.cli import app
from memoryforge.query.query import answer_question
from memoryforge.storage.changesets import ChangeSetStore
from memoryforge.storage.workspace import Workspace
from tests.cli_helpers import review_approve_apply


def _publish(workspace: Workspace) -> None:
    compilation = compile_pending_sources(workspace)
    assert compilation is not None
    ChangeSetStore(workspace).create(compilation.changeset, compilation.candidate_files)
    result = review_approve_apply(CliRunner(), compilation.changeset.changeset_id, workspace.root)
    assert result.exit_code == 0, result.output


def test_large_git_publication_keeps_unrelated_staging_and_dirty_gate(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "wiki")
    store = workspace.version_store
    paths = tuple(f"wiki/pages/{i:04d}-{'x' * 190}.md" for i in range(1600))
    store.require_clean_paths(paths)
    for path in paths:
        (workspace.root / path).write_text("initial\n")
    (workspace.root / "unrelated.txt").write_text("must remain staged\n")
    store._run(["add", "unrelated.txt"], check=True)
    before = store.head()
    commit = store.commit_paths(paths, "test: large approved publication")
    assert store._run(["rev-parse", "HEAD^"], check=True).stdout.strip() == before
    assert store._run(["show", "--format=", "--name-only", commit], check=True).stdout.count(
        ".md"
    ) == len(paths)
    assert (
        store._run(["diff", "--cached", "--name-only"], check=True).stdout.strip()
        == "unrelated.txt"
    )
    store.require_clean_paths(paths)
    archived = store.read_wiki_texts_at(commit, paths=paths)
    assert set(archived) == set(paths)
    assert set(archived.values()) == {"initial\n"}
    (workspace.root / paths[-1]).write_text("changed\n")
    with pytest.raises(WorkspaceError, match="uncommitted"):
        store.require_clean_paths(paths)
    store._run_paths(["add"], paths)
    store.reset_paths(paths)
    assert (
        store._run(["diff", "--cached", "--name-only"], check=True).stdout.strip()
        == "unrelated.txt"
    )
    # A literal glob character must never include a sibling in the commit.
    literal = "wiki/pages/[a].md"
    (workspace.root / literal).write_text("literal\n")
    (workspace.root / "wiki/pages/a.md").write_text("sibling\n")
    store.commit_paths((literal,), "test: literal path")
    assert (
        store._run(["show", "--format=", "--name-only", "HEAD"], check=True).stdout.strip()
        == literal
    )


def test_refresh_and_watch_cover_folder_updates_deletions_and_policy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "A.md").write_text("# Cache policy\n\nCache entries expire after sixty seconds.\n")
    (source / "B.md").write_text("# Retired policy\n\nRetired entries expire after one second.\n")
    workspace = Workspace.initialize(tmp_path / "wiki")
    sync_folder(
        workspace.root,
        source,
        category="notes",
        tags=("cache",),
        sensitivity=Sensitivity.LOCAL_ONLY,
    )
    _publish(workspace)
    (source / "A.md").write_text("# Cache policy\n\nCache entries expire after thirty seconds.\n")
    (source / "B.md").unlink()
    runner = CliRunner()
    result = runner.invoke(app, ["refresh", "--workspace", str(workspace.root)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "changed"
    assert (payload["folders"][0]["updated"], payload["folders"][0]["deleted"]) == (1, 1)
    assert str(source) not in result.stdout
    with sqlite3.connect(workspace.index_path) as connection:
        row = connection.execute(
            "SELECT category, sensitivity, tags_json FROM source_versions WHERE is_current=1"
        ).fetchone()
    assert row[:2] == ("notes", "local_only")
    assert "cache" in json.loads(row[2])
    unchanged = runner.invoke(app, ["refresh", "--workspace", str(workspace.root)])
    assert json.loads(unchanged.stdout)["status"] == "unchanged"
    watched = runner.invoke(app, ["watch", "--once", "--workspace", str(workspace.root)])
    assert watched.exit_code == 0, watched.output
    assert json.loads(watched.stdout)["changeset"]["status"] == "PROPOSED"


def test_version_routing_does_not_answer_from_another_product_or_version(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for filename, title, body in (
        ("old", "Spark Release 2.4.7", "This is a maintenance release with stability fixes."),
        ("new", "Spark Release 3.5.5", "This is a feature release with new SQL operators."),
        (
            "node",
            "Errors Node.js 22.14.0",
            "CERT_NOT_YET_VALID means the certificate is not yet valid.",
        ),
    ):
        (source / f"{filename}.md").write_text(f"# {title}\n\n{body}\n")
    workspace = Workspace.initialize(tmp_path / "wiki")
    sync_folder(workspace.root, source, sensitivity=Sensitivity.PUBLIC)
    _publish(workspace)
    answer = answer_question(workspace.root, "What kind of release is Spark 2.4.7?")
    assert answer["status"] == "answered", answer
    assert "maintenance" in answer["answer"], answer
    assert all("2.4.7" in item.get("section_path", "") for item in answer["citations"])
    for question in ("Does Node.js 3.5.5 exist?", "What changed in Spark 3.7.0?"):
        missing = answer_question(workspace.root, question)
        assert missing["status"] == "unknown", missing
        assert missing["citations"] == []
    node = answer_question(workspace.root, "What does CERT_NOT_YET_VALID mean in Node.js 22.14.0?")
    assert node["status"] == "answered", node
    assert "certificate" in node["answer"]


def test_wiki_fact_budget_covers_later_sections_and_preserves_exact_excerpts() -> None:
    content = "# Handbook\n\n## Intro\n\n" + "\n\n".join(f"Early fact {i}." for i in range(80))
    content += "\n\n## Recovery\n\nRestore the latest verified backup.\n"
    facts = _meaningful_paragraphs(content)
    assert len(facts) == 48
    assert any(f.quote == "Restore the latest verified backup." for f in facts)
    assert all(content[f.start : f.start + len(f.quote)] == f.quote for f in facts)


def test_linked_folder_sources_stay_stale_until_dependents_are_updated(tmp_path: Path) -> None:
    from memoryforge.compiler.linting import lint_workspace
    from memoryforge.compiler.refresh import refresh_workspace
    from memoryforge.storage.folder_dependencies import stale_folder_source_versions

    source = tmp_path / "source"
    source.mkdir()
    (source / "A.md").write_text("# Cache policy\n\nCache entries expire after sixty seconds.\n")
    (source / "B.md").write_text(
        "# Worker guide\n\nThe worker follows [Cache policy](A.md).\n\n"
        "Worker cache entries expire after sixty seconds.\n"
    )
    (source / "C.md").write_text("# Deployment guide\n\nDeploy using the [Worker guide](B.md).\n")
    workspace = Workspace.initialize(tmp_path / "wiki")
    sync_folder(workspace.root, source, sensitivity=Sensitivity.PUBLIC)
    _publish(workspace)
    assert lint_workspace(workspace.root)["status"] == "clean"
    (source / "A.md").write_text("# Cache policy\n\nCache entries expire after thirty seconds.\n")
    refresh_workspace(workspace.root)
    with sqlite3.connect(workspace.index_path) as connection:
        stale = stale_folder_source_versions(connection)
    assert len(stale) == 2
    issues = lint_workspace(workspace.root)["issues"]
    assert sum(issue["code"] == "stale_source_dependency" for issue in issues) == 2
    answer = answer_question(workspace.root, "When do worker cache entries expire?")
    assert all((c["source_id"], c["source_version"]) not in stale for c in answer["citations"])
    assert answer["status"] == "unknown", answer
    # Refresh alone must not acknowledge the dependency's new version.
    refresh_workspace(workspace.root)
    with sqlite3.connect(workspace.index_path) as connection:
        assert stale_folder_source_versions(connection) == stale
    (source / "B.md").write_text((source / "B.md").read_text().replace("sixty", "thirty"))
    (source / "C.md").write_text(
        (source / "C.md").read_text() + "\nReviewed the thirty-second policy.\n"
    )
    refresh_workspace(workspace.root)
    _publish(workspace)
    assert lint_workspace(workspace.root)["status"] == "clean"
    (source / "A.md").unlink()
    refresh_workspace(workspace.root)
    assert any(
        issue["code"] == "stale_source_dependency"
        for issue in lint_workspace(workspace.root)["issues"]
    )


@pytest.mark.parametrize("quote", ["Version 22.14.0", "There are four categories of errors:"])
def test_heading_or_unfinished_introduction_is_not_sufficient_evidence(
    tmp_path: Path, quote: str
) -> None:
    from memoryforge.query import query as query_module
    from tests.test_support_score import _citation

    support = query_module._support_score(
        tmp_path,
        "What are the categories of errors?",
        query_module._terms("categories errors"),
        [("wiki/pages/errors.md", _citation(quote))],
        symbol_matches=(),
        exact_symbol_fact_keys=set(),
        required_sources=1,
        code_page_paths=set(),
    )
    assert not support["sufficient"]
    assert "incomplete_evidence_fragment" in support["failed_hard_gates"]


def test_document_queries_require_requested_api_and_rank_classification(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.md").write_text(
        "# Runtime.js 4.2.1\n\n"
        "## ConnectionTracker\n\nTracks active connections for shutdown.\n\n"
        "## ERR_POOL_CLOSED\n\nThe pool no longer accepts work.\n\n"
        "## ERR_SOCKET_CLOSED\n\nThe socket no longer accepts work.\n\n"
        "## Runtime.match\n\n<!-- YAML added: v4.1.0 -->\n\n"
        "Primitive values are compared by identity.\n\n"
        "## Runtime.equal\n\nStability: 2 - Stable.\n"
    )
    (source / "release.md").write_text(
        "# Comet Release 4.2.1\n\n"
        "Comet 4.2.1 is a maintenance release containing stability fixes.\n\n"
        "The type of Comet data is inferred incorrectly after an upgrade.\n"
    )
    workspace = Workspace.initialize(tmp_path / "wiki")
    sync_folder(workspace.root, source, sensitivity=Sensitivity.PUBLIC)
    _publish(workspace)
    classification = answer_question(workspace.root, "What type of release is Comet 4.2.1?")
    assert classification["status"] == "answered", classification
    assert "maintenance release" in classification["answer"]
    for identifier, expected in (
        ("ConnectionTracker", "Tracks active connections"),
        ("ERR_POOL_CLOSED", "pool no longer"),
    ):
        result = answer_question(
            workspace.root, f"What is {identifier} in Runtime.js 4.2.1?", verify=True
        )
        assert result["status"] == "answered", result
        assert expected in result["answer"]
        assert all(identifier in c.get("section_path", "") for c in result["citations"])
    missing_unversioned = answer_question(workspace.root, "What is ERR_POOL_FULL?")
    assert missing_unversioned["status"] == "unknown", missing_unversioned
    assert missing_unversioned["answer"] == "不知道"
    assert missing_unversioned["citations"] == []
    for identifier in ("MissingTracker", "ERR_POOL_FULL"):
        result = answer_question(workspace.root, f"What is {identifier} in Runtime.js 4.2.1?")
        assert result["status"] == "unknown", result
        assert result["citations"] == []

    stability = answer_question(
        workspace.root, "What is the stability level of Runtime.match in Runtime.js 4.2.1?"
    )
    assert stability["status"] == "unknown", stability
    assert stability["answer"] == "不知道"
    assert stability["citations"] == []

    supported_stability = answer_question(
        workspace.root, "What is the stability level of Runtime.equal in Runtime.js 4.2.1?"
    )
    assert supported_stability["status"] == "answered", supported_stability
    assert "Stability: 2 - Stable" in supported_stability["answer"]
