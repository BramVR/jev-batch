# Batch JSONL

`batch` reads JSONL from stdin, sends one request per line in order, flushes one output line per success, and stops at the first failure with the earlier lines intact. With `--dry-run` it sends nothing, prints one report after the last line, and prints nothing when a line is invalid; see [Dry run](./dry-run.md).

## Sub-features

- `batch-order` output order follows input; duplicate IDs stay duplicated; an ID holding U+2028 stays on its own output line.
- `batch-api-failure` an API failure on line N exits 1 with `batch stopped at line N`, keeps lines 1..N-1, and sends nothing after N.
- `batch-invalid-line` an invalid line N is never sent.
- `batch-blank-line` a blank line is an error.

## How to get to it (user POV)

- `python3 jev.py batch --questions q.json < records.jsonl > results.jsonl`

## Driving it with verify.py

Preconditions:

- Doctor passes. Each step queues stub replies in order.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run batch`. Every step prints `PASS`.
- **Prefix proof.** The `API failure on line 2` step shows one stdout line with id `1`, stderr naming line 2, and exactly two stub requests.
- **Exhaustive cover.** The flush and prefix invariant over every short sequence of line outcomes lives in the `formal-verify-jev` skill.

## Gotchas

- Output uses `ensure_ascii=False`, so U+2028 can appear raw inside a line. Split JSONL on `\n` only, never `str.splitlines()`.

- Stderr for an unparseable API response says `invalid JSON record`, blaming the input line. This is known finding F2 in `formal-verify-jev`; do not assert that wording as correct behavior.
