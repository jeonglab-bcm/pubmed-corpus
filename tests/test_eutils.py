import datetime as dt
import json

import pytest

from pubmed_corpus.eutils import ESEARCH_CAP, EUtils, SearchError, parse_pubmed_xml, parse_related
from pubmed_corpus.http import Http

from conftest import pubmed_xml, record


class DatedSearch:
    """PubMed with ``n`` records spread one per day from 1960-01-01."""

    def __init__(self, n):
        start = dt.date(1960, 1, 1)
        self.days = [(start + dt.timedelta(days=i), str(1000 + i)) for i in range(n)]
        self.requests = 0

    def fetch(self, method, url, params):
        self.requests += 1
        if "mindate" in params:
            lo = dt.datetime.strptime(params["mindate"], "%Y/%m/%d").date()
            hi = dt.datetime.strptime(params["maxdate"], "%Y/%m/%d").date()
            ids = [p for d, p in self.days if lo <= d <= hi]
        else:
            ids = [p for _, p in self.days]
        return json.dumps({"esearchresult": {"count": str(len(ids)), "idlist": ids[: int(params["retmax"])]}}).encode()


def test_small_search_is_one_request():
    fake = DatedSearch(50)
    eu = EUtils(Http(fetch=fake.fetch), api_key="", email="")
    assert eu.search("x") == [str(1000 + i) for i in range(50)]
    assert fake.requests == 1


def test_search_over_the_cap_splits_by_date_and_finds_everything():
    fake = DatedSearch(ESEARCH_CAP * 2 + 123)
    eu = EUtils(Http(fetch=fake.fetch, sleep=lambda s: None), api_key="", email="")
    got = eu.search("x")
    assert len(got) == ESEARCH_CAP * 2 + 123
    assert got == sorted(got, key=int)
    assert fake.requests > 3


def test_search_error_is_raised():
    def fetch(method, url, params):
        return json.dumps({"esearchresult": {"ERROR": "Invalid query", "count": "0"}}).encode()
    with pytest.raises(SearchError):
        EUtils(Http(fetch=fetch), api_key="", email="").search("bad(")


def test_parse_pubmed_xml_fields():
    xml = pubmed_xml([
        record(1, "A <b>study</b> & more", "Background text.", "2019", pmcid="PMC9"),
        record(2, "Retracted one", "", "2001", pub_types=("Journal Article", "Retracted Publication")),
    ]).replace(b"&lt;b&gt;study&lt;/b&gt;", b"<i>study</i>")
    a, b = parse_pubmed_xml(xml)
    assert a["pmid"] == "1" and a["title"] == "A study & more" and a["abstract"] == "Background text."
    assert a["year"] == "2019" and a["pmcid"] == "PMC9" and not a["retracted"] and a["language"] == "eng"
    assert b["retracted"] and b["abstract"] == ""


def test_parse_pubmed_xml_medline_date_and_labels():
    xml = (b"<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>5</PMID><Article><Journal><JournalIssue><PubDate>"
           b"<MedlineDate>1998 Nov-Dec</MedlineDate></PubDate></JournalIssue></Journal><ArticleTitle>T</ArticleTitle>"
           b"<Abstract><AbstractText Label=\"AIMS\">Aim.</AbstractText><AbstractText Label=\"RESULTS\">Res.</AbstractText>"
           b"</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>")
    (rec,) = parse_pubmed_xml(xml)
    assert rec["year"] == "1998"
    assert rec["abstract"] == "AIMS: Aim. RESULTS: Res."


def test_parse_related_sorts_by_score_and_drops_self():
    payload = {"linksets": [{"linksetdbs": [{"linkname": "pubmed_pubmed", "links": [
        {"id": "7", "score": 10}, {"id": "5", "score": 99}, {"id": "1", "score": 1000}]},
        {"linkname": "pubmed_pubmed_five", "links": [{"id": "8", "score": 1}]}]}]}
    assert parse_related(payload, "1") == [("5", 99), ("7", 10)]
