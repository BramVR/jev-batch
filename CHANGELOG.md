# Changelog

## 0.1.0

- Add `--version`, which prints `jev.py 0.1.0` from a checkout; the User-Agent takes its version from the same constant.

- Rename the project to jev-batch, so requests now send `User-Agent: jev-batch/0.1.0`; add the MIT `LICENSE` and a README quick start that runs on the free OpenCode default with no key.

- Test the documented Python 3.9 floor in CI; on Python 3.8 or older, `jev.py` and `verify.py` now exit 1 with `needs Python 3.9+; this is <version>` instead of a traceback mid-run.

- Read and write stdin, stdout, and stderr as UTF-8 whatever the locale, so UTF-8 JSONL no longer fails with `could not read input` under an ASCII or legacy locale; the demo drops its `PYTHONIOENCODING` pin.

- Give each command its own flags: a flag the command does not use (`--cache`, `--dry-run`, `--retries`, `--chunk-tokens` on `eval`/`summarize`, `--concurrency` on `classify`, `--base-url` on `summarize`, `--input` on `batch`/`eval`) now exits 2 as a usage error instead of exit 1 or being silently ignored; `eval` without `--labels` also exits 2; flags are no longer abbreviated, so `classify --question` is not read as `--questions`; per-command flags moved to `jev.py <command> --help`.

- Reject a `--base-url` with spaces, control characters, or non-ASCII text before reading credentials, instead of crashing with a traceback or pricing a dry run for a host urllib would not contact; batch now reports an unparseable API response as `System One returned invalid JSON` instead of blaming the input line.

- Add opt-in `--chunk-tokens` to split long string state for Choice questions into bounded requests and aggregate by exact equal-weight mean probability, keeping every chunk response for audit; chunks retry, cache, and dry-run like any request, and label-rejected chunk answers are never cached.

- Retry 429, 5xx, connection errors, timeouts, and `Endpoint is unavailable` error bodies (any status except 401, 403, 422) with capped, jittered exponential backoff that honors `Retry-After`; add `--retries` (default 3) and bounded, order-preserving batch `--concurrency` (default 1).

- Add offline `summarize` for Choice results: per-label counts, first three example IDs, and fixed confidence bins with missing, malformed, and out-of-range diagnostics; the demo reuses it.
- Add `--dry-run` to classify and batch: validate, then report labelled input-token estimates and dated USD cost per backend/model, with no credential reads or HTTP; unknown prices stay null.

- Add offline `eval` that scores saved batch results for one Choice question against hand-labeled JSONL, reporting accuracy, a labeled confusion matrix, and ten fixed calibration buckets with scored and unscored denominators.

- Add opt-in `--cache DIR` that stores validated responses keyed by endpoint and exact request body, so reruns and resumed batches send only missing records; the session demo caches under `.private/demo/cache`.
- Redact secrets, bearer and API tokens, emails, and Unix/Windows home paths from demo samples before caching and again before sending, on by default with `--no-redact` to opt out; summaries report only policy and counts.

- Accept version-1 question documents (`schema_version` plus `questions`) alongside legacy raw mappings, and reject unknown versions, empty mappings, and malformed question files before any request with non-private errors.

- Reuse the approved private session sample for demo sends, cap samples at 20, and report category counts and example IDs; verify eight real OpenCode classifications without committing excerpts.

- Default to free OpenCode Jev 1.13 with configurable base URL and model, isolate TypeSafe credentials to its HTTPS origin, and reject unsupported Box requests locally.

- Add a dependency-free Python Jev CLI for single records and streaming JSONL batches, preserving IDs and complete API responses with environment or macOS Keychain authentication.
- Add a classify-many agent skill, failure-pattern question mapping, and private session demo; document the missing-key blocker without fabricated classifications.

- Support Box questions with base64 image assets and the required images header.
