"""A fake PubMed / iCite / PubTator world small enough to reason about by hand."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
import yaml

from pubmed_corpus.eutils import EUtils
from pubmed_corpus.http import Http

MESH = {
    "uid": "68054972",
    "ds_meshui": "D054972",
    "ds_meshterms": ["Postural Orthostatic Tachycardia Syndrome", "Postural Tachycardia Syndrome",
                     "Syndrome, Postural Tachycardia"],
    "ds_scopenote": "A syndrome of ORTHOSTATIC INTOLERANCE combined with excessive upright TACHYCARDIA.",
    "ds_previousindexing": ["Posture (1982-2008)", "Tachycardia (1982-2008)"],
    "ds_yearintroduced": "2009",
}


def record(pmid, title, abstract="", year="2020", pub_types=("Journal Article",), pmcid=""):
    return {"pmid": str(pmid), "title": title, "abstract": abstract, "year": year, "pub_types": list(pub_types), "pmcid": pmcid}


def pubmed_xml(recs) -> bytes:
    parts = ["<PubmedArticleSet>"]
    for r in recs:
        types = "".join(f"<PublicationType>{escape(t)}</PublicationType>" for t in r["pub_types"])
        ab = f"<Abstract><AbstractText>{escape(r['abstract'])}</AbstractText></Abstract>" if r["abstract"] else ""
        pmc = f'<ArticleId IdType="pmc">{r["pmcid"]}</ArticleId>' if r["pmcid"] else ""
        parts.append(
            f"<PubmedArticle><MedlineCitation><PMID>{r['pmid']}</PMID><Article><Journal><JournalIssue><PubDate>"
            f"<Year>{r['year']}</Year></PubDate></JournalIssue><ISOAbbreviation>J Test</ISOAbbreviation></Journal>"
            f"<ArticleTitle>{escape(r['title'])}</ArticleTitle>{ab}<Language>eng</Language>"
            f"<PublicationTypeList>{types}</PublicationTypeList></Article></MedlineCitation>"
            f"<PubmedData><ArticleIdList><ArticleId IdType=\"pubmed\">{r['pmid']}</ArticleId>{pmc}</ArticleIdList>"
            f"</PubmedData></PubmedArticle>")
    parts.append("</PubmedArticleSet>")
    return "".join(parts).encode()


def biocjson(pmid, annotations, full_text=False):
    passages = [{"infons": {"section_type": "TITLE"}, "annotations": []},
                {"infons": {"section_type": "ABSTRACT"}, "annotations": [
                    {"text": text, "infons": {"identifier": cid, "type": ctype, "name": text}} for text, cid, ctype in annotations]}]
    if full_text:
        passages.append({"infons": {"section_type": "INTRO"}, "annotations": []})
    return {"_id": f"{pmid}|None", "passages": passages}


class FakeWorld:
    def __init__(self):
        self.records: dict[str, dict] = {}
        self.searches: dict[str, list[str]] = {}
        self.related: dict[str, list[tuple[str, int]]] = {}
        self.icite: dict[str, dict] = {}
        self.docs: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def add(self, *recs):
        for r in recs:
            self.records[r["pmid"]] = r

    def fetch(self, method, url, params):
        self.calls.append((method, url, dict(params)))
        path = urllib.parse.urlsplit(url).path
        if path.endswith("/esearch.fcgi") and params.get("db") == "mesh":
            return json.dumps({"esearchresult": {"idlist": [MESH["uid"]]}}).encode()
        if path.endswith("/esummary.fcgi"):
            return json.dumps({"result": {"uids": [MESH["uid"]], MESH["uid"]: MESH}}).encode()
        if path.endswith("/esearch.fcgi"):
            term = params["term"]
            if term not in self.searches:
                raise AssertionError(f"unexpected search {term!r}")
            ids = self.searches[term]
            return json.dumps({"esearchresult": {"count": str(len(ids)), "idlist": ids[: int(params["retmax"])]}}).encode()
        if path.endswith("/efetch.fcgi"):
            ids = params["id"].split(",")
            return pubmed_xml([self.records[i] for i in ids if i in self.records])
        if path.endswith("/elink.fcgi"):
            pmid = params["id"]
            links = [{"id": p, "score": s} for p, s in self.related.get(pmid, [])]
            return json.dumps({"linksets": [{"ids": [pmid], "linksetdbs": [{"linkname": "pubmed_pubmed", "links": links}]}]}).encode()
        if "icite" in url:
            data = [{"pmid": int(p), **self.icite[p]} for p in params["pmids"].split(",") if p in self.icite]
            return json.dumps({"data": data}).encode()
        if "pubtator3-api" in url:
            return json.dumps({"PubTator3": [self.docs[p] for p in params["pmids"].split(",") if p in self.docs]}).encode()
        raise AssertionError(f"unexpected request {method} {url} {params}")

    def clients(self, cache_dir=None):
        http = Http(cache_dir, fetch=self.fetch, sleep=lambda s: None)
        return http, EUtils(http, api_key="", email="")


@pytest.fixture
def world():
    return FakeWorld()


def write_project(root: Path, **overrides) -> Path:
    config = {
        "name": "Test syndrome",
        "mesh": "D054972",
        "pubtator": {"file": "disease2pubtator3.gz", "concepts": ["MESH:D054972"]},
        "seeds": [{"pmid": "100", "key": "seedA"}, {"pmid": "200", "key": "seedB"}],
        "expansion": {"routes": ["references", "cited_by", "related"], "related_top_k": 2},
        "screening": {"criteria": "Include papers about the test syndrome."},
        "exclude_retracted": True,
        "references": [],
    }
    config.update(overrides)
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    return root
