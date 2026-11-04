"""The whole skill, step by step, against the fake world: what the agent would run, in order."""

import functools
import json

import pytest
import yaml

from pubmed_corpus import cli, corpus, pubtator, screen, steps
from pubmed_corpus.project import Project, read_tsv

from conftest import biocjson, record, write_project
from test_pubtator import FlakyServer, gz_members

BULK = [
    "300\tDisease\tMESH:D054972\tPOTS\tPubTator3",
    "500\tDisease\tMESH:D054972\tpostural tachycardia syndrome\tPubTator3",
    "501\tDisease\tMESH:D054972\tpostural instability\tPubTator3",
    "502\tDisease\tMESH:D054972\tPOTS|postural instability\tPubTator3",
    "503\tDisease\tMESH:D054972\tdystonic posturing\tPubTator3",
    "504\tDisease\tMESH:D000001\tPOTS\tPubTator3",
]
CLAUSE_HITS = {
    "mesh_heading": ["100", "200", "300"],
    "tiab_entry_terms": ["100", "300", "301"],
    "tiab_seed_wordings": ["100", "300", "301", "302"],
    "tiab_acronym_guarded": ["303"],
    "mesh_previous_indexing_1982_2008": ["400", "401"],
}


@pytest.fixture
def setup(tmp_path, world, monkeypatch):
    root = write_project(tmp_path / "proj", references=[{"name": "ref", "path": "reference/ref.txt", "description": "Toy."}])
    (root / "reference").mkdir()
    (root / "reference" / "ref.txt").write_text("# toy reference\n100\n300\n301\n400\n999\n")
    world.docs = {
        "100": biocjson("100", [("POTS", "MESH:D054972", "Disease")] * 3 + [("postural tachycardia", "MESH:D054972", "Disease")],
                        full_text=True),
        "200": biocjson("200", [("Postural tachycardia syndrome", "MESH:D054972", "Disease")]),
    }
    world.icite = {"100": {"references": [300, 600, 601], "cited_by": [602, 200]}}
    world.related = {"100": [("603", 50), ("604", 40), ("605", 30)], "200": [("500", 9)]}
    world.add(
        record(100, "Seed A on POTS"), record(200, "Seed B on POTS"),
        *[record(p, f"Core paper {p}") for p in (300, 301, 302, 303)],
        record(400, "Posture and tachycardia in 1990", year="1990"),
        record(500, "Long COVID review"), record(502, "Fatigue review"),
        record(600, "REDCap: a metadata-driven methodology", "Software for research data capture."),
        record(601, "Teenagers", "We studied POTS in adolescents."),
        record(602, "Orthostatic intolerance after COVID-19", "Heart rate rose on standing."),
        record(603, "Withdrawn study", pub_types=("Journal Article", "Retracted Publication")),
        record(604, "Gut motility", "Gastric emptying in postural tachycardia syndrome patients."),
    )
    srv = FlakyServer(gz_members(BULK), drops=1, after=20)
    monkeypatch.setattr(steps.pubtator, "head", lambda url: {"url": url, "last_modified": "Tue, 06 Oct 2026 11:44:00 GMT",
                                                             "content_length": 0, "etag": ""})
    monkeypatch.setattr(steps.pubtator, "scan_bulk",
                        functools.partial(pubtator.scan_bulk, open_range=srv.open, sleep=lambda s: None))
    monkeypatch.setattr(steps, "clients", lambda project, refresh=False: world.clients(project.cache))
    return root, world


def run(*args):
    return cli.main([*args])


