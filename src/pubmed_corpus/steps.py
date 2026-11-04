"""The collection steps. Each reads the project, calls one source, and writes ``out/``.

Network steps: ``terms``, ``query``, ``pubtator``, ``expand``, ``metadata``.
Offline steps: ``mentions``, ``screen-queue``, ``assemble``, ``report``.
"""

from __future__ import annotations

import sys
from collections import defaultdict

import yaml

from . import corpus as corpus_mod
from . import expand as expand_mod
from . import mentions as mentions_mod
from . import pubtator
from . import query as query_mod
from . import screen as screen_mod
from .eutils import EUtils
from .http import Http
from .project import Project, pmid_key, write_json, write_tsv

MENTIONS_TEMPLATE = """\
# Wording audit for the PubTator full-text tier. Run `pubmed-corpus mentions` to
# list every wording PubTator mapped to the project's concepts, with paper counts,
# in out/mention_audit.tsv. Decide every wording seen in at least
# min_papers_to_decide papers. Explicit lists beat patterns; patterns are
# case-insensitive regular expressions that must match the whole normalised wording.
min_papers_to_decide: 20
approve: []
approve_patterns: []
reject: []
reject_patterns: []
"""


def clients(project: Project, *, refresh: bool = False) -> tuple[Http, EUtils]:
    http = Http(project.cache, refresh=refresh)
    return http, EUtils(http)


# -- terms -----------------------------------------------------------------
def terms(project: Project, http: Http, eutils: EUtils, *, min_seed_mentions: int = 1) -> dict:
    mesh = eutils.mesh_descriptor(project.config["mesh"])
    docs = pubtator.export_documents(http, project.seed_pmids)
    by_pmid = {pubtator.doc_pmid(d): d for d in docs}
    wordings = pubtator.seed_mentions(docs, project.concepts)
    clauses, term_rows = query_mod.draft(mesh, wordings, min_seed_mentions=min_seed_mentions)

    write_json(project.out / "mesh.json", mesh)
    write_tsv(project.out / "terms.tsv", ["term", "kind", "detail"], term_rows)
    write_tsv(project.out / "seed_concepts.tsv", ["type", "concept", "name", "seeds", "mentions"],
              [{**c, "seeds": len(c["seeds"])} for c in pubtator.seed_concepts(docs)])
    write_tsv(project.out / "seeds.tsv", ["pmid", "key", "pubtator_text", "disease_mentions"], [
        {"pmid": s["pmid"], "key": s["key"],
         "pubtator_text": ("missing" if s["pmid"] not in by_pmid else
                           "full text" if pubtator.doc_is_full_text(by_pmid[s["pmid"]]) else "abstract"),
         "disease_mentions": sum(r["mentions"] for r in pubtator.seed_mentions([by_pmid[s["pmid"]]], project.concepts).values())
         if s["pmid"] in by_pmid else 0}
        for s in project.seeds])
    header = ("# Drafted by `pubmed-corpus terms` from MeSH and the seeds' PubTator annotations.\n"
              "# Review every clause, add proposed ones with a rationale, set status to approved,\n"
              "# proposed or rejected, then run `pubmed-corpus query`.\n")
    draft_text = header + yaml.safe_dump({"clauses": clauses}, sort_keys=False, allow_unicode=True, width=1000)
    (project.root / "query.draft.yaml").write_text(draft_text, encoding="utf-8")
    wrote_query = False
    if not project.query_file.exists():
        project.query_file.write_text(draft_text, encoding="utf-8")
        wrote_query = True
    if not project.mentions_file.exists():
        project.mentions_file.write_text(MENTIONS_TEMPLATE, encoding="utf-8")
    info = {"mesh": mesh["ui"], "seed_documents": len(docs), "seed_wordings": len(wordings), "drafted_clauses": len(clauses)}
    project.log_step("terms", info)
    return {**info, "wrote_query_yaml": wrote_query}


