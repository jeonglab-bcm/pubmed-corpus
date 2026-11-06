"""A small, readable example of the whole idea: input files in, corpus out.

No network and no fake servers. The test writes the files a finished run would
leave behind (query hits, PubTator rows, seed links, PubMed records, screening
decisions) and checks what `assemble` makes of them.

    paper 1  seed
    paper 2  found by the approved query clause          -> core
    paper 9  found only by a proposed clause             -> not in the corpus
    paper 3  PubTator tags it "POTS"                      -> fulltext
    paper 4  PubTator tags it "postural instability"      -> not in the corpus (wording rejected)
    paper 5  cited by the seed, screened in               -> expansion
    paper 6  cited by the seed, screened out              -> excluded
"""

import json

import pytest
import yaml

from pubmed_corpus import corpus, mentions
from pubmed_corpus.project import Project, write_tsv
from pubmed_corpus.steps import METADATA_COLUMNS


def make_project(root, decisions):
    root.mkdir()
    (root / "project.yaml").write_text(yaml.safe_dump({
        "name": "Example", "mesh": "D054972", "seeds": [{"pmid": "1", "key": "seed1"}]}))
    (root / "query.yaml").write_text(yaml.safe_dump({"clauses": [
        {"id": "name", "status": "approved", "query": '"postural tachycardia syndrome"[tiab]'},
        {"id": "old_names", "status": "proposed", "query": '"soldier\'s heart"[tiab]'},
    ]}))
    (root / "mentions.yaml").write_text(yaml.safe_dump({"approve": ["pots"], "reject": ["postural instability"]}))

    out = root / "out"
    write_tsv(out / "query_hits.tsv", ["clause", "pmid"],
              [{"clause": "name", "pmid": "2"}, {"clause": "old_names", "pmid": "9"}])
    write_tsv(out / "pubtator_hits.tsv", ["pmid", "type", "concept", "mentions", "resource"],
              [{"pmid": "3", "type": "Disease", "concept": "MESH:D054972", "mentions": "POTS"},
               {"pmid": "4", "type": "Disease", "concept": "MESH:D054972", "mentions": "postural instability"}])
    write_tsv(out / "expansion.tsv", ["seed", "route", "pmid", "score"],
              [{"seed": "1", "route": "cited_by", "pmid": "5"}, {"seed": "1", "route": "cited_by", "pmid": "6"}])
    write_tsv(out / "metadata.tsv", METADATA_COLUMNS,
              [{"pmid": p, "missing": False, "year": "2020", "retracted": False, "has_abstract": True,
                "title": f"Paper {p}"} for p in ("1", "2", "3", "5", "6")])

    (root / "screen").mkdir()
    (root / "screen" / "decisions.jsonl").write_text("".join(json.dumps(d) + "\n" for d in decisions))
    return Project.load(root)


SCREENED = [
    {"pmid": "5", "decision": "include", "reason": "orthostatic intolerance cohort", "screener": "agent"},
    {"pmid": "6", "decision": "exclude", "reason": "survey software", "screener": "agent"},
]


def test_each_paper_gets_the_right_tier(tmp_path):
    project = make_project(tmp_path / "example", SCREENED)

    kept, dropped, manifest = corpus.assemble(project)

    assert {r["pmid"]: r["tier"] for r in kept} == {"1": "seed", "2": "core", "3": "fulltext", "5": "expansion"}
    assert {r["pmid"]: r["reason"] for r in dropped} == {"6": "screened out: survey software"}
    assert manifest["counts"]["total"] == 4


def test_an_unscreened_paper_stops_assembly(tmp_path):
    project = make_project(tmp_path / "example", SCREENED[:1])    # paper 6 has no decision

    with pytest.raises(corpus.AssemblyError, match="no screening decision"):
        corpus.assemble(project)


def test_wording_decisions():
    rules = mentions.Rules.from_dict({"approve": ["pots"], "reject": ["postural instability"]})

    assert rules.decide("POTS") == "approve"                    # case does not matter
    assert rules.decide("Postural instability") == "reject"
    assert rules.decide("syncope") == "undecided"               # nobody has decided yet
