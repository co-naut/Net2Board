"""The carve's export contract, enforced at fast tier (ADR-0027, #119):
``git archive HEAD`` honors export-ignore, so the archive is exactly what a
carve publishes. Two regressions this catches, both invisible privately and
fatal only in the public tree after publication: the ``docs/`` export-ignore
rule weakened or removed, and shipped markdown linking a path the export
does not carry."""

import subprocess
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

import test_adr_docs

REPO_ROOT = test_adr_docs.REPO_ROOT


@pytest.fixture(scope="module")
def export(tmp_path_factory) -> Path:
    """The tree exactly as a carve would publish it: HEAD's archive with
    export-ignore applied, extracted once for the module's assertions."""
    archive = subprocess.run(
        ["git", "archive", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    destination = tmp_path_factory.mktemp("carve-export") / "tree"
    with tarfile.open(fileobj=BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")
    return destination


def test_export_carries_no_docs(export: Path):
    leaked = sorted(p.relative_to(export).as_posix() for p in export.rglob("docs"))
    assert not leaked, f"the export ships docs/ paths: {leaked}"


def test_export_markdown_path_refs_resolve(export: Path):
    missing = test_adr_docs.unresolved_path_refs(export)
    assert not missing, (
        f"exported markdown references paths the export lacks: {missing}"
    )
