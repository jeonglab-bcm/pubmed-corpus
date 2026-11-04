"""Smoke tests against the real services. Run with PUBMED_CORPUS_NETWORK=1 pytest -m network."""

import os

import pytest

from pubmed_corpus import icite, pubtator
from pubmed_corpus.eutils import EUtils
from pubmed_corpus.http import Http

pytestmark = [pytest.mark.network,
              pytest.mark.skipif(os.environ.get("PUBMED_CORPUS_NETWORK") != "1", reason="set PUBMED_CORPUS_NETWORK=1")]
LOW_2009 = "19207771"   # Low et al., "Postural tachycardia syndrome (POTS)", J Cardiovasc Electrophysiol 2009


@pytest.fixture(scope="module")
def http():
    return Http()


def test_mesh_and_search(http):
    eu = EUtils(http)
    mesh = eu.mesh_descriptor("D054972")
    assert mesh["heading"] == "Postural Orthostatic Tachycardia Syndrome"
    assert eu.count('"Postural Orthostatic Tachycardia Syndrome"[MeSH Terms]') > 900   # 932 on 2026-10-01


def test_records_and_related(http):
    eu = EUtils(http)
    (rec,) = eu.fetch_records([LOW_2009])
    assert rec["title"].startswith("Postural tachycardia syndrome")
    assert len(eu.related(LOW_2009, 10)) == 10


def test_icite(http):
    got = icite.links(http, [LOW_2009])[LOW_2009]
    assert got["found"] and len(got["cited_by"]) > 100 and len(got["references"]) > 10


def test_pubtator_export(http):
    (doc,) = pubtator.export_documents(http, ["24386408"])   # Gibbons 2013, PMC open access
    assert pubtator.doc_is_full_text(doc)
    assert pubtator.seed_mentions([doc], ["MESH:D054972"])