# -- query -----------------------------------------------------------------
def query(project: Project, eutils: EUtils, *, counts_only: bool = False) -> dict:
    clauses = project.clauses()
    active = [c for c in clauses if c["status"] != "rejected"]
    if counts_only:
        return {c["id"]: {"status": c["status"], "count": eutils.count(c["query"])} for c in active}
    hits = query_mod.run(eutils, clauses)
    write_tsv(project.out / "query_hits.tsv", ["clause", "pmid"],
              [{"clause": cid, "pmid": p} for cid in sorted(hits) for p in hits[cid]])
    write_tsv(project.out / "query_counts.tsv", ["clause", "status", "hits", "query"],
              [{"clause": c["id"], "status": c["status"], "hits": len(hits[c["id"]]), "query": c["query"]} for c in active])
    core = query_mod.core(hits, clauses)
    info = {"clauses_run": len(active), "core_papers": len(core)}
    project.log_step("query", info)
    return info


# -- pubtator --------------------------------------------------------------
def pubtator_scan(project: Project) -> dict:
    url = f"{pubtator.BULK}/{project.pubtator_file}"
    source = pubtator.head(url)
    last = [0]

    def progress(n: int) -> None:
        if n - last[0] >= 200 << 20:
            last[0] = n
            total = source["content_length"] or 0
            print(f"  scanned {n >> 20} MB" + (f" of {total >> 20} MB" if total else ""), file=sys.stderr, flush=True)

    rows, stats = pubtator.scan_bulk(url, project.concepts, expected_bytes=source["content_length"] or None, progress=progress)
    write_tsv(project.out / "pubtator_hits.tsv", ["pmid", "type", "concept", "mentions", "resource"], rows)
    info = {**source, **stats, "concepts": project.concepts, "papers": len({r["pmid"] for r in rows})}
    write_json(project.out / "pubtator_source.json", info)
    project.log_step("pubtator", {k: info[k] for k in ("url", "last_modified", "content_length", "rows", "papers")})
    return info


# -- mentions --------------------------------------------------------------
def mentions(project: Project) -> dict:
    if not project.mentions_file.exists():
        project.mentions_file.write_text(MENTIONS_TEMPLATE, encoding="utf-8")
    rows = corpus_mod.load_pubtator(project)
    rules = mentions_mod.Rules.load(project.mentions_file)
    core = set(query_mod.core(corpus_mod.load_query_hits(project), project.clauses())) \
        if (project.out / "query_hits.tsv").exists() else set()
    audit = mentions_mod.audit(rows, rules, core)
    write_tsv(project.out / "mention_audit.tsv", ["wording", "papers", "papers_outside_core", "decision"], audit)
    approved = mentions_mod.approved_papers(rows, rules)
    block = mentions_mod.blocking(audit, rules)
    return {"wordings": len(audit), "blocking": len(block), "papers_tagged": len({r["pmid"] for r in rows}),
            "papers_approved": len(approved), "papers_approved_outside_core": len(set(approved) - core),
            "first_blocking": [f"{b['wording']} ({b['papers']})" for b in block[:15]]}


# -- expand ----------------------------------------------------------------
def expand(project: Project, http: Http, eutils: EUtils) -> dict:
    edges, summary = expand_mod.expand(http, eutils, project.seeds, routes=project.expansion_routes,
                                       related_top_k=project.related_top_k)
    write_tsv(project.out / "expansion.tsv", ["seed", "route", "pmid", "score"], edges)
    write_tsv(project.out / "expansion_seeds.tsv", ["seed", "key", "icite_found", "references", "cited_by", "related"], summary)
    info = {"edges": len(edges), "papers": len({e["pmid"] for e in edges}),
            "seeds_missing_from_icite": [s["key"] for s in summary if not s["icite_found"]]}
    project.log_step("expand", {k: info[k] for k in ("edges", "papers")})
    return info


