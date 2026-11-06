"""One real paper through each step, offline.

The paper is Gibbons et al. 2013, "Structural and functional small fiber abnormalities in the
neuropathic postural tachycardia syndrome" (PMID 24386408, open access). `tests/fixtures/gibbons2013/`
holds the real answers PubMed, iCite and PubTator gave for it; the tests replay them, so they need no
network and the expected values below never change.
"""

from pathlib import Path

import yaml

from pubmed_corpus import corpus, icite, pubtator, query
from pubmed_corpus.eutils import EUtils
from pubmed_corpus.http import Http
from pubmed_corpus.project import Project, write_tsv
from pubmed_corpus.steps import METADATA_COLUMNS

FIX = Path(__file__).parent / "fixtures" / "gibbons2013"
PMID = "24386408"
POTS = "MESH:D054972"


def recorded(method, url, params):
    """Answer each request with the recorded response for this paper."""
    if "efetch" in url:
        name = "pubmed.xml"
    elif "elink" in url:
        name = "related.json"
    elif "esearch" in url and params.get("db") == "mesh":
        name = "mesh_esearch.json"
    elif "esummary" in url:
        name = "mesh_esummary.json"
    elif "icite" in url:
        name = "icite.json"
    elif "pubtator3-api" in url:
        name = "pubtator.json"
    else:
        raise AssertionError(f"no recording for {url}")
    return (FIX / name).read_bytes()


def http():
    return Http(fetch=recorded)


def eutils():
    return EUtils(http(), api_key="", email="")


def wordings():
    return pubtator.seed_mentions(pubtator.export_documents(http(), [PMID]), [POTS])


def test_pubmed_record():
    (rec,) = eutils().fetch_records([PMID])

    assert rec["title"] == ("Structural and functional small fiber abnormalities in the neuropathic "
                            "postural tachycardia syndrome.")
    assert rec["year"] == "2013"
    assert rec["pmcid"] == "PMC3874039"
    assert rec["abstract"] and not rec["retracted"]


def test_how_the_paper_names_the_disease():
    counts = {text: r["mentions"] for text, r in wordings().items()}

    assert counts == {"POTS": 5, "neuropathic POTS": 7, "Neuropathic POTS": 1,
                      "neuropathic postural tachycardia syndrome": 2, "postural tachycardia": 1}


def test_query_drafted_from_this_paper():
    clauses, _ = query.draft(eutils().mesh_descriptor("D054972"), wordings())
    got = {c["id"]: c["query"] for c in clauses}

    assert got["mesh_heading"] == '"Postural Orthostatic Tachycardia Syndrome"[MeSH Terms]'
    # "neuropathic postural tachycardia syndrome" is already covered by the shorter "postural tachycardia"
    assert got["tiab_seed_wordings"] == '"postural tachycardia"[tiab]'
    # "POTS" and "neuropathic POTS" become one guarded acronym clause
    assert got["tiab_acronym_guarded"].startswith("(POTS[tiab]) AND (")


def test_references_citing_papers_and_similar_articles():
    links = icite.links(http(), [PMID])[PMID]
    similar = eutils().related(PMID, top_k=3)

    assert len(links["references"]) == 44
    assert len(links["cited_by"]) == 80
    assert len(similar) == 3
    assert similar[0][0] == "36349067"   # most similar: Zhang 2022, another of the POTS seed papers


def test_the_paper_alone_as_a_corpus(tmp_path):
    """With this paper as the only seed and nothing else found, the corpus is that one paper."""
    root = tmp_path / "one"
    root.mkdir()
    (root / "project.yaml").write_text(yaml.safe_dump({"name": "POTS", "mesh": "D054972",
                                                      "seeds": [{"pmid": PMID, "key": "gibbons2013"}]}))
    (root / "query.yaml").write_text(yaml.safe_dump({"clauses": [
        {"id": "mesh_heading", "status": "approved", "query": '"Postural Orthostatic Tachycardia Syndrome"[MeSH Terms]'}]}))
    write_tsv(root / "out" / "query_hits.tsv", ["clause", "pmid"], [{"clause": "mesh_heading", "pmid": PMID}])
    write_tsv(root / "out" / "expansion.tsv", ["seed", "route", "pmid", "score"], [])
    (rec,) = eutils().fetch_records([PMID])
    write_tsv(root / "out" / "metadata.tsv", METADATA_COLUMNS,
              [{**rec, "missing": False, "has_abstract": bool(rec["abstract"])}])

    kept, dropped, manifest = corpus.assemble(Project.load(root))

    assert len(kept) == 1 and not dropped
    paper = kept[0]
    assert (paper["pmid"], paper["tier"], paper["clauses"], paper["pmcid"]) == (PMID, "seed", ["mesh_heading"], "PMC3874039")
    assert manifest["counts"] == {"seed": 1, "core": 0, "fulltext": 0, "expansion": 0, "total": 1}
