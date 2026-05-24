# Demo Results

This directory contains sanitized demo outputs from the built-in `payment_risk.py` PR fixture.

Included files:

```text
council-report.md       # Sanitized council baseline report
council-judge.md        # AI Judge Markdown score for council mode
council-judge.json      # AI Judge JSON score for council mode
debate-report.md        # Sanitized debate mode report
debate-judge.md         # AI Judge Markdown score for debate mode
debate-judge.json       # AI Judge JSON score for debate mode
company-rag-report.md   # Sanitized Company Knowledge RAG snapshot
```

Excluded files:

```text
transcript*.jsonl       # Full execution traces are not included
judge_input*.json       # Large standardized judge inputs are not included
.env                    # Local environment files are not included
```

## Comparison

| Metric | Council | Debate |
|---|---:|---:|
| AI Judge overall score | 72 | 92 |
| critical issue coverage | 100 | 100 |
| evidence quality | 85 | 95 |
| severity accuracy | 70 | 90 |
| duplicate/noise control | 40 | 98 |
| actionability | 90 | 96 |
| report clarity | 80 | 85 |

These results are intended as a reproducible demo comparison, not as a universal benchmark.

## Company Knowledge RAG

The latest version adds company knowledge RAG on top of the original council/debate comparison.

What changed:

- Company policies live under `knowledge/company/`.
- `retrieve_company_policy` retrieves policy chunks with DashScope embeddings when available.
- If embeddings are unavailable, retrieval falls back to keyword matching and records `keyword_fallback`.
- Findings can include `company_policy` evidence in their evidence chain.
- Standardized reports and `judge_input.json` can include `policy_references`.
- A new `company-policy-reviewer` can proactively raise policy-violation findings, instead of only attaching policy evidence to findings discovered by other reviewers.

This does not replace the original demo comparison. It adds another evaluation angle:

```text
Before RAG: did the agent find security/correctness/test issues?
After RAG: did the agent tie those issues back to company-specific standards?
```

Suggested additional metrics for future demo results:

| Metric | Meaning |
|---|---|
| policy citation rate | Accepted findings with at least one relevant `policy_reference` |
| policy precision | Whether cited company policies actually support the finding |
| severity alignment | Whether severity matches company standards |
| reviewer acceptance | Whether humans accept policy-backed findings more often |

See `company-rag-report.md` for a compact snapshot of policy-backed findings, `company_policy` evidence, and the new `company-policy-reviewer`.
