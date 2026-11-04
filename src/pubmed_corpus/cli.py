"""Command line: ``pubmed-corpus <step> <project-dir>``."""

from __future__ import annotations

import argparse
import json
import sys

from . import corpus as corpus_mod
from . import report as report_mod
from . import screen as screen_mod
from . import steps
from .project import Project

COMMANDS = ("init", "terms", "query", "pubtator", "mentions", "expand", "metadata", "screen-queue", "assemble", "report",
            "status")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pubmed-corpus", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a project directory with project.yaml and mentions.yaml")
    p.add_argument("project")
    p.add_argument("--name", required=True, help="disease name")
    p.add_argument("--mesh", required=True, help="MeSH descriptor UI, e.g. D054972")
    p.add_argument("--concept", action="append", default=[], help="PubTator concept ID (default MESH:<mesh>)")
    p.add_argument("--seed", action="append", required=True, help="seed PMID, optionally PMID:key")

    for name, help_ in [
        ("terms", "draft query.yaml from MeSH and the seeds' PubTator annotations (network)"),
        ("query", "run every non-rejected clause in query.yaml (network)"),
        ("pubtator", "stream the PubTator3 bulk file and keep rows for the project's concepts (network, ~2 GB)"),
        ("mentions", "write the wording audit for the PubTator rows (offline)"),
        ("expand", "one round of references, citing papers and similar articles from the seeds (network)"),
        ("metadata", "fetch PubMed records for every candidate and reference paper (network)"),
        ("screen-queue", "apply the wording rule and queue the rest of the expansion papers for screening (offline)"),
        ("assemble", "freeze corpus.tsv, excluded.tsv and manifest.json (offline)"),
        ("report", "write REPORT.md (offline)"),
        ("status", "show which steps are done and what is blocking (offline)"),
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("project")
        if name in ("terms", "query", "expand", "metadata"):
            p.add_argument("--refresh", action="store_true", help="ignore cached responses")
        if name == "terms":
            p.add_argument("--min-seed-mentions", type=int, default=1)
        if name == "query":
            p.add_argument("--counts-only", action="store_true", help="print hit counts without writing outputs")
        if name in ("assemble", "report"):
            p.add_argument("--check", action="store_true", help="fail if the committed outputs differ from a fresh build")
        if name == "assemble":
            p.add_argument("--allow-unscreened", action="store_true", help="exclude unscreened papers instead of failing")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return _run(args)
    except (corpus_mod.AssemblyError, FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False))


def _run(args) -> int:
    if args.command == "init":
        steps.init(args.project, name=args.name, mesh=args.mesh, concepts=args.concept, seeds=args.seed)
        print(f"created {args.project}/project.yaml")
        return 0
    project = Project.load(args.project)
    cmd = args.command
    if cmd in ("terms", "query", "expand", "metadata"):
        http, eutils = steps.clients(project, refresh=args.refresh)
        if cmd == "terms":
            _print(steps.terms(project, http, eutils, min_seed_mentions=args.min_seed_mentions))
        elif cmd == "query":
            _print(steps.query(project, eutils, counts_only=args.counts_only))
        elif cmd == "expand":
            _print(steps.expand(project, http, eutils))
        else:
            _print(steps.metadata(project, eutils))
        return 0
    if cmd == "pubtator":
        info = steps.pubtator_scan(project)
        _print({k: info[k] for k in ("last_modified", "compressed_bytes", "lines", "rows", "papers", "resumes")})
        return 0
    if cmd == "mentions":
        result = steps.mentions(project)
        _print(result)
        return 1 if result["blocking"] else 0
    if cmd == "screen-queue":
        _print(screen_mod.build_queue(project))
        return 0
    if cmd == "assemble":
        corpus, excluded, manifest = corpus_mod.assemble(project, allow_unscreened=args.allow_unscreened)
        drift = corpus_mod.write_or_check(project, corpus_mod.render(corpus, excluded, manifest), check=args.check)
        if args.check:
            if drift:
                print(f"out of date: {', '.join(drift)}; rerun `pubmed-corpus assemble`", file=sys.stderr)
                return 1
            print("corpus is up to date")
            return 0
        _print(manifest["counts"] | {"excluded": manifest["excluded"]})
        return 0
    if cmd == "report":
        text = report_mod.build(project)
        path = project.root / "REPORT.md"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                print("REPORT.md is out of date; rerun `pubmed-corpus report`", file=sys.stderr)
                return 1
            print("REPORT.md is up to date")
            return 0
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
        return 0
    if cmd == "status":
        print("\n".join(steps.status(project)))
        return 0
    raise AssertionError(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
