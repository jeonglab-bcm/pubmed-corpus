"""Assemble and freeze the corpus from the steps' outputs and the human decisions.

Each paper gets one tier, the strongest route that reached it:

    seed       a seed paper
    core       retrieved by an approved PubMed clause (title/abstract/MeSH)
    fulltext   tagged by PubTator with an approved wording, outside the core
               (for most, the disease is named only in the body of the full text)
    expansion  reached only from the seeds (reference, citing paper, similar
               article) and kept by the relevance screen

and keeps every route that reached it, so any narrower corpus is a filter on
``corpus.tsv`` rather than a re-run. Assembly is offline and deterministic:
the same inputs give byte-identical outputs, which ``--check`` verifies.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from . import __version__
from . import mentions as mentions_mod
from . import query as query_mod
from .project import Project, pmid_key, read_jsonl, read_tsv, sha256

TIERS = ("seed", "core", "fulltext", "expansion")
CORPUS_COLUMNS = ["pmid", "tier", "year", "clauses", "pubtator_wordings", "expansion", "screen", "pmcid", "has_abstract",
                  "title"]
EXCLUDED_COLUMNS = ["pmid", "tier", "reason", "title"]
DECISIONS = ("include", "exclude")


class AssemblyError(RuntimeError):
    pass


def _need(path: Path, step: str) -> Path:
    if not path.exists():
        raise AssemblyError(f"{path} is missing; run `pubmed-corpus {step}` first")
    return path


def load_query_hits(project: Project) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = defaultdict(list)
    for r in read_tsv(_need(project.out / "query_hits.tsv", "query")):
        hits[r["clause"]].append(r["pmid"])
    return dict(hits)


def load_pubtator(project: Project) -> list[dict]:
    path = project.out / "pubtator_hits.tsv"
    return read_tsv(path) if path.exists() else []


def load_edges(project: Project) -> list[dict]:
    return read_tsv(_need(project.out / "expansion.tsv", "expand"))


def load_decisions(project: Project) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for n, d in enumerate(read_jsonl(project.decisions_file), 1):
        where = f"{project.decisions_file}:{n}"
        for key in ("pmid", "decision", "reason", "screener"):
            if not str(d.get(key, "")).strip():
                raise AssemblyError(f"{where}: `{key}` is required")
        if d["decision"] not in DECISIONS:
            raise AssemblyError(f"{where}: decision must be one of {DECISIONS}, not {d['decision']!r}")
        pmid = str(d["pmid"])
        if pmid in out and out[pmid]["decision"] != d["decision"]:
            raise AssemblyError(f"{where}: PMID {pmid} is both included and excluded")
        out[pmid] = {**d, "pmid": pmid}
    return out


def candidates(project: Project) -> dict[str, dict]:
    """Every paper any route reached, with its tier and routes (before metadata and screening)."""
    clauses = project.clauses()
    core = query_mod.core(load_query_hits(project), clauses)
    rules = mentions_mod.Rules.load(project.mentions_file)
    fulltext = mentions_mod.approved_papers(load_pubtator(project), rules)
    keys = {s["pmid"]: s["key"] for s in project.seeds}
    expansion: dict[str, set[str]] = defaultdict(set)
    for e in load_edges(project):
        expansion[e["pmid"]].add(f"{keys.get(e['seed'], e['seed'])}:{e['route']}")
    out: dict[str, dict] = {}
    for pmid in set(keys) | set(core) | set(fulltext) | set(expansion):
        tier = ("seed" if pmid in keys else "core" if pmid in core else "fulltext" if pmid in fulltext else "expansion")
        out[pmid] = {"pmid": pmid, "tier": tier, "clauses": core.get(pmid, []), "pubtator_wordings": fulltext.get(pmid, []),
                     "expansion": sorted(expansion.get(pmid, ()))}
    return out


def needs_screen(cands: dict[str, dict]) -> list[str]:
    return sorted((p for p, c in cands.items() if c["tier"] == "expansion"), key=pmid_key)


def blocking_wordings(project: Project) -> list[dict]:
    rows = load_pubtator(project)
    if not rows:
        return []
    rules = mentions_mod.Rules.load(project.mentions_file)
    return mentions_mod.blocking(mentions_mod.audit(rows, rules), rules)


def assemble(project: Project, *, allow_unscreened: bool = False) -> tuple[list[dict], list[dict], dict]:
    block = blocking_wordings(project)
    if block:
        listing = ", ".join(f"{b['wording']!r} ({b['papers']})" for b in block[:10])
        raise AssemblyError(f"{len(block)} PubTator wordings need a decision in {project.mentions_file.name}: {listing}")
    cands = candidates(project)
    meta = {r["pmid"]: r for r in read_tsv(_need(project.out / "metadata.tsv", "metadata"))}
    decisions = load_decisions(project)
    corpus, excluded = [], []
    unscreened = []
    for pmid in sorted(cands, key=pmid_key):
        c = cands[pmid]
        m = meta.get(pmid)
        title = m["title"] if m else ""
        reason = None
        screen = ""
        if m is None or m.get("missing") == "1":
            reason = "no PubMed record"
        elif project.exclude_retracted and m.get("retracted") == "1":
            reason = "retracted"
        elif c["tier"] == "expansion":
            d = decisions.get(pmid)
            if d is None:
                unscreened.append(pmid)
                reason = "not screened"
            elif d["decision"] == "exclude":
                reason = f"screened out: {d['reason']}"
            else:
                screen = "rule" if d["screener"] == "rule" else "include"
        if reason:
            excluded.append({"pmid": pmid, "tier": c["tier"], "reason": reason, "title": title})
            continue
        corpus.append({**c, "year": m["year"], "screen": screen, "pmcid": m["pmcid"], "has_abstract": m["has_abstract"],
                       "title": title})
    if unscreened and not allow_unscreened:
        raise AssemblyError(f"{len(unscreened)} expansion papers have no screening decision; run `pubmed-corpus screen-queue` "
                            f"and record decisions in {project.decisions_file} (first: {', '.join(unscreened[:5])})")
    stale = sorted(set(decisions) - set(needs_screen(cands)), key=pmid_key)
    manifest = {
        "tool": f"pubmed-corpus {__version__}",
        "project": project.config["name"],
        "mesh": project.config["mesh"],
        "seeds": project.seed_pmids,
        "counts": {t: sum(1 for r in corpus if r["tier"] == t) for t in TIERS} | {"total": len(corpus)},
        "excluded": dict(sorted(Counter(_reason_class(e["reason"]) for e in excluded).items())),
        "stale_screening_decisions": len(stale),
        "inputs": {str(p.relative_to(project.root)): sha256(p) for p in _inputs(project)},
        "run_log": project.run_log(),
    }
    return corpus, excluded, manifest


def _reason_class(reason: str) -> str:
    return reason.split(":")[0]


def _inputs(project: Project) -> list[Path]:
    paths = [project.root / "project.yaml", project.query_file, project.mentions_file, project.decisions_file]
    paths += [project.out / n for n in ("query_hits.tsv", "pubtator_hits.tsv", "expansion.tsv", "metadata.tsv")]
    return [p for p in paths if p.exists()]


def render(corpus: list[dict], excluded: list[dict], manifest: dict) -> dict[str, str]:
    """File name -> exact content, so writing and checking share one code path."""
    import csv
    import io

    def tsv(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf, delimiter="\t", lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow([";".join(v) if isinstance(v, list) else str(v).replace("\t", " ").replace("\n", " ")
                        for v in (r.get(h, "") for h in header)])
        return buf.getvalue()

    return {
        "corpus.tsv": tsv(CORPUS_COLUMNS, corpus),
        "excluded.tsv": tsv(EXCLUDED_COLUMNS, excluded),
        "manifest.json": json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    }


def write_or_check(project: Project, files: dict[str, str], *, check: bool) -> list[str]:
    """Write ``files`` under out/, or with ``check`` return the names that differ from disk."""
    drift = []
    for name, content in files.items():
        path = project.out / name
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                drift.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    return drift
