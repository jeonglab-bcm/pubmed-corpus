"""A disease project: one directory holding its configuration, decisions and outputs.

    projects/<slug>/
      project.yaml          disease, MeSH UI, PubTator concepts, seeds, expansion and screening settings
      query.yaml            named PubMed clauses, each approved / proposed / rejected
      mentions.yaml         the wording audit decisions for PubTator rows
      screen/decisions.jsonl  relevance decisions for papers found only through the seeds
      reference/            optional reference PMID lists, used only by the report
      out/                  everything the steps write; committed, never edited by hand
      cache/                raw API responses and full records; not committed
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

QUERY_STATUSES = ("approved", "proposed", "rejected")


@dataclass
class Project:
    root: Path
    config: dict

    @classmethod
    def load(cls, root: Path | str) -> "Project":
        root = Path(root)
        path = root / "project.yaml"
        if not path.exists():
            raise FileNotFoundError(f"{path} not found; create it with `pubmed-corpus init`")
        config = yaml.safe_load(path.read_text()) or {}
        for key in ("name", "mesh", "seeds"):
            if not config.get(key):
                raise ValueError(f"{path}: `{key}` is required")
        return cls(root, config)

    # -- paths ----------------------------------------------------------
    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def query_file(self) -> Path:
        return self.root / "query.yaml"

    @property
    def mentions_file(self) -> Path:
        return self.root / "mentions.yaml"

    @property
    def decisions_file(self) -> Path:
        return self.root / "screen" / "decisions.jsonl"

    @property
    def queue_file(self) -> Path:
        return self.root / "screen" / "queue.jsonl"

    # -- settings -------------------------------------------------------
    @property
    def seeds(self) -> list[dict]:
        out = []
        for s in self.config["seeds"]:
            s = {"pmid": str(s)} if not isinstance(s, dict) else {**s, "pmid": str(s["pmid"])}
            s.setdefault("key", s["pmid"])
            out.append(s)
        return out

    @property
    def seed_pmids(self) -> list[str]:
        return [s["pmid"] for s in self.seeds]

    @property
    def concepts(self) -> list[str]:
        return list((self.config.get("pubtator") or {}).get("concepts") or [f"MESH:{self.config['mesh']}"])

    @property
    def pubtator_file(self) -> str:
        return (self.config.get("pubtator") or {}).get("file", "disease2pubtator3.gz")

    @property
    def related_top_k(self) -> int:
        return int((self.config.get("expansion") or {}).get("related_top_k", 100))

    @property
    def expansion_routes(self) -> list[str]:
        return list((self.config.get("expansion") or {}).get("routes") or ["references", "cited_by", "related"])

    @property
    def exclude_retracted(self) -> bool:
        return bool(self.config.get("exclude_retracted", True))

    @property
    def screening_criteria(self) -> str:
        return ((self.config.get("screening") or {}).get("criteria") or "").strip()

    def reference_sets(self) -> dict[str, set[str]]:
        out = {}
        for ref in self.config.get("references") or []:
            path = self.root / ref["path"]
            out[ref.get("name", path.stem)] = {line.split()[0] for line in path.read_text().splitlines()
                                               if line.strip() and not line.startswith("#")}
        return out

    # -- query file -----------------------------------------------------
    def clauses(self) -> list[dict]:
        if not self.query_file.exists():
            raise FileNotFoundError(f"{self.query_file} not found; run `pubmed-corpus terms` first")
        data = yaml.safe_load(self.query_file.read_text()) or {}
        clauses = data.get("clauses") or []
        seen = set()
        for c in clauses:
            for key in ("id", "query", "status"):
                if not c.get(key):
                    raise ValueError(f"{self.query_file}: clause missing `{key}`: {c}")
            if c["status"] not in QUERY_STATUSES:
                raise ValueError(f"{self.query_file}: clause {c['id']} has status {c['status']!r}; use one of {QUERY_STATUSES}")
            if c["id"] in seen:
                raise ValueError(f"{self.query_file}: duplicate clause id {c['id']}")
            seen.add(c["id"])
        return clauses

    # -- run log --------------------------------------------------------
    def log_step(self, step: str, info: dict) -> None:
        path = self.out / "run_log.json"
        log = json.loads(path.read_text()) if path.exists() else {}
        log[step] = {"date": dt.date.today().isoformat(), **info}
        write_json(path, log)

    def run_log(self) -> dict:
        path = self.out / "run_log.json"
        return json.loads(path.read_text()) if path.exists() else {}


# -- deterministic file helpers -------------------------------------------
def write_tsv(path: Path, header: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        w.writerow(header)
        for r in rows:
            w.writerow([_cell(r.get(h, "")) for h in header])


def _cell(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (list, tuple, set)):
        return ";".join(str(x) for x in v)
    return str(v).replace("\t", " ").replace("\n", " ")


def read_tsv(path: Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    out = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{n}: not valid JSON: {exc}") from exc
    return out


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else ""


def pmid_key(p: str) -> int:
    return int(p) if str(p).isdigit() else 0
