"""Draft the PubMed query from MeSH and the seeds' own wording, and run it clause by clause.

The draft is deterministic and uses only sources a reviewer can check:

* ``mesh_heading``: the MeSH descriptor itself.
* ``tiab_entry_terms``: the descriptor's entry terms (inverted forms such as
  "Syndrome, Postural Tachycardia" dropped) in title/abstract.
* ``tiab_seed_wordings``: the strings PubTator found the seeds using for the
  disease, where they reach papers the entry terms do not.
* ``tiab_acronym_guarded``: acronyms from those sources, admitted only next to
  a concept from the MeSH scope note, because bare acronyms are overloaded
  ("POTS" also means cooking pots and telephone service).
* ``mesh_previous_indexing``: how NLM indexed the topic before the descriptor
  existed (for POTS, Posture AND Tachycardia, 1982-2008). Proposed, not
  approved: it is broad and needs a human decision.

The agent may add clauses (historical names, spelling variants) with a
rationale and ``status: proposed``; only ``approved`` clauses define the core
corpus. Proposed clauses are still run and counted, so their effect is in the
report before anyone approves them.

PubMed phrase search matches a quoted token sequence anywhere in the field, so
a phrase also retrieves every longer phrase that contains it; ``reduce`` drops
the longer ones.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .eutils import EUtils
from .mentions import normalize

GENERIC_TOKENS = {"syndrome", "syndromes", "disease", "diseases", "disorder", "disorders", "and", "of", "the", "with"}


def tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def covers(short: str, long: str) -> bool:
    """True when a PubMed phrase search for ``short`` also retrieves ``long``."""
    a, b = tokens(short), tokens(long)
    return bool(a) and any(b[i : i + len(a)] == a for i in range(len(b) - len(a) + 1))


def reduce(phrases) -> list[str]:
    uniq = sorted({normalize(p) for p in phrases if normalize(p)})
    return [p for p in uniq if not any(q != p and covers(q, p) for q in uniq)]


def tiab(term: str) -> str:
    return f"{term}[tiab]" if re.fullmatch(r"[A-Za-z0-9]+", term) else f'"{term}"[tiab]'


def acronym_tokens(wording: str) -> list[str]:
    return [t for t in re.findall(r"[A-Za-z0-9]+", wording)
            if 2 <= len(t) <= 8 and sum(c.isupper() for c in t) >= 2 and not t.isdigit()]


def scope_concepts(scope_note: str) -> list[str]:
    """MeSH scope notes write cross-referenced descriptors in capitals."""
    found = re.findall(r"\b[A-Z][A-Z\-]+(?: [A-Z][A-Z\-]+)*\b", scope_note or "")
    return sorted({f.lower() for f in found if len(f) > 3})


def previous_indexing(entries: list[str]) -> dict[tuple[str, str], list[str]]:
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for e in entries:
        m = re.fullmatch(r"\s*(.+?)\s*\((\d{4})-(\d{4})\)\s*", e)
        if m:
            groups[(m.group(2), m.group(3))].append(m.group(1))
    return dict(groups)


def draft(mesh: dict, seed_wordings: dict[str, dict], *, min_seed_mentions: int = 1) -> tuple[list[dict], list[dict]]:
    """(clauses, term rows) from a MeSH record and ``pubtator.seed_mentions`` output."""
    terms: list[dict] = []
    entry = [t for t in mesh["entry_terms"] if ", " not in t]
    for t in mesh["entry_terms"]:
        terms.append({"term": t, "kind": "mesh_entry_term", "detail": "inverted form, not searched" if ", " in t else ""})

    acronyms: set[str] = set()
    seed_phrases: list[str] = []
    for text, rec in sorted(seed_wordings.items(), key=lambda kv: (-len(kv[1]["seeds"]), -kv[1]["mentions"], kv[0])):
        detail = f"{rec['mentions']} mentions in {len(rec['seeds'])} seeds"
        acr = acronym_tokens(text)
        if rec["mentions"] < min_seed_mentions:
            terms.append({"term": text, "kind": "seed_wording", "detail": detail + "; below threshold"})
            continue
        if acr:
            acronyms.update(a.upper() for a in acr)
            terms.append({"term": text, "kind": "seed_wording", "detail": detail + f"; acronym {','.join(a.upper() for a in acr)}"})
        else:
            seed_phrases.append(text)
            terms.append({"term": text, "kind": "seed_wording", "detail": detail})
    for t in entry:
        acronyms.update(a.upper() for a in acronym_tokens(t) if a.isupper())

    clauses: list[dict] = [{
        "id": "mesh_heading", "source": "mesh", "status": "approved",
        "rationale": f"NLM-indexed descriptor {mesh['ui']} (introduced {mesh.get('year_introduced') or 'n/a'}).",
        "query": f'"{mesh["heading"]}"[MeSH Terms]',
    }]
    entry_phrases = reduce(t for t in entry if not acronym_tokens(t) or len(tokens(t)) > 1)
    if entry_phrases:
        clauses.append({
            "id": "tiab_entry_terms", "source": "mesh", "status": "approved",
            "rationale": "MeSH entry terms in title/abstract; catches records not yet indexed with MeSH.",
            "query": " OR ".join(tiab(p) for p in entry_phrases),
        })
    new_seed = [p for p in reduce(seed_phrases) if not any(covers(e, p) for e in entry_phrases)]
    if new_seed:
        clauses.append({
            "id": "tiab_seed_wordings", "source": "seeds", "status": "approved",
            "rationale": "Wording the seed papers use for the disease (PubTator annotations) that the entry terms do not reach.",
            "query": " OR ".join(tiab(p) for p in new_seed),
        })
    guards = scope_concepts(mesh.get("scope_note", ""))
    if not guards:
        guards = sorted({t for t in tokens(mesh["heading"]) if len(t) >= 5 and t not in GENERIC_TOKENS})
    for t in guards:
        terms.append({"term": t, "kind": "scope_concept", "detail": "acronym guard"})
    if acronyms and guards:
        acr = " OR ".join(f"{a}[tiab]" for a in sorted(acronyms))
        guard = " OR ".join(tiab(g) for g in guards)
        clauses.append({
            "id": "tiab_acronym_guarded", "source": "seeds", "status": "approved",
            "rationale": "Acronyms are overloaded, so they count only next to a concept from the MeSH scope note.",
            "query": f"({acr}) AND ({guard})",
        })
    for (start, end), heads in sorted(previous_indexing(mesh.get("previous_indexing", [])).items()):
        for h in heads:
            terms.append({"term": h, "kind": "previous_indexing", "detail": f"{start}-{end}"})
        joined = " AND ".join(f'"{h}"[MeSH Terms:noexp]' for h in heads)
        clauses.append({
            "id": f"mesh_previous_indexing_{start}_{end}", "source": "mesh", "status": "proposed",
            "rationale": f"How NLM indexed this topic before the descriptor existed ({start}-{end}). Broad: approve only after "
                         "checking a sample.",
            "query": f'({joined}) AND ("{start}"[dp] : "{end}"[dp])',
        })
    return clauses, terms


def run(eutils: EUtils, clauses: list[dict]) -> dict[str, list[str]]:
    """PMIDs per clause for every clause that is not rejected."""
    return {c["id"]: eutils.search(c["query"]) for c in clauses if c["status"] != "rejected"}


def core(hits: dict[str, list[str]], clauses: list[dict]) -> dict[str, list[str]]:
    """{pmid: approved clause ids}."""
    approved = {c["id"] for c in clauses if c["status"] == "approved"}
    out: dict[str, list[str]] = defaultdict(list)
    for cid in sorted(hits):
        if cid in approved:
            for p in hits[cid]:
                out[p].append(cid)
    return dict(out)


def wording_regex(wordings: list[str]) -> re.Pattern | None:
    words = sorted({normalize(w) for w in wordings if normalize(w)}, key=len, reverse=True)
    if not words:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(r"\s+".join(map(re.escape, w.split())) for w in words) + r")(?![A-Za-z0-9])",
                      re.I)
