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

Requests run sequentially, with `--timeout 60` seconds per request. No automatic retries. On the first invalid record or API failure, exit 1; previous successful JSONL lines remain valid and flushed. Batch stderr identifies the failed line; it omits private HTTP bodies. Resume from that line explicitly after resolving the error, or rerun the full input with `--cache` so finished records send nothing. CLI argument errors exit 2. Help and successful runs exit 0.

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
- Bypass. Omit `--cache` to send every request. Delete `DIR`, or one entry file, to force a refetch. There is no expiry.

## Question files

`--questions` reads one UTF-8 JSON file in either form:

- Version 1 document. An object with exactly two keys. `schema_version` is the integer `1`. `questions` is the API question mapping.
- Legacy raw mapping. The API question mapping itself, as in [examples/failure-patterns.json](examples/failure-patterns.json).

A top-level `schema_version` key marks a versioned document, so it cannot name a legacy question. [examples/failure-patterns.v1.json](examples/failure-patterns.v1.json) is the versioned equivalent of the legacy example. Both send identical requests.

Before any HTTP request, the CLI exits 1 on unreadable or invalid JSON, nonfinite numbers, an unknown or non-integer `schema_version`, missing or extra wrapper keys, an empty or non-object mapping, or an unsupported question type. Errors name the rule that failed and never echo file contents.

Only the `questions` mapping is sent. `schema_version` describes the file format. It is never sent and is not part of a question set's identity. Wrapping a legacy file in version 1 changes nothing the API sees, and any change to question content changes every request. The `--cache` key uses the unwrapped mapping, never the file path or `schema_version`, so a version-1 file and its legacy equivalent share cache entries.

## Question contracts

Question objects in the mapping pass through to the API, which validates their detailed schema:

- `choice` uses `criteria` as an object mapping option names to descriptions. Answers include `choice`, `confidence`, and the complete `probabilities` map.
- `score` uses `criteria` as an ordered array of level descriptions, indexed from zero. Answers include `score`, `legend`, `confidence`, and `probabilities`.
- `noul` asks a yes/no question through `instructions`, with optional `criteria.true` and `criteria.false`. Answers contain `noul`, the probability of yes.

Instructions and descriptions can be strings, objects, or arrays where documented. Free OpenCode and Jev 1.13 reject Box questions and image assets locally. With a supporting backend and model, Box questions pass through with optional record `assets` containing image `type`, `format`, and `data_base64`. The CLI automatically sends `TypeSafe-Danger-Zone-Images: true` for assets or Box questions. Box answers retain coordinates and confidence. Multipart, long-context headers, retry policies, concurrency, and packaging are outside this version.

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

Duplicate IDs in either file, a label outside the criteria keys, an empty labels file, malformed or blank lines, and a missing `--labels` exit 1 with no report. Errors name the file and line. Empty predictions report zero scored rows. Eval sends no requests, so `--cache` is rejected with eval.

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

The script retains trailing user and assistant messages up to 24,000 characters, caps each message at 4,000 characters, and omits tools and reasoning. It writes the redacted inputs, API results, and the counts and example IDs by category beneath ignored `.private/demo/`. Do not commit those files.

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

Tests mock HTTP at the CLI boundary, checking request shape/auth, complete responses, failures, partial batch output, and credential selection. Eval tests check the example fixture's exact metrics through a fresh process, edge cases, and that eval sends nothing and reads no credentials. No live API credential is required for tests.

Two agent skills under `.claude/skills/` (mirrored in `.agents/skills/`) go further, both stdlib only. `verify-jev` drives a fresh copy of the CLI and the session demo against a loopback System One stub, with an opt-in `--live` journey of three synthetic OpenCode requests. `formal-verify-jev` exhaustively checks request shape, credential isolation, probability passthrough, batch invariants, question-file versioning, and demo redaction over bounded inputs, and proves each check can fail on a broken `jev.py` or demo script:

```sh
python3 .claude/skills/verify-jev/verify.py run
python3 .claude/skills/formal-verify-jev/check.py --mutants
```

Evidence lands in ignored `.private/verify/`.
