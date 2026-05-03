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
