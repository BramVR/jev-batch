# Dry run

`classify --dry-run` and `batch --dry-run` validate exactly as a send would, then print one JSON report of labelled input-token estimates and a dated USD cost. No request is sent and no TypeSafe key or Keychain entry is read.

## Sub-features

- `dry-run-free` the default OpenCode `jev-1.13-free` reports status `free` and `estimated_cost_usd` `"0"`.
- `dry-run-priced` TypeSafe `--base-url https://api.typesafe.ai/v1 --model jev-1.14.0` reports status `priced`, `0.042` USD per million input tokens, and cost equal to `0.042 × total / 1e6`, with no key in the env.
- `dry-run-unknown` any other host or model reports status `unknown`, and price and cost are `null`, never a guess.
- `dry-run-rows` batch rows carry `line` and `id` in input order, and the total equals the sum of the rows. Unicode state counts one token per non-ASCII code point.
- `dry-run-assets` a record with image `assets` is flagged `assets_not_estimated`, and the cost becomes `null`.
- `dry-run-invalid` the first invalid batch line exits 1 with `batch stopped at line N` and no report.
- `dry-run-gating` Box on the free model is rejected exactly as a send would be.

## How to get to it (user POV)

- `python3 jev.py batch --dry-run --questions q.json < records.jsonl`
- `python3 jev.py classify --dry-run --questions q.json --base-url https://api.typesafe.ai/v1 --model jev-1.14.0 --input record.json`

## Driving it with verify.py

Preconditions:

- Doctor passes. No key is needed or set, except the sentinel in the unknown-price step.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run dry-run`. Every step prints `PASS`.
- **No-send proof.** The stub-backed steps show no `stub saw` block, although the stub had a reply queued.
- **Estimate proof.** Each report's stdout names `not a tokenizer count` in `token_method`, the pricing `date`, and `currency` `USD`.
- **Regression proof.** `run --ref 8de76f2 dry-run` fails every step, because that commit predates the flag.

## Gotchas

- The free and TypeSafe steps target real hostnames because pricing is keyed by host. They stay offline only because dry run never opens a connection. A regression that sends would surface as a network error or a missing-key exit 1, not a quiet pass.
- On macOS, a regression that reads credentials could still find a real Keychain entry, because the scrubbed `HOME` may not hide the login keychain. Dry run would still send nothing. `formal-verify-jev` proves in-process that `api_key` is never called.
- Token counts are a heuristic. Assert totals and pricing arithmetic, never a specific token count against a real tokenizer.