def test_full_skill_run(setup, capsys):
    root, world = setup
    p = str(root)

    # 1. draft the query from MeSH and the seeds
    assert run("terms", p) == 0
    project = Project.load(root)
    clauses = {c["id"]: c for c in project.clauses()}
    assert set(clauses) == set(CLAUSE_HITS)
    assert clauses["mesh_previous_indexing_1982_2008"]["status"] == "proposed"
    seeds = read_tsv(root / "out" / "seeds.tsv")
    assert [s["pubtator_text"] for s in seeds] == ["full text", "abstract"]

    # 2. run it (the agent's approval checkpoint is the query.yaml file itself)
    world.searches = {clauses[cid]["query"]: ids for cid, ids in CLAUSE_HITS.items()}
    assert run("query", p) == 0
    counts = {r["clause"]: r["hits"] for r in read_tsv(root / "out" / "query_counts.tsv")}
    assert counts["mesh_previous_indexing_1982_2008"] == "2"

    # 3. PubTator bulk scan (one dropped connection along the way)
    assert run("pubtator", p) == 0
    assert {r["pmid"] for r in read_tsv(root / "out" / "pubtator_hits.tsv")} == {"300", "500", "501", "502", "503"}

    # 4. wording audit: blocks until frequent wordings are decided
    (root / "mentions.yaml").write_text(yaml.safe_dump({"min_papers_to_decide": 2, "approve": ["pots"]}))
    assert run("mentions", p) == 1                                   # "postural instability" (2 papers) undecided
    with pytest.raises(corpus.AssemblyError, match="need a decision"):
        corpus.assemble(Project.load(root))
    (root / "mentions.yaml").write_text(yaml.safe_dump({
        "min_papers_to_decide": 2, "approve": ["pots", "postural tachycardia syndrome"], "reject": ["postural instability"]}))
    assert run("mentions", p) == 0
    audit = {r["wording"]: r for r in read_tsv(root / "out" / "mention_audit.tsv")}
    assert audit["dystonic posturing"]["decision"] == "undecided"   # 1 paper: below threshold, dropped

    # 5. expansion and metadata
    assert run("expand", p) == 0
    seeds_tsv = {r["key"]: r for r in read_tsv(root / "out" / "expansion_seeds.tsv")}
    assert seeds_tsv["seedB"]["icite_found"] == "0" and seeds_tsv["seedA"]["related"] == "2"
    assert run("metadata", p) == 0
    meta = {r["pmid"]: r for r in read_tsv(root / "out" / "metadata.tsv")}
    assert meta["999"]["missing"] == "1" and meta["603"]["retracted"] == "1" and "400" in meta

    # 6. screening: rule first, then the agent's decisions
    with pytest.raises(corpus.AssemblyError, match="no screening decision"):
        corpus.assemble(Project.load(root))
    capsys.readouterr()
    assert run("screen-queue", p) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {"to_screen": 5, "already_decided": 0, "rule_included": 2, "queued": 2}
    queue = [json.loads(line) for line in (root / "screen" / "queue.jsonl").read_text().splitlines()]
    assert [q["pmid"] for q in queue] == ["600", "602"]
    assert queue[0]["found_via"] == ["seedA:references"]
    with (root / "screen" / "decisions.jsonl").open("a") as fh:
        fh.write(json.dumps({"pmid": "600", "decision": "exclude", "reason": "software tool", "screener": "agent"}) + "\n")
        fh.write(json.dumps({"pmid": "602", "decision": "include", "reason": "orthostatic intolerance", "screener": "agent"}) + "\n")
    assert run("screen-queue", p) == 0                                # idempotent: nothing new
    assert (root / "screen" / "queue.jsonl").read_text() == ""

    # 7. freeze and report
    assert run("assemble", p) == 0
    rows = {r["pmid"]: r for r in read_tsv(root / "out" / "corpus.tsv")}
    tiers = {t: sorted(p for p, r in rows.items() if r["tier"] == t) for t in corpus.TIERS}
    assert tiers == {"seed": ["100", "200"], "core": ["300", "301", "302", "303"], "fulltext": ["500", "502"],
                     "expansion": ["601", "602", "604"]}
    assert rows["300"]["clauses"] == "mesh_heading;tiab_entry_terms;tiab_seed_wordings"
    assert rows["300"]["pubtator_wordings"] == "pots" and rows["300"]["expansion"] == "seedA:references"
    assert rows["601"]["screen"] == "rule" and rows["602"]["screen"] == "include"
    excluded = {r["pmid"]: r["reason"] for r in read_tsv(root / "out" / "excluded.tsv")}
    assert excluded == {"600": "screened out: software tool", "603": "retracted"}
    assert run("assemble", p, "--check") == 0
    assert run("report", p) == 0
    report = (root / "REPORT.md").read_text()
    assert "| + fulltext | 3 | 60.0% |" in report
    assert "Proposed clause `mesh_previous_indexing_1982_2008` would recover 1 of them." in report
    assert "| postural instability | 2 | 2 |" in report
    assert "| seedB | 200 | **no** | 0 | 0 | 1 |" in report
    assert run("report", p, "--check") == 0

    # 8. a changed decision is caught by --check
    with (root / "screen" / "decisions.jsonl").open("a") as fh:
        fh.write(json.dumps({"pmid": "605", "decision": "include", "reason": "x", "screener": "agent"}) + "\n")
    assert run("assemble", p, "--check") == 1                         # manifest input hash changed
    with (root / "screen" / "decisions.jsonl").open("a") as fh:
        fh.write(json.dumps({"pmid": "602", "decision": "exclude", "reason": "changed mind", "screener": "agent"}) + "\n")
    assert run("assemble", p) == 2                                     # contradictory decisions are refused

    # status works on a finished project
    capsys.readouterr()
    assert run("status", p) == 0
    assert "corpus     assembled" in capsys.readouterr().out


def test_terms_does_not_overwrite_an_edited_query(setup):
    root, world = setup
    (root / "query.yaml").write_text("clauses:\n- {id: mine, status: approved, query: 'x[tiab]'}\n")
    assert run("terms", str(root)) == 0
    assert "mine" in (root / "query.yaml").read_text()
    assert "mesh_heading" in (root / "query.draft.yaml").read_text()


def test_decisions_are_validated(setup):
    root, _ = setup
    (root / "screen").mkdir()
    (root / "screen" / "decisions.jsonl").write_text('{"pmid": "1", "decision": "maybe", "reason": "r", "screener": "a"}\n')
    with pytest.raises(corpus.AssemblyError, match="decision must be one of"):
        corpus.load_decisions(Project.load(root))
    (root / "screen" / "decisions.jsonl").write_text('{"pmid": "1", "decision": "include", "screener": "a"}\n')
    with pytest.raises(corpus.AssemblyError, match="`reason` is required"):
        corpus.load_decisions(Project.load(root))


def test_records_cache_round_trip(tmp_path):
    project = Project(tmp_path, {"name": "x", "mesh": "D1", "seeds": ["1"]})
    screen.save_records(project, {"1": {"title": "t"}})
    assert screen.load_records(project) == {"1": {"title": "t"}}
