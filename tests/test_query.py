from pubmed_corpus import query
from pubmed_corpus.eutils import parse_mesh_summary

from conftest import MESH


def mesh():
    return parse_mesh_summary({"result": {"uids": [MESH["uid"]], MESH["uid"]: MESH}}, "D054972")


def test_covers_is_contiguous_token_match():
    assert query.covers("orthostatic tachycardia", "postural orthostatic tachycardia syndrome")
    assert query.covers("postural tachycardia", "neuropathic postural tachycardia syndrome")
    assert not query.covers("postural tachycardia", "postural orthostatic tachycardia syndrome")
    assert not query.covers("tachycardia syndrome", "tachycardia syndromes")   # PubMed does not stem phrases


def test_reduce_keeps_only_the_shortest_covering_phrases():
    got = query.reduce(["Postural tachycardia syndrome", "postural tachycardia", "-Neuropathic postural tachycardia syndrome",
                        "orthostatic tachycardia", "postural orthostatic tachycardia syndrome"])
    assert got == ["orthostatic tachycardia", "postural tachycardia"]


def test_tiab_quotes_only_multiword_terms():
    assert query.tiab("POTS") == "POTS[tiab]"
    assert query.tiab("orthostatic intolerance") == '"orthostatic intolerance"[tiab]'
    assert query.tiab("soldier's heart") == '"soldier\'s heart"[tiab]'


def test_acronyms_and_scope_concepts():
    assert query.acronym_tokens("neuropathic POTS") == ["POTS"]
    assert query.acronym_tokens("PoTS") == ["PoTS"]
    assert query.acronym_tokens("Postural Tachycardia Syndrome") == []
    assert query.scope_concepts(MESH["ds_scopenote"]) == ["orthostatic intolerance", "tachycardia"]


def test_draft_from_mesh_and_seed_wordings():
    wordings = {
        "POTS": {"mentions": 40, "seeds": {"1", "2"}},
        "neuropathic POTS": {"mentions": 5, "seeds": {"1"}},
        "postural tachycardia": {"mentions": 2, "seeds": {"2"}},
        "Postural tachycardia syndrome": {"mentions": 1, "seeds": {"1"}},
    }
    clauses, terms = query.draft(mesh(), wordings)
    by_id = {c["id"]: c for c in clauses}
    assert by_id["mesh_heading"]["query"] == '"Postural Orthostatic Tachycardia Syndrome"[MeSH Terms]'
    assert by_id["tiab_entry_terms"]["query"] == ('"postural orthostatic tachycardia syndrome"[tiab] OR '
                                                 '"postural tachycardia syndrome"[tiab]')
    # "postural tachycardia" reaches papers the entry terms miss; the syndrome wording is already covered
    assert by_id["tiab_seed_wordings"]["query"] == '"postural tachycardia"[tiab]'
    assert by_id["tiab_acronym_guarded"]["query"] == '(POTS[tiab]) AND ("orthostatic intolerance"[tiab] OR tachycardia[tiab])'
    prev = by_id["mesh_previous_indexing_1982_2008"]
    assert prev["status"] == "proposed"
    assert prev["query"] == '("Posture"[MeSH Terms:noexp] AND "Tachycardia"[MeSH Terms:noexp]) AND ("1982"[dp] : "2008"[dp])'
    assert all(c["status"] == "approved" for c in clauses if c is not prev)
    kinds = {(t["term"], t["kind"]) for t in terms}
    assert ("Syndrome, Postural Tachycardia", "mesh_entry_term") in kinds
    assert ("POTS", "seed_wording") in kinds


def test_draft_threshold_drops_rare_seed_wordings():
    wordings = {"postural tachycardia": {"mentions": 1, "seeds": {"1"}}}
    clauses, terms = query.draft(mesh(), wordings, min_seed_mentions=2)
    assert "tiab_seed_wordings" not in {c["id"] for c in clauses}
    assert any("below threshold" in t["detail"] for t in terms)


def test_core_uses_approved_clauses_only():
    clauses = [{"id": "a", "status": "approved", "query": "x"}, {"id": "b", "status": "proposed", "query": "y"}]
    assert query.core({"a": ["1", "2"], "b": ["2", "3"]}, clauses) == {"1": ["a"], "2": ["a"]}


def test_wording_regex_matches_whole_words_across_whitespace():
    rx = query.wording_regex(["pots", "postural tachycardia syndrome"])
    assert rx.search("Patients with POTS were")
    assert rx.search("postural\ntachycardia  syndrome")
    assert not rx.search("teapots and potsherds")
    assert query.wording_regex([]) is None
