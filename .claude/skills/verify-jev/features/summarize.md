# Summarize results

`summarize` turns saved `classify` or `batch` JSONL into one JSON summary of a Choice answer: per-label counts, the first three IDs per label, and ten fixed confidence bins. It runs offline.

## Sub-features

- `summarize-counts` `counts` covers every record, seeded in `--questions` criteria order with zeros, then first-seen labels. Seeding also applies when the question is inferred from the input.
- `summarize-examples` `examples` keeps the first three IDs per label in input order, unchanged, including IDs holding U+2028 read through `--input`.
- `summarize-bins` bins `0.0-0.1` through `0.9-1.0` include their lower edge; 0 lands in the first bin and 1 in the last.
- `summarize-confidence-diagnostics` missing confidence counts as `missing`; non-numbers as `malformed` and values outside [0, 1] as `out_of_range`, each with a `jev: line N: ...` stderr line and exit 0.
- `summarize-selection` several question names in the input exit 1 unless `--question` picks one.
- `summarize-empty` empty input with a known question prints `records: 0` and zero bins.
- `summarize-choice-only` a Score, Noul, or Box answer exits 1 naming the line.
- `summarize-offline` no request leaves, whatever `--base-url` or `TYPESAFE_API_KEY` say.

## How to get to it (user POV)

- `python3 jev.py summarize [--question NAME] [--questions q.json] [--input results.jsonl] < results.jsonl`
- `python3 scripts/demo_sessions.py --send` calls it for the demo's `classification_counts`, `example_ids_by_category`, and `confidence`.

## Driving it with verify.py

Preconditions:

- Doctor passes. The run writes synthetic `results.jsonl` (seven `topic` results, one ID ending in U+2028) and `mixed.json` (Choice plus Noul) into `work/`.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run summarize`. Every step prints `PASS`.
- **Offline proof.** The first step points `--base-url` at the stub with the sentinel key set; the stub records zero requests.
- **Boundary proof.** The first step's stdout shows `0.0-0.1: 1`, `0.1-0.2: 1`, `0.9-1.0: 2`, and missing, malformed, out-of-range at 1 each.

## Gotchas

- All lines are parsed before any answer is checked, so an invalid JSON line is reported ahead of an earlier non-Choice answer.
- The step-level success rule allows stderr only when the step names expected stderr; diagnostics are expected on exit 0 here.
- That `summarize` reads no credentials is proven in `tests/test_cli.py` by patching `jev.api_key`; the stub can only show nothing was sent.
