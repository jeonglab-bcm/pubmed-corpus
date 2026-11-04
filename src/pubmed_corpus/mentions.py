"""The wording audit: decide which PubTator surface strings really name the disease.

A bulk row lists, for one paper, every string PubTator mapped to the concept
("PoTS|Postural orthostatic tachycardia syndrome"). Wordings are normalised
(lower case, collapsed whitespace, end punctuation stripped) and decided by
``mentions.yaml``::

    min_papers_to_decide: 20      # every wording seen in this many papers needs a decision
    approve: [pots, postural tachycardia syndrome]
    approve_patterns: ['.*\\bpostural (orthostatic )?tachycardia\\b.*']
    reject: [postural instability, posture]
    reject_patterns: ['.*\\bdyston.*']

Explicit lists beat patterns. A wording that matches both an approve and a
reject pattern, or neither, is undecided. A paper enters the full-text tier
when at least one of its wordings is approved. Undecided wordings seen in at
least ``min_papers_to_decide`` papers block assembly, so no large block of
papers is kept or dropped without someone having looked at its wording.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

APPROVE, REJECT, UNDECIDED, CONFLICT = "approve", "reject", "undecided", "conflict"


def normalize(wording: str) -> str:
    w = re.sub(r"\s+", " ", wording.strip().lower())
    return re.sub(r"^[^\w]+|[^\w)]+$", "", w)


@dataclass
class Rules:
    approve: set[str] = field(default_factory=set)
    reject: set[str] = field(default_factory=set)
    approve_patterns: list[re.Pattern] = field(default_factory=list)
    reject_patterns: list[re.Pattern] = field(default_factory=list)
    min_papers_to_decide: int = 20

    @classmethod
    def load(cls, path: Path) -> "Rules":
        data = yaml.safe_load(Path(path).read_text()) if Path(path).exists() else {}
        return cls.from_dict(data or {})

    @classmethod
    def from_dict(cls, data: dict) -> "Rules":
        rules = cls(
            approve={normalize(w) for w in data.get("approve") or []},
            reject={normalize(w) for w in data.get("reject") or []},
            approve_patterns=[re.compile(p, re.I) for p in data.get("approve_patterns") or []],
            reject_patterns=[re.compile(p, re.I) for p in data.get("reject_patterns") or []],
            min_papers_to_decide=int(data.get("min_papers_to_decide", 20)),
        )
        both = rules.approve & rules.reject
        if both:
            raise ValueError(f"wordings both approved and rejected: {sorted(both)}")
        return rules

    def decide(self, wording: str) -> str:
        w = normalize(wording)
        if w in self.approve:
            return APPROVE
        if w in self.reject:
            return REJECT
        a = any(p.fullmatch(w) for p in self.approve_patterns)
        r = any(p.fullmatch(w) for p in self.reject_patterns)
        if a and r:
            return CONFLICT
        return APPROVE if a else REJECT if r else UNDECIDED


def wordings(row: dict) -> set[str]:
    return {normalize(m) for m in row["mentions"].split("|") if normalize(m)}


def audit(rows: list[dict], rules: Rules, core: set[str] = frozenset()) -> list[dict]:
    """One line per wording: papers carrying it, how many are outside the core, and the decision."""
    papers: Counter = Counter()
    outside: Counter = Counter()
    for row in rows:
        for w in wordings(row):
            papers[w] += 1
            if row["pmid"] not in core:
                outside[w] += 1
    out = [{"wording": w, "papers": n, "papers_outside_core": outside[w], "decision": rules.decide(w)}
           for w, n in papers.items()]
    out.sort(key=lambda r: (-r["papers"], r["wording"]))
    return out


def blocking(audit_rows: list[dict], rules: Rules) -> list[dict]:
    return [r for r in audit_rows if r["decision"] in (UNDECIDED, CONFLICT) and r["papers"] >= rules.min_papers_to_decide]


def approved_papers(rows: list[dict], rules: Rules) -> dict[str, list[str]]:
    """{pmid: approved wordings} for papers with at least one approved wording."""
    out: dict[str, set[str]] = {}
    for row in rows:
        ok = {w for w in wordings(row) if rules.decide(w) == APPROVE}
        if ok:
            out.setdefault(row["pmid"], set()).update(ok)
    return {p: sorted(ws) for p, ws in out.items()}
