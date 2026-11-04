# pubmed-corpus

A reusable agent skill that builds a literature corpus for one disease,
starting from a handful of seed papers. Claude Code runs the skill
([`.claude/skills/disease-corpus/SKILL.md`](.claude/skills/disease-corpus/SKILL.md));
the `pubmed-corpus` command line tool does every deterministic step, and the
agent and the user make the few judgements in plain files that are committed
with the corpus.

## How a paper gets in

| Tier | Route | Why it is there |
|---|---|---|
| `seed` | chosen by the user | the starting point |
| `core` | an approved PubMed clause (title, abstract, MeSH) | transparent, auditable, and what reviewers expect |
| `fulltext` | PubTator3 tags the disease in the paper with an approved wording, and no clause retrieved it | most of these name the disease only in the body of the full text, which no PubMed query can reach |
| `expansion` | a reference, citing paper or similar article of a seed, kept by a relevance screen | literature next to the disease that never names it in the title or abstract |

Every paper keeps every route that reached it (`out/corpus.tsv`), so a
narrower corpus is a filter, not a re-run.

## Design decisions, and the evidence for them

**A query defines the core; seeds do not.** On the POTS seeds, one round of
expansion reached 767 papers, only 22% of the 2,668-paper POTS query corpus,
and about a quarter of the papers outside it were reference-list noise (REDCap,
symptom scales). Seeds are used to draft the query and as a second route, not
as the corpus definition.

**The query is drafted from checkable sources.** `pubmed-corpus terms` uses
the MeSH descriptor, its entry terms, the wording PubTator finds in the seeds,
acronyms (guarded by concepts from the MeSH scope note), and NLM's previous
indexing for the topic (proposed only). The agent adds historical names and
variants as `proposed` clauses; only the user approves.

**PubTator bulk tags pass a wording audit.** In the 2026-10-06 release, 30,932
papers carry the POTS concept (MESH:D054972), but 27,360 of the 28,965 outside
the POTS query corpus use no POTS wording at all: "postural instability"
alone accounts for 12,649 papers, mostly Parkinson's disease. The tool lists
every wording with its paper count and refuses to assemble while a frequent
wording is undecided.

**The bulk files, not the search API.** The API search returns a ranked list
(31,243 "hits" for POTS, off-topic by rank 3,000), not a set. A monthly bulk
release is a fixed, citable snapshot; its Last-Modified date is recorded.

**Citations from iCite.** PubMed's `pubmed_pubmed_citedin` link listed 149
papers citing Low 2009 against 260 in iCite (NIH Open Citation Collection) and
248 in Europe PMC (checked 2026-11-04).

**Frozen and replayable.** `assemble` and `report` run offline from committed
files and are byte-identical for identical inputs; CI replays every committed
project with `--check`.

## Quick start

```bash
pip install -e ".[test]"
pytest                                   # offline: unit tests, the whole pipeline on a fake PubMed, project replays
PUBMED_CORPUS_NETWORK=1 pytest -m network   # live smoke tests against NCBI, iCite and PubTator
```

Then ask Claude Code to build a corpus ("build a corpus for <disease> from
these seeds") and it follows the skill, or run the steps yourself:

```bash
pubmed-corpus init projects/pots --name "Postural orthostatic tachycardia syndrome" --mesh D054972 --seed 19207771:low2009
pubmed-corpus terms projects/pots          # draft query.yaml; review and approve clauses
pubmed-corpus query projects/pots
pubmed-corpus pubtator projects/pots       # ~2 GB stream
pubmed-corpus mentions projects/pots       # decide wordings in mentions.yaml
pubmed-corpus expand projects/pots
pubmed-corpus metadata projects/pots
pubmed-corpus screen-queue projects/pots   # screen queued papers into screen/decisions.jsonl
pubmed-corpus assemble projects/pots
pubmed-corpus report projects/pots
```

Set `NCBI_API_KEY` to raise the E-utilities rate limit from 3 to 10 requests
per second, and `NCBI_TOOL_EMAIL` so NCBI can contact you instead of blocking
you.

## Layout

| Path | Contents |
|---|---|
| `src/pubmed_corpus/` | `eutils` (search with date splitting past the 10,000-record ESearch cap, records, similar articles, MeSH), `icite`, `pubtator` (seed annotations; resumable bulk scan), `query` (drafting), `mentions` (wording audit), `expand`, `screen`, `corpus` (assembly), `report`, `cli` |
| `.claude/skills/disease-corpus/SKILL.md` | the agent's instructions: steps, the two user checkpoints, screening rules |
| `projects/<slug>/` | one disease: `project.yaml`, `query.yaml`, `mentions.yaml`, `screen/decisions.jsonl`, `out/`, `REPORT.md` |
| `tests/` | offline tests; `test_live.py` is opt-in |

## Limitations

- Full text is reached only through PubTator, which covers the PMC
  open-access subset and author manuscripts; papers whose full text is not
  open contribute their abstract only.
- iCite lags recent papers: a seed published in the last few months may have
  no references or citing papers yet. The report flags such seeds.
- The relevance screen is a single reading by the agent, from title and
  abstract, recorded with its reason. It is not yet repeated or checked
  against a human sample.
- Collection only. Claim extraction and the evidence graph are later steps.
