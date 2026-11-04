"""PubTator3: concept annotations for the seed papers, and a scan of the bulk files.

Two uses, both deliberately narrow:

* ``export_documents`` fetches the seeds' annotated text (full text for PMC
  open-access seeds, abstracts otherwise) so their wording for the disease can
  seed the PubMed query.
* ``scan_bulk`` streams one of the monthly bulk files (for diseases,
  ``disease2pubtator3.gz``, about 2 GB) and keeps the rows tagged with the
  project's concept IDs. Those rows include mentions found only in full text,
  which a PubMed title/abstract query cannot reach.

PubTator's disease normaliser can be badly wrong for a given disease: on the
2026-10-06 release, 12,649 of the 30,932 papers tagged with the POTS concept
(MESH:D054972) carried only the wording "postural instability", mostly from
Parkinson's disease papers. So bulk rows never enter a corpus directly; they
pass through the wording audit in ``mentions.py`` first.

The API search endpoint is not used to define a corpus: it returns a ranked
list (31,243 "hits" for POTS, off-topic by rank 3,000), not a set.
"""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
import zlib
from collections import defaultdict
from typing import Callable, Iterable

from .http import USER_AGENT, Http

API = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api"
BULK = "https://ftp.ncbi.nlm.nih.gov/pub/lu/PubTator3"
EXPORT_BATCH = 50

OpenRange = Callable[[str, int], object]


# -- seed annotations ------------------------------------------------------
def export_documents(http: Http, pmids: Iterable[str]) -> list[dict]:
    pmids = sorted({str(p) for p in pmids}, key=int)
    http.intervals.setdefault("www.ncbi.nlm.nih.gov", 0.35)
    docs: list[dict] = []
    for i in range(0, len(pmids), EXPORT_BATCH):
        chunk = pmids[i : i + EXPORT_BATCH]
        payload = http.get_json(f"{API}/publications/export/biocjson", {"pmids": ",".join(chunk), "full": "true"})
        docs.extend(payload.get("PubTator3", []) if isinstance(payload, dict) else payload)
    return docs


def doc_pmid(doc: dict) -> str:
    return str(doc.get("_id", doc.get("id", ""))).split("|")[0]


def doc_is_full_text(doc: dict) -> bool:
    return any((p.get("infons") or {}).get("section_type") not in (None, "TITLE", "ABSTRACT")
               for p in doc.get("passages", []))


def _annotations(doc: dict):
    for passage in doc.get("passages", []):
        for ann in passage.get("annotations", []) or []:
            yield ann


def seed_mentions(docs: list[dict], concepts: Iterable[str]) -> dict[str, dict]:
    """Surface strings the seeds use for the project's concepts: {text: {"mentions", "seeds"}}."""
    wanted = set(concepts)
    out: dict[str, dict] = defaultdict(lambda: {"mentions": 0, "seeds": set()})
    for doc in docs:
        pmid = doc_pmid(doc)
        for ann in _annotations(doc):
            if (ann.get("infons") or {}).get("identifier") in wanted:
                rec = out[ann.get("text", "")]
                rec["mentions"] += 1
                rec["seeds"].add(pmid)
    return dict(out)


def seed_concepts(docs: list[dict]) -> list[dict]:
    """Every normalised concept in the seeds with the number of seeds that mention it."""
    acc: dict[tuple[str, str], dict] = {}
    for doc in docs:
        pmid = doc_pmid(doc)
        for ann in _annotations(doc):
            inf = ann.get("infons") or {}
            cid, ctype = inf.get("identifier"), inf.get("type")
            if not cid or cid == "-" or ctype in ("Species", "CellLine"):
                continue
            rec = acc.setdefault((ctype, cid), {"type": ctype, "concept": cid, "name": inf.get("name") or ann.get("text", ""),
                                               "seeds": set(), "mentions": 0})
            rec["seeds"].add(pmid)
            rec["mentions"] += 1
    return sorted(acc.values(), key=lambda r: (-len(r["seeds"]), -r["mentions"], r["concept"]))


# -- bulk files ------------------------------------------------------------
def _open_range(url: str, offset: int):
    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)


def head(url: str) -> dict:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return {"url": url, "last_modified": resp.headers.get("Last-Modified", ""),
                "content_length": int(resp.headers.get("Content-Length") or 0), "etag": resp.headers.get("ETag", "")}


