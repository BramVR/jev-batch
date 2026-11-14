# jev-cli

Python 3.9+ stdlib CLI for Jev System One. No installation or dependencies. Defaults to free OpenCode `POST https://opencode.ai/zen/v1/systemone`, model `jev-1.13-free`, without authentication.

## Classify records

Use `--base-url` or `JEV_BASE_URL` to select the API base URL and `--model` or `JEV_MODEL` to select the model. Flags override environment values, which override defaults. The CLI appends `/systemone` after removing trailing slashes.

For TypeSafe, select `--base-url https://api.typesafe.ai/v1 --model jev-1.14.0`. Set `TYPESAFE_API_KEY`, or store a generic password under macOS Keychain service `typesafe`. The environment key takes precedence. Only the exact HTTPS TypeSafe origin reads these credentials. Other origins receive no TypeSafe key and no Authorization header. The CLI never reads credential files or accepts a key argument.

Write a version-1 question document to `questions.json`:

```json
{
  "schema_version": 1,
  "questions": {
    "billing": {"type": "noul", "instructions": "Is this message about billing?"}
  }
}
```

A raw API question mapping without the wrapper still works. See [Question files](#question-files).

Classify one JSON record from stdin or `--input record.json`:

```sh
printf '%s\n' '{"id":"ticket-1","state":"Charged twice"}' |
  python3 jev.py classify --questions questions.json
```

Batch JSONL from stdin, one independent POST per line:

```sh
python3 jev.py batch --questions examples/failure-patterns.json < records.jsonl > results.jsonl
```

Each input is an object with `id` and `state`. IDs are any JSON value and pass through unchanged. State accepts a string, object, or array. Optional `assets` passes through the documented base64 image asset mapping. Other extra record fields are ignored. Blank lines are errors.

Each output is `{ "id": <input id>, ...<full API response> }`. It includes `model`, `answers`, `usage`, all probabilities, confidence, and future response fields. No rounding or thresholding. Output order follows input order. Duplicate IDs remain duplicates.

Each request gets `--timeout 60` seconds. Batch keeps up to `--concurrency` requests in flight (default 1, max 32) and still writes results strictly in input order. It buffers at most that many finished results while it waits for an earlier line.

HTTP 429, HTTP 5xx, connection failures, timeouts, and an upstream error body containing exactly `Endpoint is unavailable` are retried up to `--retries` times per request (default 3, max 10). The wait is capped exponential backoff with full jitter, starting at 0.5 seconds and capped at 30. A `Retry-After` header in seconds or as an HTTP date replaces the backoff for that wait, up to the same 30-second cap. HTTP 401, 403, and 422 are never retried, nor are other 4xx statuses, invalid records, or invalid responses. Out-of-range `--retries` or `--concurrency` exits 1 before any request is sent. `classify` uses the same retry policy.

On the first invalid record or exhausted API failure, exit 1; previous successful JSONL lines remain valid and flushed. Stderr names the first line without output, for example `jev: batch stopped at line 2; System One HTTP 503; gave up after 4 attempts`. It never includes HTTP bodies. An invalid line is never sent, and nothing after it is read. With concurrency above 1, requests for up to `--concurrency - 1` later lines may already be running when an earlier line fails. Those requests finish or time out, pending retries stop, and their results are discarded, so resuming from the reported line sends them again, unless `--cache` is on and they already stored an entry.

Ctrl-C works the same way. It exits 130 with `batch stopped at line N; interrupted`, stops new requests and pending retries, and waits for already-running requests to finish or reach their timeout. Resume from line N explicitly, or rerun the full input with `--cache` so finished records send nothing. CLI argument errors exit 2. Help and successful runs exit 0.

### Dry run

Price a run before sending it. `--dry-run` works with `classify` and `batch`. It validates questions, backend, and every record exactly as a real run does, then prints one JSON report to stdout. It sends no request and reads no TypeSafe key or Keychain entry:

```sh
python3 jev.py batch --dry-run --questions questions.json \
  --base-url https://api.typesafe.ai/v1 --model jev-1.14.0 < records.jsonl
```

The report names the backend endpoint and model and lists each record's `id`, its exact `request_bytes`, and estimated input, question, and state tokens. Batch rows also carry the input `line`. It totals the input estimates and gives `estimated_cost_usd` as a decimal string. A batch with an invalid line exits 1 with the usual `batch stopped at line N` error and prints no report.

Token counts are estimates, not tokenizer counts. The rule is `ceil(bytes / 4)` over the exact UTF-8 request body a send would POST, built by the same serializer. That body is ASCII-escaped JSON, so each non-ASCII character counts as its `\uXXXX` escape (6 bytes, 12 for emoji). It includes the model, state, questions, and JSON syntax. The rule is deterministic. Real tokenizers can count differently.

Prices were read 2026-11-14 as input USD per million tokens. The source pages show `$` without a currency code, so USD is assumed:

- OpenCode Zen ([docs](https://opencode.ai/docs/zen)) lists `jev-1.13-free` at 0 and `jev-1.13` at 0.042.
- TypeSafe ([models](https://docs.typesafe.ai/models)) lists `jev-1.14.0`, `jev-1.13.0`, `jev-latest`, and `jev-preview` at 0.042, for the HTTPS origin only.

`--dry-run` ignores `--cache`: it reads and writes no entry and prices every record as if sent, so the estimate is an upper bound for a cached rerun. Output tokens are not estimated. Both sources list them as free. Any other host or model reports price status `unknown` and a `null` price and cost, never a guess. Image `assets` count toward `request_bytes` but are excluded from token estimates. Such records are flagged `assets_not_estimated`, and the run's cost is `null`.

## Cache responses

Caching is off by default. Pass `--cache DIR` to `classify` or `batch` to reuse successful responses:

```sh
python3 jev.py batch --questions questions.json --cache .private/jev-cache < records.jsonl > results.jsonl
```

A rerun with the same cache sends no request for records it already answered. After an interrupted batch, rerun the whole input with the same cache; only records without an entry send requests. Output is identical to an uncached run, with each record's current `id` attached to the stored response.

- Identity. The key is SHA-256 over the format version, the full endpoint, and the exact request body: model, state, questions, and assets. A change to any of them misses. Record IDs, other record fields, credentials, and `--timeout` are not part of the key, so renamed records still hit. Question and object key order is part of the body, so reordering a mapping misses.
- Format. `DIR/v1/<first two hex>/<sha256>.json` holds `{"version": 1, "key": <sha256>, "response": <full API response>}`. A future format bumps the version directory and ignores old entries.
- What is stored. Only responses that pass the CLI's response validation. HTTP errors, connection failures, invalid JSON, and invalid response objects never write an entry. A corrupt, truncated, mismatched, or invalid entry counts as a miss and is overwritten after a successful request.
- Writes. Each entry is written to a temp file in the same directory, fsynced, and renamed into place, so readers never see a partial entry. Concurrent writers for the same key each write a complete valid entry and the last rename wins. Runs do not lock or coordinate, so two concurrent runs can both send the same uncached record. If an entry cannot be written, stderr shows `jev: warning: could not write cache entry` and the result is still printed.
- Private data. Entries hold full API responses, not states, questions, or credentials, but responses can still reveal private content. Entry files are created owner-only (mode 0600). Keep the cache under an ignored path such as `.private/` and never commit it.
- Retries and concurrency. A cached record sends nothing and is never retried. A record is stored only after a request succeeds, so a failure that exhausts `--retries` writes nothing, and a later run sends it again. Within one run at `--concurrency` above 1, records with identical request bodies that are in flight at the same time can each send a request, because each misses before either stores.
- Bypass. Omit `--cache` to send every request. Delete `DIR`, or one entry file, to force a refetch. There is no expiry.

## Question files

`--questions` reads one UTF-8 JSON file in either form:

- Version 1 document. An object with exactly two keys. `schema_version` is the integer `1`. `questions` is the API question mapping.
- Legacy raw mapping. The API question mapping itself, as in [examples/failure-patterns.json](examples/failure-patterns.json).

A top-level `schema_version` key marks a versioned document, so it cannot name a legacy question. [examples/failure-patterns.v1.json](examples/failure-patterns.v1.json) is the versioned equivalent of the legacy example. Both send identical requests.

Before any HTTP request, the CLI exits 1 on unreadable or invalid JSON, nonfinite numbers, an unknown or non-integer `schema_version`, missing or extra wrapper keys, an empty or non-object mapping, or an unsupported question type. Errors name the rule that failed and never echo file contents.

Only the `questions` mapping is sent. `schema_version` describes the file format. It is never sent and is not part of a question set's identity. Wrapping a legacy file in version 1 changes nothing the API sees, and any change to question content changes every request. The `--cache` key uses the unwrapped mapping, never the file path or `schema_version`, so a version-1 file and its legacy equivalent share cache entries.

## Summarize results

Summarize saved batch output offline. It reads no credentials and makes no network calls:

```sh
python3 jev.py summarize --question failure_pattern --questions examples/failure-patterns.json < results.jsonl
```

Input is the JSONL that `classify` or `batch` wrote, from stdin or `--input results.jsonl`. The source file is only read. `--question` names the Choice answer to count. It is optional only when the input holds exactly one question name, or when `--questions` holds exactly one question. Results with several question names need one summary per name. `--questions` is optional here. When given, it accepts either question-file form and is validated as for `classify`, and its `criteria` order seeds every label with a zero count. Labels not in the criteria follow in first-seen order.

The output is one JSON object:

- `records` is the denominator: every input line. Each line counts once in `counts`, and once in either a confidence bin, `missing`, `malformed`, or `out_of_range`.
- `counts` maps each label to its record count. `examples` lists the first three IDs per label in input order, unchanged.
- `confidence.bins` holds ten fixed bins, `0.0-0.1` through `0.9-1.0`. Each includes its lower edge and excludes its upper edge, except `0.9-1.0`, which includes 1. A confidence of 0 lands in `0.0-0.1`. These are the same edges as `eval`'s calibration buckets.
- `missing` counts answers with no `confidence`. A non-numeric or boolean value counts as `malformed`. A number below 0 or above 1 counts as `out_of_range`. Each malformed or out-of-range value gets a line-numbered entry in `diagnostics`, also printed to stderr, and the exit stays 0.

Summarize sends no requests, so `--cache`, `--dry-run`, and `--labels` exit 1 instead of being ignored. Empty input with a selected question prints `records: 0`, zero counts for any seeded labels, and zero bins. Invalid JSON, a line without `id` and an `answers` object, or a selected answer that is not a Choice stops with exit 1 and the line number, printing no summary. The summary never rewrites results, so full probability maps stay in the saved JSONL. Score, Noul, and Box answers are not summarized: Score levels and Noul probabilities have no label to count, and Box answers hold coordinates. Read those from the saved results with your own code.

## Question contracts

Question objects in the mapping pass through to the API, which validates their detailed schema:

- `choice` uses `criteria` as an object mapping option names to descriptions. Answers include `choice`, `confidence`, and the complete `probabilities` map.
- `score` uses `criteria` as an ordered array of level descriptions, indexed from zero. Answers include `score`, `legend`, `confidence`, and `probabilities`.
- `noul` asks a yes/no question through `instructions`, with optional `criteria.true` and `criteria.false`. Answers contain `noul`, the probability of yes.

Instructions and descriptions can be strings, objects, or arrays where documented. Free OpenCode and Jev 1.13 reject Box questions and image assets locally. With a supporting backend and model, Box questions pass through with optional record `assets` containing image `type`, `format`, and `data_base64`. The CLI automatically sends `TypeSafe-Danger-Zone-Images: true` for assets or Box questions. Box answers retain coordinates and confidence. Multipart, long-context headers, and packaging are outside this version.

Contracts checked 2026-11-14 against [models](https://docs.typesafe.ai/models), [System One](https://docs.typesafe.ai/api/system-one), [Choice](https://docs.typesafe.ai/primitives/choice), [Score](https://docs.typesafe.ai/primitives/score), [Noul](https://docs.typesafe.ai/primitives/noul), [Box](https://docs.typesafe.ai/primitives/box), and [OpenAPI](https://api.typesafe.ai/openapi.json). Jev's documented default limits are 64k tokens per request and 32k per question. Keep transcripts bounded.

## Evaluate against hand labels

`eval` scores saved batch results for one Choice question against hand labels. It runs offline: no requests, no credential reads, and its input files are only read.

```sh
python3 jev.py eval --questions examples/eval/questions.json \
  --labels examples/eval/labels.jsonl < examples/eval/results.jsonl
```

`--questions` is the same file used for `batch`, in either [question file](#question-files) form. `--question` names the Choice question and may be omitted when the mapping holds one question. Its `criteria` keys are the label set and fix the order of rows and columns. Predictions on stdin are `batch` output lines. Each needs `id` and an `answers` object; eval reads `answers.<question>.choice` and `confidence`.

The labels file is JSONL with one object per line, `{"id": <same JSON value as the record id>, "label": "<criteria key>"}`. IDs join by exact JSON value, so `1` and `"1"` differ. Labels are human judgments written before looking at predictions. Predicted labels are never ground truth, so do not copy model output into this file.

Output is one JSON object:

- `counts.labeled` is the number of hand labels. `counts.scored` is the accuracy and confusion denominator.
- `counts.unscored.missing_prediction` counts labeled IDs with no prediction line. `counts.unscored.unknown_choice` counts predictions whose choice is missing, not a string, or not a criteria key.
- `counts.extra_predictions` counts prediction IDs with no label. They are ignored.
- `correct` and `accuracy` are over scored rows. `accuracy` is `null` when nothing was scored.
- `confusion[label][choice]` counts scored rows by hand label, then predicted choice.
- `calibration` has ten fixed buckets `[0.0, 0.1)` through `[0.9, 1.0]`, each low-inclusive, with `1.0` in the top bucket. Each holds `count`, `mean_confidence`, and empirical `accuracy`, both `null` when empty. Only scored rows with a numeric confidence in `[0, 1]` enter a bucket. `counts.calibrated` and `counts.missing_confidence` split scored rows by that rule.

Read calibration bucket by bucket. When `accuracy` sits near `mean_confidence`, the stated confidence matches observed correctness. Accuracy below mean confidence means overconfidence. Small counts make a bucket noise; compare buckets only with enough labeled examples.

Duplicate IDs in either file, a label outside the criteria keys, an empty labels file, malformed or blank lines, and a missing `--labels` exit 1 with no report. Errors name the file and line. Empty predictions report zero scored rows. Eval sends no requests, so `--cache` and `--dry-run` are rejected with eval.

## Run the session demo

Prepare the eight most recently modified local session files with usable messages, excluding this repository's sessions:

```sh
python3 scripts/demo_sessions.py
```

After reviewing the saved sample and obtaining authorization to send it to the selected backend (OpenCode by default):

```sh
python3 scripts/demo_sessions.py --send
```

`--send` reuses the exact existing `.private/demo/sample.jsonl` without resampling or rewriting it, and pipes it to `jev.py batch` as UTF-8 whatever the locale. It caches successful responses in `.private/demo/cache`, so a rerun sends only missing records. Cache keys cover the redacted outgoing request, so a change to the redaction policy or `--no-redact` misses earlier entries. Missing samples fail before transmission. `--limit` must be between 1 and 20; an existing sample larger than the limit is rejected without truncation. Running without `--send` prepares and replaces the sample.

The script retains trailing user and assistant messages up to 24,000 characters, caps each message at 4,000 characters, and omits tools and reasoning. It writes the redacted inputs, API results, and the `jev.py summarize` counts, example IDs, and confidence bins by category beneath ignored `.private/demo/`. Do not commit those files.

### Redaction

Redaction is on by default, under policy `jev-demo-redact-1`. Each message is redacted whole, before the 4,000-character cap, the 24,000-character budget, and the write to `sample.jsonl`, so a cut cannot leave part of a match behind. `--send` leaves the saved sample unchanged and redacts the outgoing copy again, which also protects samples prepared before redaction existed. Every string value in a record is redacted, nested objects and arrays included. Record IDs, object keys, non-string values, and structure stay unchanged. The rules run in this order, on Python `re` syntax:

- `private_key`: `-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----` through the matching `END` line, or to the end of the text, becomes `[REDACTED:private_key]`.
- `bearer`: `bearer` (any case) and whitespace, then 8 or more of `[A-Za-z0-9._~+/=-]`. The value becomes `[REDACTED:bearer]`.
- `token`: `sk-` + 16 or more `[A-Za-z0-9_-]`, `gh[pousr]_` + 20 or more alphanumerics, `github_pat_` + 20 or more, `xox[abprs]-` + 10 or more, `AKIA` + 16 `[0-9A-Z]`, `AIza` + 35, and three-part `eyJ...` JWTs. Each becomes `[REDACTED:token]`.
- `assignment`: `api_key`, `api-key`, `apikey`, `secret`, `token`, `password`, `passwd`, or `access_key` (any case), then `:` or `=`, optional quotes, and 8 or more characters other than whitespace, quotes, `,`, or `;`. The value becomes `[REDACTED:secret]`.
- `email`: `local@domain.tld` addresses become `[REDACTED:email]`.
- `home_path`: `/Users/<name>`, `/home/<name>`, and `X:\Users\<name>` (either slash) become `[REDACTED:home]` when they do not follow a word character, `.`, `:`, or a slash. The rest of the path stays.

The summary holds only `redaction.policy` and per-rule `redaction.counts`, never matched text. Prepare counts are the complete placeholders in the saved excerpts, so a match past the 4,000-character cap, or a placeholder the cap cuts, is not counted. Send counts cover the second pass, so they are zero for a sample this version prepared.

Limitations. This pattern list does not detect every secret. It misses short values, unprefixed tokens, tokens glued to a preceding letter or digit, secrets without a `key=` label, names, phone numbers, addresses, and paths outside home directories. It also hits some harmless text, for example `git@github.com` reads as an email. Review the saved sample before sending, and remove anything sensitive the rules missed. Placeholders can hide the context a classifier needs.

To opt out, pass `--no-redact` to both the prepare and the send run. The sample is then saved and sent as extracted, the summary reports policy `disabled`, and the manual review is the only protection. The four Choice labels are heuristic, mutually exclusive judgments over excerpts; `fine` also includes insufficient evidence. Counts are not an accuracy evaluation or a full-session audit. To measure accuracy, hand-label a subset and run [eval](#evaluate-against-hand-labels).

The agent workflow lives in [SKILL.md](SKILL.md). Local demo status lives in [docs/demo.md](docs/demo.md).

## Verify

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q jev.py scripts tests
python3 jev.py --help
git diff --check
```

Tests mock HTTP at the CLI boundary, checking request shape/auth, complete responses, failures, retries and backoff, the concurrency bound, partial batch output, and credential selection. Eval tests check the example fixture's exact metrics through a fresh process, edge cases, and that eval sends nothing and reads no credentials. No live API credential is required for tests. Summarize tests run the CLI on synthetic result files.

Two agent skills under `.claude/skills/` (mirrored in `.agents/skills/`) go further, both stdlib only. `verify-jev` drives a fresh copy of the CLI and the session demo against a loopback System One stub, with an opt-in `--live` journey of three synthetic OpenCode requests. `formal-verify-jev` exhaustively checks request shape, credential isolation, probability passthrough, batch invariants, question-file versioning, and demo redaction over bounded inputs, and proves each check can fail on a broken `jev.py` or demo script:

```sh
python3 .claude/skills/verify-jev/verify.py run
python3 .claude/skills/formal-verify-jev/check.py --mutants
```

Evidence lands in ignored `.private/verify/`.
