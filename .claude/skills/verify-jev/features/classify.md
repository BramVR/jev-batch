# Classify one record

`classify` sends one `{id,state}` record and prints `{"id": <id>, ...<full API response>}` with every probability untouched. With `--dry-run` it sends nothing and prints an estimate report instead; see [Dry run](./dry-run.md).

## Sub-features

- `classify-stdin` reads the record from stdin.
- `classify-input` reads the record from `--input record.json`; object IDs pass through.
- `classify-verbatim` output equals the response with `id` first, floats like `0.30000000000000004`, `1e-07`, `-0.0`, and unknown fields kept.
- `classify-request` POST to `<base>/systemone` with body exactly `model`, `state`, `questions`, `User-Agent: jev-cli/0.1`, and no `Authorization` off TypeSafe.
- `classify-retry-exhausted` HTTP 503 with `--retries 1` makes exactly two requests, exits 1 with `gave up after 2 attempts`, and never echoes the response body.
- `classify-retry-recovers` HTTP 503 then 200 prints the normal result after two requests.
- `classify-permanent-error` HTTP 401 and 422 make one request each and exit 1 with `not retryable`.
- `classify-timeout` a slow backend past `--timeout` with `--retries 0` makes one request and exits 1.

## How to get to it (user POV)

- `printf '%s\n' '{"id":"t-1","state":"..."}' | python3 jev.py classify --questions q.json`
- `python3 jev.py classify --questions q.json --input record.json`

## Driving it with verify.py

Preconditions:

- Doctor passes. The stub answers with a fixed Choice response.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run classify`. Every step prints `PASS`.
- **Request proof.** In the transcript, `stub saw` for the first step shows path `/zen/v1/systemone`, the exact body, and no `authorization` header although `TYPESAFE_API_KEY` was set.
- **Output proof.** The stdout line equals the stub body with `"id": "t-1"` prepended, byte for byte.

## Gotchas

- The stub returns HTTP 500 with a private body when its queue runs dry. An unexpected extra request shows up as a failure, not a hang.
- The timeout step takes about 0.5s by design; the stub's late reply is discarded.
- Retries default to 3. A step that expects a single request on a transient failure must pass `--retries 0`, or the CLI retries into the stub's dry-queue 500s.
- Backoff without `Retry-After` is random, up to 0.5s for the first retry. Retry steps take a fraction of a second.
