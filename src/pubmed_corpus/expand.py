"""One round of expansion from the seeds: references, citing papers, similar articles.

References and citing papers come from iCite; similar articles from PubMed's
``pubmed_pubmed`` neighbours (top ``related_top_k`` by PubMed's score). One
round only: on the POTS seeds a single round already reached 767 papers, and a
second round would be dominated by papers that merely cite a cited paper.

Papers reached only through this step are screened for relevance before they
enter the corpus (``screen.py``): reference lists pull in methods papers
(REDCap, symptom scales) that are not about the disease.
"""

from __future__ import annotations

from . import icite
from .eutils import EUtils
from .http import Http


def expand(http: Http, eutils: EUtils, seeds: list[dict], *, routes: list[str], related_top_k: int) -> tuple[list[dict], list[dict]]:
    """(edges, per-seed summary). An edge is {seed, route, pmid, score}."""
    seed_ids = {s["pmid"] for s in seeds}
    edges: list[dict] = []
    summary: list[dict] = []
    cites = icite.links(http, seed_ids) if {"references", "cited_by"} & set(routes) else {}
    for s in seeds:
        pmid = s["pmid"]
        row = {"seed": pmid, "key": s["key"], "icite_found": False, "references": 0, "cited_by": 0, "related": 0}
        rec = cites.get(pmid)
        if rec:
            row["icite_found"] = rec["found"]
            for route in ("references", "cited_by"):
                if route in routes:
                    linked = [p for p in rec[route] if p != pmid]
                    row[route] = len(linked)
                    edges.extend({"seed": pmid, "route": route, "pmid": p, "score": ""} for p in linked)
        if "related" in routes:
            rel = eutils.related(pmid, related_top_k)
            row["related"] = len(rel)
            edges.extend({"seed": pmid, "route": "related", "pmid": p, "score": score} for p, score in rel)
        summary.append(row)
    edges = [e for e in edges if e["pmid"] not in seed_ids]   # seeds are in the corpus already
    edges.sort(key=lambda e: (int(e["seed"]), e["route"], int(e["pmid"])))
    return edges, summary
