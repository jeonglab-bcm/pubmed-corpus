"""Relevance screen for papers reached only from the seeds.

``screen-queue`` does two things for every expansion-tier paper without a
decision:

1. If its title or abstract uses an approved disease wording (the ``approve``
   list in ``mentions.yaml`` plus ``screening.auto_include_wordings`` in
   ``project.yaml``), it records an include decision with screener ``rule``.
2. Otherwise it writes the paper to ``screen/queue.jsonl`` (title, abstract,
   how it was found) for the agent to judge against ``screening.criteria``.

The agent appends one JSON line per judged paper to ``screen/decisions.jsonl``::

    {"pmid": "123", "decision": "include", "reason": "orthostatic intolerance in long COVID", "screener": "agent"}

Decisions are never edited in place; a later line for the same PMID must
agree with the earlier one or assembly fails.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import yaml

from . import corpus as corpus_mod
from .project import Project, pmid_key, read_jsonl, read_tsv, write_jsonl
from .query import wording_regex

ABSTRACT_CHARS = 3000


def records_path(project: Project) -> Path:
    return project.cache / "records.json.gz"


def load_records(project: Project) -> dict[str, dict]:
    path = records_path(project)
    if not path.exists():
        return {}
    return json.loads(gzip.decompress(path.read_bytes()))


def save_records(project: Project, records: dict[str, dict]) -> None:
    path = records_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(records, sort_keys=True).encode(), mtime=0))


def auto_wordings(project: Project) -> list[str]:
    data = yaml.safe_load(project.mentions_file.read_text()) if project.mentions_file.exists() else {}
    words = list((data or {}).get("approve") or [])
    words += list((project.config.get("screening") or {}).get("auto_include_wordings") or [])
    return words


def build_queue(project: Project) -> dict:
    cands = corpus_mod.candidates(project)
    todo = corpus_mod.needs_screen(cands)
    decided = corpus_mod.load_decisions(project)
    records = load_records(project)
    pattern = wording_regex(auto_wordings(project))
    meta_path = project.out / "metadata.tsv"
    absent = {r["pmid"] for r in read_tsv(meta_path) if r.get("missing") == "1"} if meta_path.exists() else set()
    todo = [p for p in todo if p not in absent]   # no PubMed record: excluded at assembly
    missing = [p for p in todo if p not in decided and p not in records]
    if missing:
        raise corpus_mod.AssemblyError(f"{len(missing)} papers to screen have no cached record; run `pubmed-corpus metadata` first")
    rule_rows, queue = [], []
    for pmid in todo:
        if pmid in decided:
            continue
        rec = records[pmid]
        if rec.get("retracted") and project.exclude_retracted:
            continue   # excluded at assembly anyway
        text = f"{rec.get('title', '')} {rec.get('abstract', '')}"
        hit = pattern.search(text) if pattern else None
        if hit:
            rule_rows.append({"pmid": pmid, "decision": "include", "screener": "rule",
                              "reason": f"disease wording in title/abstract: {hit.group(0).lower()!r}"})
            continue
        queue.append({"pmid": pmid, "title": rec.get("title", ""), "abstract": rec.get("abstract", "")[:ABSTRACT_CHARS],
                      "year": rec.get("year", ""), "pub_types": rec.get("pub_types", []),
                      "found_via": cands[pmid]["expansion"]})
    if rule_rows:
        existing = read_jsonl(project.decisions_file)
        write_jsonl(project.decisions_file, existing + rule_rows)
    write_jsonl(project.queue_file, sorted(queue, key=lambda r: pmid_key(r["pmid"])))
    return {"to_screen": len(todo), "already_decided": len([p for p in todo if p in decided]), "rule_included": len(rule_rows),
            "queued": len(queue)}
