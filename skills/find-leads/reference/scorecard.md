# The leads file

`leads.json` holds the scorecard and the evidence. `score.py` reads it and does all the maths. Set `as_of` to the day you gathered the evidence (it defaults to today); `domain` is the bare host.

```json
{
  "as_of": "2026-10-17",
  "icp": {
    "summary": "Software services firms selling to the US and Europe",
    "signs": [
      {"id": "services", "label": "Sells software development services", "weight": 3},
      {"id": "size", "label": "50 to 1,000 employees", "weight": 2, "required": true}
    ],
    "signals": [
      {"id": "hiring_bd", "label": "Hiring sales or business development", "weight": 3},
      {"id": "funding", "label": "Raised funding", "weight": 2}
    ]
  },
  "leads": [
    {
      "company": "Example Labs",
      "domain": "example.com",
      "summary": "Builds web and mobile apps for US retailers.",
      "signs": [
        {"id": "services", "state": "met", "source": "https://example.com/services", "quote": "We build custom web and mobile apps"},
        {"id": "size", "state": "unknown"}
      ],
      "signals": [
        {"id": "hiring_bd", "date": "2026-10-02", "detail": "Hiring a Business Development Manager, US", "source": "https://boards.greenhouse.io/example/jobs/123"}
      ],
      "questions": ["How many people work there?"],
      "first_question": "I saw you're hiring a BD manager for the US. Is that a new market or more of the same?"
    }
  ]
}
```

## Rules the script enforces

- A sign marked `met` or `not_met` without an `http(s)` source is treated as `unknown`.
- A signal without a source, without a `YYYY-MM-DD` date, or dated in the future is dropped.
- A `required` sign only rules a lead out when it is `not_met` with a source. Unknown never rules a lead out.
- Ids that are not in the scorecard are ignored. Duplicate companies (same domain, after removing `https://`, `www.` and paths) are kept once. A sign listed twice keeps the entry with evidence.
- Every change is reported as a warning, so you can go back to the source.

## The formula

| Score | How it is worked out |
|---|---|
| Fit (0–100) | weight of `met` signs ÷ weight of known signs (`met` + `not_met`). Unknown signs count neither way. |
| Coverage (0–100) | weight of known signs ÷ weight of all signs. Below 50 the fit is "thin". |
| Intent (0–100) | for each signal type, its weight × the recency of its freshest copy; summed, ÷ total signal weight. Five job posts count as one hiring signal. Recency: 0–30 days 1.0, 31–90 days 0.6, 91–180 days 0.3, older 0. |

| Priority | Rule |
|---|---|
| Call now | fit ≥ 60, coverage ≥ 50, intent ≥ 30 |
| Nurture | fit ≥ 60, coverage ≥ 50, intent < 30 |
| Qualify | intent ≥ 30, but fit below 60 or thin |
| Skip | everything else, and any lead that fails a sign marked `"required": true` |

Ranking: priority, then fit, then intent, then coverage.
