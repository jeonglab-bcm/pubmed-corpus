import gzip
import io

import pytest

from pubmed_corpus import icite, pubtator
from pubmed_corpus.http import Http

from conftest import biocjson

LINES = [
    "1\tDisease\tMESH:D054972\tPOTS|postural tachycardia syndrome\tPubTator3",
    "1\tDisease\tMESH:D000001\tsomething else\tPubTator3",
    "2\tDisease\tMESH:D054972\tpostural instability\tPubTator3",
    "3\tDisease\tMESH:D0549721\tnot the same concept\tPubTator3",
    "4\tDisease\tMESH:D000002\tmention naming MESH:D054972 in text\tPubTator3",
    "5\tDisease\tMESH:D054972\tPoTS\tMeSH",
    "5\tDisease\tMESH:D054972\tpostural tachycardia\tPubTator3",
]


def gz_members(lines, members=3) -> bytes:
    """A multi-member gzip file, as produced by concatenating gzip streams."""
    text = ("\n".join(lines) + "\n").encode()
    step = max(1, len(text) // members)
    return b"".join(gzip.compress(text[i : i + step]) for i in range(0, len(text), step))


class FlakyServer:
    """Serves ``data``; the first ``drops`` connections die after ``after`` bytes. Honours Range unless told not to."""

    def __init__(self, data, drops=0, after=37, honour_range=True):
        self.data, self.drops, self.after, self.honour_range = data, drops, after, honour_range
        self.offsets = []

    def open(self, url, offset):
        self.offsets.append(offset)
        start = offset if self.honour_range else 0
        body = self.data[start:]
        dying = self.drops > 0
        self.drops -= 1 if dying else 0
        server = self

        class Resp:
            status = 206 if (offset and server.honour_range) else 200

            def __init__(self):
                self.buf = io.BytesIO(body)
                self.sent = 0

            def read(self, n):
                if dying and self.sent >= server.after:
                    raise ConnectionResetError("proxy reset")
                limit = n if not dying else min(n, server.after - self.sent)
                got = self.buf.read(limit)
                self.sent += len(got)
                return got

            def close(self):
                pass

        return Resp()


@pytest.mark.parametrize("chunk", [5, 16, 64, 1 << 20])
def test_scan_keeps_exact_concept_rows_across_chunk_boundaries(chunk):
    srv = FlakyServer(gz_members(LINES))
    rows, stats = pubtator.scan_bulk("u", ["MESH:D054972"], open_range=srv.open, chunk_size=chunk)
    assert [(r["pmid"], r["mentions"]) for r in rows] == [
        ("1", "POTS|postural tachycardia syndrome"), ("2", "postural instability"), ("5", "PoTS|postural tachycardia")]
    assert rows[-1]["resource"] == "MeSH|PubTator3"
    assert stats["lines"] == len(LINES) and stats["resumes"] == 0


@pytest.mark.parametrize("honour_range", [True, False])
def test_scan_resumes_after_dropped_connections(honour_range):
    data = gz_members(LINES)
    srv = FlakyServer(data, drops=3, after=29, honour_range=honour_range)
    rows, stats = pubtator.scan_bulk("u", ["MESH:D054972"], open_range=srv.open, chunk_size=8, sleep=lambda s: None,
                                     expected_bytes=len(data))
    assert {r["pmid"] for r in rows} == {"1", "2", "5"}
    assert stats["resumes"] == 3 and stats["compressed_bytes"] == len(data)
    assert srv.offsets[0] == 0 and srv.offsets[1] > 0


def test_scan_rejects_a_truncated_file():
    data = gz_members(LINES, members=1)[:-12]
    with pytest.raises(RuntimeError, match="gzip stream was complete"):
        pubtator.scan_bulk("u", ["MESH:D054972"], open_range=FlakyServer(data).open)


def test_scan_gives_up_after_retries():
    srv = FlakyServer(gz_members(LINES), drops=100, after=3)
    with pytest.raises(RuntimeError, match="failed at byte"):
        pubtator.scan_bulk("u", ["MESH:D054972"], open_range=srv.open, retries=2, sleep=lambda s: None)


def test_seed_mentions_and_concepts():
    docs = [biocjson("10", [("POTS", "MESH:D054972", "Disease"), ("POTS", "MESH:D054972", "Disease"),
                            ("syncope", "MESH:D013575", "Disease"), ("humans", "9606", "Species")], full_text=True),
            biocjson("11", [("postural tachycardia", "MESH:D054972", "Disease"), ("syncope", "MESH:D013575", "Disease")])]
    m = pubtator.seed_mentions(docs, ["MESH:D054972"])
    assert m["POTS"]["mentions"] == 2 and m["POTS"]["seeds"] == {"10"}
    assert m["postural tachycardia"]["seeds"] == {"11"}
    concepts = pubtator.seed_concepts(docs)
    assert [(c["concept"], len(c["seeds"])) for c in concepts] == [("MESH:D054972", 2), ("MESH:D013575", 2)]   # ties on seeds broken by mentions
    assert pubtator.doc_is_full_text(docs[0]) and not pubtator.doc_is_full_text(docs[1])


def test_icite_handles_missing_papers_and_null_lists(world):
    world.icite = {"1": {"references": [3, 2], "cited_by": None}}
    http, _ = world.clients()
    got = icite.links(http, ["1", "9"])
    assert got["1"] == {"pmid": "1", "found": True, "references": ["2", "3"], "cited_by": []}
    assert got["9"] == {"found": False, "references": [], "cited_by": []}


def test_http_cache_replays_without_network(tmp_path):
    calls = []

    def fetch(method, url, params):
        calls.append(url)
        return b'{"ok": 1}'

    http = Http(tmp_path, fetch=fetch)
    assert http.get_json("https://x/y", {"a": 1}) == {"ok": 1}
    assert Http(tmp_path, fetch=fetch).get_json("https://x/y", {"a": 1}) == {"ok": 1}
    assert len(calls) == 1
    Http(tmp_path, fetch=fetch, refresh=True).get_json("https://x/y", {"a": 1})
    assert len(calls) == 2
