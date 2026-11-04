import pytest

from pubmed_corpus import mentions as m

ROWS = [
    {"pmid": "1", "mentions": "POTS|Postural orthostatic tachycardia syndrome"},
    {"pmid": "2", "mentions": "postural instability"},
    {"pmid": "3", "mentions": "postural instability|PoTS"},
    {"pmid": "4", "mentions": "dystonic posturing"},
    {"pmid": "5", "mentions": "posture"},
]


def rules(**kw):
    return m.Rules.from_dict({"min_papers_to_decide": 2, **kw})


def test_normalize():
    assert m.normalize("  -Neuropathic  POTS, ") == "neuropathic pots"
    assert m.normalize("POTS (postural)") == "pots (postural)"


def test_explicit_lists_beat_patterns_and_conflicts_are_undecided():
    r = rules(approve=["postural instability"], approve_patterns=[r".*\bpots\b.*"], reject_patterns=[r".*postur.*"])
    assert r.decide("Postural instability") == m.APPROVE          # explicit beats pattern
    assert r.decide("neuropathic POTS") == m.APPROVE
    assert r.decide("dystonic posturing") == m.REJECT
    assert r.decide("postural pots") == m.CONFLICT
    assert r.decide("syncope") == m.UNDECIDED


def test_a_wording_cannot_be_both_approved_and_rejected():
    with pytest.raises(ValueError):
        rules(approve=["pots"], reject=["POTS"])


def test_audit_counts_papers_and_blocks_frequent_undecided_wordings():
    r = rules(approve=["pots", "postural orthostatic tachycardia syndrome"], reject=["posture"])
    audit = m.audit(ROWS, r, core={"1"})
    by = {a["wording"]: a for a in audit}
    assert by["pots"]["papers"] == 2 and by["pots"]["papers_outside_core"] == 1   # "PoTS" normalises to "pots"
    assert by["postural instability"]["decision"] == m.UNDECIDED
    assert [b["wording"] for b in m.blocking(audit, r)] == ["postural instability"]   # 2 papers >= threshold
    assert "dystonic posturing" not in {b["wording"] for b in m.blocking(audit, r)}   # 1 paper < threshold


def test_approved_papers_needs_one_approved_wording():
    r = rules(approve=["pots"], reject=["postural instability"])
    assert m.approved_papers(ROWS, r) == {"1": ["pots"], "3": ["pots"]}
