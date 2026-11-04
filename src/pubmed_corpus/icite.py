"""Citation links from NIH iCite (NIH Open Citation Collection).

PubMed's own ``pubmed_pubmed_citedin`` link misses many citing papers: for
Low 2009 (PMID 19207771) it listed 149 citing papers on 2026-11-04 against 260
in iCite and 248 in Europe PMC. iCite also returns each paper's reference list.
Recent papers can be missing from iCite, or present with empty lists, for some
months after publication; ``links`` reports those as such rather than as zero.
"""

from __future__ import annotations

from typing import Iterable

from .http import Http

API = "https://icite.od.nih.gov/api/pubs"
BATCH = 200


def links(http: Http, pmids: Iterable[str]) -> dict[str, dict]:
    """{pmid: {"found": bool, "references": [...], "cited_by": [...]}} for every requested PMID."""
    pmids = sorted({str(p) for p in pmids}, key=int)
    http.intervals.setdefault("icite.od.nih.gov", 0.2)
    out = {p: {"found": False, "references": [], "cited_by": []} for p in pmids}
    for i in range(0, len(pmids), BATCH):
        chunk = pmids[i : i + BATCH]
        payload = http.get_json(API, {"pmids": ",".join(chunk), "fl": "pmid,year,references,cited_by"}, cache=False)
        for rec in parse(payload):
            if rec["pmid"] in out:
                out[rec["pmid"]] = rec
    return out


def parse(payload: dict) -> list[dict]:
    recs = []
    for d in payload.get("data", []):
        recs.append({
            "pmid": str(d["pmid"]),
            "found": True,
            "references": sorted({str(x) for x in (d.get("references") or [])}, key=int),
            "cited_by": sorted({str(x) for x in (d.get("cited_by") or [])}, key=int),
        })
    return recs
