# Batch JSONL

`batch` reads JSONL from stdin, keeps up to `--concurrency` requests in flight (default 1), retries transient failures up to `--retries` times, flushes one output line per success in input order, and stops at the first failure with the earlier lines intact. With `--dry-run` it sends nothing, prints one report after the last line, and prints nothing when a line is invalid; see [Dry run](./dry-run.md).

## Sub-features

- `batch-order` output order follows input; duplicate IDs stay duplicated; an ID holding U+2028 stays on its own output line.
- `batch-api-failure` an API failure on line N with `--retries 0` exits 1 with `batch stopped at line N`, keeps lines 1..N-1, and sends nothing after N at concurrency 1.
- `batch-retry-recovers` 429 with `Retry-After: 1`, then 503, then 200 on line 1 still yields exactly one result per line in input order, and the run waits at least the `Retry-After` second.
- `batch-retry-exhausted` line 2 failing every attempt exits 1 naming line 2 with `gave up after N attempts`, keeps line 1, and sends nothing for line 3.
- `batch-concurrency` `--concurrency 3` never has more than 3 requests at the stub at once, and output order equals input order although later lines finish first.
- `batch-invalid-line` an invalid line N is never sent.
- `batch-blank-line` a blank line is an error.

## How to get to it (user POV)

- `python3 jev.py batch --questions q.json < records.jsonl > results.jsonl`

## Driving it with verify.py

Preconditions:

- Doctor passes. Each step queues stub replies in order.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run batch`. Every step prints `PASS`.
- **Prefix proof.** The `API failure on line 2` step shows one stdout line with id `1`, stderr naming line 2, and exactly two stub requests.
- **Retry proof.** The `429 Retry-After` step's `stub saw` lists states `a, a, a, b, c`, and its timing line shows at least 1s.
- **Concurrency proof.** The `concurrency 3` step's `stub saw` records `in_flight` per request; the maximum is exactly 3.
- **Exhaustive cover.** The flush and prefix invariant over every short sequence of line outcomes lives in the `formal-verify-jev` skill.

## Gotchas

- Output uses `ensure_ascii=False`, so U+2028 can appear raw inside a line. Split JSONL on `\n` only, never `str.splitlines()`.
- The stub answers in arrival order, not line order. Under concurrency, the queue entries are not tied to specific lines, so concurrency steps queue identical successful replies and vary only the delay.
- Ctrl-C exits 130 with `batch stopped at line N; interrupted` after running requests finish. No journey drives a signal; drive it ad hoc if that path changes.

- Stderr for an unparseable API response says `invalid JSON record`, blaming the input line. This is known finding F2 in `formal-verify-jev`; do not assert that wording as correct behavior.
