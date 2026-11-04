"""Every committed project must rebuild byte-identically from its committed inputs, offline."""

from pathlib import Path

import pytest

from pubmed_corpus import cli

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = sorted(p.parent for p in (ROOT / "projects").glob("*/out/corpus.tsv"))


@pytest.mark.skipif(not PROJECTS, reason="no assembled project committed yet")
@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.name)
def test_project_replays(project):
    assert cli.main(["assemble", str(project), "--check"]) == 0
    assert cli.main(["report", str(project), "--check"]) == 0