class _Gunzip:
    """Incremental gunzip that also handles files made of several gzip members."""

    def __init__(self):
        self.d = zlib.decompressobj(wbits=31)

    def feed(self, block: bytes) -> bytes:
        out = []
        while block:
            if self.d.eof:
                self.d = zlib.decompressobj(wbits=31)
            out.append(self.d.decompress(block))
            block = self.d.unused_data if self.d.eof else b""
        return b"".join(out)

    @property
    def complete(self) -> bool:
        return self.d.eof


def scan_bulk(
    url: str,
    concepts: Iterable[str],
    *,
    open_range: OpenRange = _open_range,
    expected_bytes: int | None = None,
    chunk_size: int = 1 << 20,
    retries: int = 8,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[int], None] | None = None,
) -> tuple[list[dict], dict]:
    """Rows of a PubTator3 bulk entity file whose concept column is one of ``concepts``.

    Streams and decompresses without writing the file to disk. A dropped
    connection resumes from the last byte received with an HTTP Range request,
    feeding the same decompressor, so a 2 GB scan survives proxy resets.
    """
    concept_set = set(concepts)
    needles = [f"\t{c}\t".encode() for c in sorted(concept_set)]
    gz = _Gunzip()
    offset = 0
    carry = b""
    lines = 0
    resumes = 0
    rows: dict[tuple[str, str], dict] = {}

    def consume(buf: bytes, final: bool) -> bytes:
        nonlocal lines
        if final:
            complete, rest = buf, b""
        else:
            cut = buf.rfind(b"\n")
            if cut < 0:
                return buf
            complete, rest = buf[: cut + 1], buf[cut + 1 :]
        lines += complete.count(b"\n") + (1 if final and complete and not complete.endswith(b"\n") else 0)
        for needle in needles:
            start = 0
            while (i := complete.find(needle, start)) >= 0:
                ls = complete.rfind(b"\n", 0, i) + 1
                le = complete.find(b"\n", i)
                le = len(complete) if le < 0 else le
                parts = complete[ls:le].decode("utf-8", "replace").rstrip("\r").split("\t")
                if len(parts) >= 4 and parts[2] in concept_set:
                    key = (parts[0], parts[2])
                    resource = parts[4] if len(parts) > 4 else ""
                    if key in rows:   # the same paper and concept on two rows: merge them
                        prev = rows[key]
                        prev["mentions"] = "|".join(sorted(set(prev["mentions"].split("|")) | set(parts[3].split("|"))))
                        prev["resource"] = "|".join(sorted(set(filter(None, prev["resource"].split("|") + [resource]))))
                    else:
                        rows[key] = {"pmid": parts[0], "type": parts[1], "concept": parts[2],
                                     "mentions": parts[3], "resource": resource}
                start = le
        return rest

    failures = 0
    while True:
        try:
            resp = open_range(url, offset)
            try:
                status = getattr(resp, "status", None) or getattr(resp, "code", 200)
                if offset and status != 206:      # server ignored Range: skip what we already have
                    skip = offset
                    while skip:
                        got = resp.read(min(skip, chunk_size))
                        if not got:
                            raise ConnectionError("short read while skipping to resume offset")
                        skip -= len(got)
                while True:
                    block = resp.read(chunk_size)
                    if not block:
                        break
                    offset += len(block)
                    carry = consume(carry + gz.feed(block), final=False)
                    if progress:
                        progress(offset)
            finally:
                close = getattr(resp, "close", None)
                if close:
                    close()
            if expected_bytes and offset < expected_bytes:
                raise ConnectionError(f"stream ended at {offset} of {expected_bytes} bytes")
            break
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, OSError, EOFError) as exc:
            failures += 1
            if failures > retries:
                raise RuntimeError(f"bulk scan of {url} failed at byte {offset}: {exc}") from exc
            resumes += 1
            sleep(min(60, 2 ** failures))
    consume(carry, final=True)
    if not gz.complete:
        raise RuntimeError(f"{url} ended before the gzip stream was complete (byte {offset})")
    ordered = sorted(rows.values(), key=lambda r: (int(r["pmid"]) if r["pmid"].isdigit() else 0, r["pmid"], r["concept"]))
    return ordered, {"compressed_bytes": offset, "lines": lines, "rows": len(ordered), "resumes": resumes}
