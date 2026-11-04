---
name: disease-corpus
description: Build a reproducible PubMed literature corpus for one disease, starting from seed papers. Drafts a named-clause PubMed query from MeSH and the seeds' own wording, adds papers that name the disease only in full text (PubTator3 bulk tags, after a wording audit), expands one round of references, citing papers and similar articles from the seeds with a relevance screen, and freezes the corpus with every paper's route and a report. Use when asked to collect, build, update or evaluate a disease paper corpus or literature set.
---

# Disease corpus from seed papers

You orchestrate the `pubmed-corpus` command line tool. The tool does every
deterministic step (searching, downloading, matching, counting, freezing).
You do the three things that need judgement, and the user approves two of
them:

| Judgement | Where it is recorded | Who decides |
|---|---|---|
| Extra query clauses (historical names, spelling variants) | `query.yaml` | you propose, **the user approves** |
| Which PubTator wordings really name the disease | `mentions.yaml` | you draft, **the user approves** |
| Relevance of papers reached only from the seeds | `screen/decisions.jsonl` | you decide, from title and abstract |

Everything for one disease lives in `projects/<slug>/`. Never edit files in
`out/` by hand: change a decision file and rerun the step.

## What you need first

- The disease name and its MeSH descriptor UI (for example `D054972`). If the
  user does not know it, look it up in MeSH and confirm it with them.
- 3–10 seed PMIDs the user trusts. Ask for at least one recent review and one
  primary study.
- Optionally, a reference list of PMIDs (a systematic review's included
  studies, an older corpus) to measure recall against. It is used only by the
  report, never to build the corpus.

Install once from the repository root: `pip install -e .`

## Steps

Run the steps in order. Each prints a JSON summary; read it before going on.

1. **Create the project.**
   `pubmed-corpus init projects/<slug> --name "<disease>" --mesh <UI> --seed <PMID>:<key> --seed <PMID>:<key>`
   Then add any `references:` entries to `projects/<slug>/project.yaml`
   (`name`, `path` under the project directory, `description`).

2. **Draft the query.** `pubmed-corpus terms projects/<slug>`
   This writes `query.yaml` (only if it does not exist yet) and
   `query.draft.yaml`, plus `out/terms.tsv`, `out/seed_concepts.tsv` and
   `out/seeds.tsv`. Read them, then add clauses with `status: proposed` and
   a one-sentence `rationale` for:
   - historical or alternative names the seeds and MeSH do not use
     (the drafted query only knows modern wording; for POTS the historical
     names reach hundreds of papers nothing else does),
   - plurals and spelling variants (PubMed phrase search does not stem),
   - acronyms that need a guard (keep the `tiab_acronym_guarded` pattern).
   Check counts with `pubmed-corpus query projects/<slug> --counts-only`.
   **Checkpoint 1:** show the user every clause (id, status, count,
   rationale) and ask which to approve. Set `status` to what they decide.
   Do not approve your own proposals.

3. **Run the query.** `pubmed-corpus query projects/<slug>`

4. **Scan PubTator.** `pubmed-corpus pubtator projects/<slug>`
   It streams a ~2 GB file for several minutes; run it in the background. It
   resumes by itself after dropped connections.

5. **Audit the wordings.** `pubmed-corpus mentions projects/<slug>`
   Open `out/mention_audit.tsv`. Draft `mentions.yaml` so that every wording
   with at least `min_papers_to_decide` papers is decided:
   - approve wordings that name the disease itself, including its acronym,
     subtypes ("neuropathic POTS") and misspellings;
   - reject symptoms, signs, other diseases and generic words that PubTator
     mapped to the concept (for POTS: "postural instability", "posture",
     "dystonic posturing" — mostly Parkinson's papers).
   Prefer explicit lists; use patterns only for families of spellings.
   **Checkpoint 2:** show the user the approved and rejected wordings with
   their paper counts, and apply their changes. The command exits 1 while any
   frequent wording is undecided.

6. **Expand from the seeds.** `pubmed-corpus expand projects/<slug>`
   Tell the user about any seed with `icite_found` 0 in
   `out/expansion_seeds.tsv`: iCite lags recent papers, so that seed
   contributes no references or citing papers.

7. **Fetch records.** `pubmed-corpus metadata projects/<slug>`

8. **Screen.** `pubmed-corpus screen-queue projects/<slug>`
   Papers that use an approved wording in their title or abstract are
   included by rule. The rest are in `screen/queue.jsonl`. Judge each one
   against `screening.criteria` in `project.yaml`, from the queue entry only
   (title, abstract, how it was found), and append one line per paper to
   `screen/decisions.jsonl`:
   `{"pmid": "...", "decision": "include", "reason": "...", "screener": "agent"}`
   - `decision` is `include` or `exclude`; `reason` names the deciding fact
     in under 15 words.
   - With no abstract and an ambiguous title, include and start the reason
     with `uncertain:`.
   - Never change an earlier line; a contradicting line makes assembly fail.
   - For a long queue, split it into batches of about 100 and give each batch
     to a subagent with the criteria text verbatim and this format.
   Run `screen-queue` again; the queue must come back empty.

9. **Freeze and report.**
   `pubmed-corpus assemble projects/<slug>` then `pubmed-corpus report projects/<slug>`
   Give the user the numbers from `REPORT.md` (tiers, what the audit and the
   screen removed, recall against each reference, and what proposed clauses
   would add), not estimates.

10. **Check and commit.** `pubmed-corpus status projects/<slug>`, then
    `pubmed-corpus assemble projects/<slug> --check` and
    `pubmed-corpus report projects/<slug> --check`. Commit the project
    directory; `cache/` is ignored by git.

## Reading the outputs

`out/corpus.tsv` has one row per paper: `tier` (seed, core, fulltext,
expansion), the approved `clauses` that retrieved it, the approved
`pubtator_wordings`, the `expansion` routes (`<seed key>:<route>`), and
whether the paper has an abstract and a PMCID. A narrower corpus (for
example, core only, or without one clause) is a filter on this file.

Full-text-tier papers mostly name the disease once in the body. They are not
screened at collection time; downstream extraction should read only the
passages around the mention.

## When something fails

- Network errors: rerun the step. Record fetches are cached in `cache/`.
- `assemble` refuses to run while a frequent wording is undecided, an
  expansion paper has no decision, or a PMID has contradictory decisions.
  The message says which; fix the decision file, do not bypass the check.
- Do not use `--allow-unscreened` unless the user asks for a provisional
  corpus, and say so when you report it.
