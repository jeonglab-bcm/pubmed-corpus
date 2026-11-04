"""NCBI E-utilities: PubMed search, records, related articles and MeSH descriptors.

Searches and links are always fetched live (they change as PubMed grows);
full records are cached per PMID by the metadata step.

ESearch returns at most the first 10,000 PubMed records for a query, and
``retstart`` cannot page past that (NCBI E-utilities documentation,
https://www.ncbi.nlm.nih.gov/books/NBK25499/). ``search`` therefore splits a
query that exceeds the cap into publication-date ranges, halving each range
until every piece fits, and checks that the pieces add up to the total.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import xml.etree.ElementTree as ET
from typing import Iterable

from .http import Http

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ESEARCH_CAP = 10_000
EFETCH_BATCH = 200
EARLIEST = dt.date(1000, 1, 1)
LATEST = dt.date(2100, 12, 31)   # PubMed holds some issues dated after today


class SearchError(RuntimeError):
    pass


class EUtils:
    def __init__(self, http: Http, *, api_key: str | None = None, email: str | None = None, tool: str = "pubmed-corpus"):
        self.http = http
        self.api_key = api_key if api_key is not None else os.environ.get("NCBI_API_KEY")
        self.email = email if email is not None else os.environ.get("NCBI_TOOL_EMAIL")
        self.tool = tool
        host = "eutils.ncbi.nlm.nih.gov"
        http.intervals.setdefault(host, 0.11 if self.api_key else 0.34)

    def _common(self) -> dict:
        params = {"tool": self.tool}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    # -- search ---------------------------------------------------------
    def _esearch(self, term: str, retmax: int, mindate: dt.date | None = None, maxdate: dt.date | None = None) -> tuple[int, list[str]]:
        params = {**self._common(), "db": "pubmed", "term": term, "retmax": retmax, "retmode": "json"}
        if mindate or maxdate:
            params.update(datetype="pdat", mindate=(mindate or EARLIEST).strftime("%Y/%m/%d"),
                          maxdate=(maxdate or LATEST).strftime("%Y/%m/%d"))
        result = self.http.get_json(f"{BASE}/esearch.fcgi", params, method="POST", cache=False)["esearchresult"]
        if "ERROR" in result:
            raise SearchError(f"ESearch error for {term!r}: {result['ERROR']}")
        return int(result["count"]), [str(x) for x in result.get("idlist", [])]

    def count(self, term: str) -> int:
        return self._esearch(term, 0)[0]

    def search(self, term: str) -> list[str]:
        """Every PMID matching ``term``, sorted numerically."""
        total, ids = self._esearch(term, ESEARCH_CAP - 1)
        if total < ESEARCH_CAP:
            return _sorted_ids(ids)
        found: set[str] = set()
        self._search_range(term, EARLIEST, LATEST, found)
        if len(found) < total:
            raise SearchError(f"date-split search found {len(found)} of {total} records for {term!r}; "
                              "some records may lack a publication date")
        return _sorted_ids(found)

    def _search_range(self, term: str, start: dt.date, end: dt.date, found: set[str]) -> None:
        count, ids = self._esearch(term, ESEARCH_CAP - 1, start, end)
        if count < ESEARCH_CAP:
            found.update(ids)
            return
        if start >= end:
            raise SearchError(f"{count} records on {start} alone exceed the ESearch cap for {term!r}")
        mid = start + (end - start) // 2
        self._search_range(term, start, mid, found)
        self._search_range(term, mid + dt.timedelta(days=1), end, found)

    # -- records --------------------------------------------------------
    def fetch_records(self, pmids: Iterable[str]) -> list[dict]:
        pmids = _sorted_ids(set(pmids))
        out: list[dict] = []
        for i in range(0, len(pmids), EFETCH_BATCH):
            chunk = pmids[i : i + EFETCH_BATCH]
            raw = self.http.request(f"{BASE}/efetch.fcgi", {**self._common(), "db": "pubmed", "id": ",".join(chunk),
                                                            "retmode": "xml"}, method="POST", cache=False)
            out.extend(parse_pubmed_xml(raw))
        return out

    # -- links ----------------------------------------------------------
    def related(self, pmid: str, top_k: int) -> list[tuple[str, int]]:
        """PubMed similar articles for ``pmid`` as (pmid, score), best first."""
        params = {**self._common(), "dbfrom": "pubmed", "db": "pubmed", "id": pmid, "cmd": "neighbor_score",
                  "linkname": "pubmed_pubmed", "retmode": "json"}
        payload = self.http.get_json(f"{BASE}/elink.fcgi", params, cache=False)
        return parse_related(payload, pmid)[:top_k]

    # -- MeSH -----------------------------------------------------------
    def mesh_descriptor(self, mesh_ui: str) -> dict:
        """The MeSH record for a descriptor UI such as D054972."""
        params = {**self._common(), "db": "mesh", "term": f"{mesh_ui}[MeSH Unique ID]", "retmode": "json"}
        ids = self.http.get_json(f"{BASE}/esearch.fcgi", params, cache=False)["esearchresult"].get("idlist", [])
        if not ids:
            raise SearchError(f"no MeSH descriptor {mesh_ui}")
        payload = self.http.get_json(f"{BASE}/esummary.fcgi", {**self._common(), "db": "mesh", "id": ids[0], "retmode": "json"},
                                     cache=False)
        return parse_mesh_summary(payload, mesh_ui)


# -- parsers (pure, tested offline) --------------------------------------
def _sorted_ids(ids: Iterable[str]) -> list[str]:
    return sorted({str(i) for i in ids}, key=int)


def parse_related(payload: dict, pmid: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for linkset in payload.get("linksets", []):
        for db in linkset.get("linksetdbs", []):
            if db.get("linkname") != "pubmed_pubmed":
                continue
            for link in db.get("links", []):
                if isinstance(link, dict):
                    lid, score = str(link["id"]), int(link.get("score", 0))
                else:
                    lid, score = str(link), 0
                if lid != str(pmid):
                    out.append((lid, score))
    out.sort(key=lambda x: (-x[1], int(x[0])))
    return out


def parse_mesh_summary(payload: dict, mesh_ui: str) -> dict:
    result = payload["result"]
    rec = result[result["uids"][0]]
    if rec.get("ds_meshui") != mesh_ui:
        raise SearchError(f"MeSH lookup for {mesh_ui} returned {rec.get('ds_meshui')}")
    return {
        "ui": rec["ds_meshui"],
        "heading": (rec.get("ds_meshterms") or [""])[0],
        "entry_terms": list(rec.get("ds_meshterms") or []),
        "scope_note": rec.get("ds_scopenote", ""),
        "previous_indexing": list(rec.get("ds_previousindexing") or []),
        "year_introduced": rec.get("ds_yearintroduced", ""),
    }


def _text(el: ET.Element | None) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _year(article: ET.Element) -> str:
    for path in (".//Article/Journal/JournalIssue/PubDate/Year", ".//Article/ArticleDate/Year",
                 ".//Book/PubDate/Year", ".//PubmedData/History/PubMedPubDate[@PubStatus='pubmed']/Year"):
        el = article.find(path)
        if el is not None and (el.text or "").strip():
            return el.text.strip()
    md = article.find(".//PubDate/MedlineDate")
    m = re.search(r"\d{4}", md.text or "") if md is not None else None
    return m.group(0) if m else ""


def parse_pubmed_xml(raw: bytes) -> list[dict]:
    """PubmedArticle and PubmedBookArticle records as flat dicts."""
    root = ET.fromstring(raw)
    records = []
    for art in list(root.findall("PubmedArticle")) + list(root.findall("PubmedBookArticle")):
        pmid_el = art.find(".//MedlineCitation/PMID")
        if pmid_el is None:
            pmid_el = art.find(".//BookDocument/PMID")
        if pmid_el is None:
            continue
        title = _text(art.find(".//Article/ArticleTitle")) or _text(art.find(".//BookDocument/ArticleTitle")) \
            or _text(art.find(".//BookDocument/Book/BookTitle"))
        parts = []
        for ab in art.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            body = _text(ab)
            if body:
                parts.append(f"{label}: {body}" if label else body)
        pub_types = [_text(p) for p in art.findall(".//PublicationTypeList/PublicationType")]
        ids = {a.get("IdType"): (a.text or "").strip() for a in art.findall(".//PubmedData/ArticleIdList/ArticleId")}
        records.append({
            "pmid": pmid_el.text.strip(),
            "title": title,
            "abstract": " ".join(parts),
            "year": _year(art),
            "journal": _text(art.find(".//Article/Journal/ISOAbbreviation")) or _text(art.find(".//Article/Journal/Title")),
            "pub_types": pub_types,
            "retracted": "Retracted Publication" in pub_types,
            "language": ";".join(_text(x) for x in art.findall(".//Article/Language")),
            "mesh": [_text(d) for d in art.findall(".//MeshHeadingList/MeshHeading/DescriptorName")],
            "pmcid": ids.get("pmc", ""),
            "doi": ids.get("doi", ""),
        })
    return records
