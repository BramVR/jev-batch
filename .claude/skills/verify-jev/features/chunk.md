# Chunked Choice text

`--chunk-tokens N` splits long string state into requests that each fit N estimated tokens (UTF-8 bytes) after a 512-token allowance and the questions JSON. It then prints one record per input with mean-aggregated Choice answers and every chunk response.

## Sub-features

- `chunk-small` state within capacity, including empty state, is sent once and printed exactly as without the flag.
- `chunk-split` oversized state becomes consecutive, non-overlapping requests of at most `state_capacity` bytes whose states concatenate to the original. Output keeps the input `id`, `chunking` metadata, and per-chunk `start`, `end`, `estimated_tokens`, and verbatim `response`.
- `chunk-mean` each label's probability is the equal-weight mean across chunks. The choice is the highest mean, and its mean is `confidence`.
- `chunk-tie` exact ties go to the label listed first in `criteria`.
- `chunk-unicode` splits fall on code points. A multi-byte character is never cut.
- `chunk-question-files` a version-1 question file gets the same capacity and requests as its legacy mapping, because overhead counts only the unwrapped mapping that is sent.
- `chunk-cache` with `--cache`, each chunk request is cached on its own. A label-rejected chunk answer is never stored, so a rerun resends only that chunk, and an identical rerun sends nothing.
- `chunk-batch` batch lines chunk independently. Small lines keep plain output. Under `--concurrency`, records run in parallel and their chunks run one after another, so output keeps input order.
- `chunk-retry` a transient chunk failure is retried under `--retries` and only that chunk is resent.
- `chunk-dry-run` `--dry-run --chunk-tokens` prices every chunk request and sends nothing. `summarize` and `eval` reject the flag.
- `chunk-failure` any failed chunk, including an unparseable response (`chunk i/n: System One returned invalid JSON`), exits 1 naming `chunk i/n`, prints no aggregate, and sends no later chunk.
- `chunk-guards` non-Choice questions, object or array state, assets, and budgets outside 1..32000 or too small for the question overhead exit 1 before any request.

## How to get to it (user POV)

- `python3 jev.py classify|batch --questions choice.json --chunk-tokens 30000`

## Driving it with verify.py

Preconditions:

- Doctor passes. `CHUNK_BUDGET` in `verify.py` is computed from the README formula so `choice.json` leaves 20 bytes of state per chunk.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run chunk`. Every step prints `PASS`.
- **Split proof.** The `oversized input` step's stub view shows two requests of 20 and 8 bytes with unchanged `questions`. Stdout shows `bug` at confidence `0.5` from dyadic probabilities that a reader can average by hand.
- **Can fail.** `run chunk --ref b69688c`, main before chunking, fails every step.
- **Live.** `run --live live-opencode` includes one synthetic 53-byte record split into three real requests.
- **Unit cover.** Empty input, exact boundaries, and label mismatches live in `tests/test_chunking.py`.

## Gotchas

- `start` and `end` are character offsets. Request sizes are bytes. Unicode steps assert both.
- Earlier chunk requests for a failed record were already sent. Assert the request count, not just the exit code.
- Default `--retries 3` resends a 5xx chunk with random backoff. Failure steps that count requests pass `--retries 0`.
- Free Jev 1.13 returns coarse per-chunk probabilities, so live aggregates often land on thirds. The live step checks the map's shape, not exact values.