# -- metadata --------------------------------------------------------------
METADATA_COLUMNS = ["pmid", "missing", "year", "retracted", "has_abstract", "pmcid", "language", "pub_types", "journal", "title"]


def metadata(project: Project, eutils: EUtils) -> dict:
    cands = corpus_mod.candidates(project)
    want = set(cands)
    for ref in project.reference_sets().values():
        want |= ref
    records = screen_mod.load_records(project)
    todo = sorted(want - set(records), key=pmid_key)
    if todo:
        print(f"  fetching {len(todo)} PubMed records", file=sys.stderr, flush=True)
        for rec in eutils.fetch_records(todo):
            records[rec["pmid"]] = rec
        screen_mod.save_records(project, records)
    rows = []
    for pmid in sorted(want, key=pmid_key):
        r = records.get(pmid)
        if r is None:
            rows.append({"pmid": pmid, "missing": True})
            continue
        rows.append({"pmid": pmid, "missing": False, "year": r["year"], "retracted": r["retracted"],
                     "has_abstract": bool(r["abstract"]), "pmcid": r["pmcid"], "language": r["language"],
                     "pub_types": r["pub_types"], "journal": r["journal"], "title": r["title"]})
    write_tsv(project.out / "metadata.tsv", METADATA_COLUMNS, rows)
    info = {"records": len(rows), "missing": sum(1 for r in rows if r["missing"]),
            "retracted": sum(1 for r in rows if r.get("retracted"))}
    project.log_step("metadata", info)
    return info


# -- status ----------------------------------------------------------------
def status(project: Project) -> list[str]:
    lines = []
    log = project.run_log()
    for step in ("terms", "query", "pubtator", "expand", "metadata"):
        lines.append(f"{step:10s} {'done ' + log[step]['date'] if step in log else 'not run'}")
    try:
        clauses = project.clauses()
        n = defaultdict(int)
        for c in clauses:
            n[c["status"]] += 1
        lines.append(f"query.yaml {dict(n)}")
    except FileNotFoundError:
        lines.append("query.yaml missing")
    if (project.out / "pubtator_hits.tsv").exists():
        lines.append(f"wordings   {len(corpus_mod.blocking_wordings(project))} need a decision")
    try:
        cands = corpus_mod.candidates(project)
        todo = corpus_mod.needs_screen(cands)
        decided = corpus_mod.load_decisions(project)
        lines.append(f"screening  {sum(1 for p in todo if p in decided)} of {len(todo)} decided")
    except (FileNotFoundError, corpus_mod.AssemblyError) as exc:
        lines.append(f"screening  waiting: {exc}")
    lines.append(f"corpus     {'assembled' if (project.out / 'corpus.tsv').exists() else 'not assembled'}")
    return lines


def init(root, *, name: str, mesh: str, concepts: list[str], seeds: list[str]) -> None:
    from pathlib import Path

    root = Path(root)
    if (root / "project.yaml").exists():
        raise FileExistsError(f"{root / 'project.yaml'} already exists")
    seed_rows = []
    for s in seeds:
        pmid, _, key = s.partition(":")
        seed_rows.append({"pmid": pmid, "key": key or pmid})
    config = {
        "name": name,
        "mesh": mesh,
        "pubtator": {"file": "disease2pubtator3.gz", "concepts": concepts or [f"MESH:{mesh}"]},
        "seeds": seed_rows,
        "expansion": {"routes": ["references", "cited_by", "related"], "related_top_k": 100},
        "screening": {"criteria": "Include a paper if its own data or argument bears on the disease: its mechanisms, tests, "
                                  "subtypes, comorbidities, triggers, treatments or outcomes. Exclude papers about other "
                                  "conditions with no link to the disease, and generic methods papers (software, survey "
                                  "instruments, statistics) cited only as tools.",
                      "auto_include_wordings": []},
        "exclude_retracted": True,
        "references": [],
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.yaml").write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
    (root / "mentions.yaml").write_text(MENTIONS_TEMPLATE, encoding="utf-8")
