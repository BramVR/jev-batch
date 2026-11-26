# jev-batch

jev-batch classifies many records with Jev System One, then scores and counts the results offline.

Python 3.9+ stdlib CLI for Jev System One, with no runtime dependencies. Defaults to free OpenCode `POST https://opencode.ai/zen/v1/systemone`, model `jev-1.13-free`, without authentication.

## Install

The installed command is `jev-batch`. There is no `jev` command, so it does not clash with other Jev CLIs.

```sh
uv tool install jev-batch
pipx install jev-batch
```

Run it straight from git, before or without a PyPI release of jev-batch. The build still fetches setuptools from your package index:

```sh
uvx --from git+https://github.com/BramVR/jev-batch jev-batch --version
```

A checkout needs no install. The examples below run `python3 jev.py`, which is the same CLI; with an install, type `jev-batch` instead.

## Quick start

Run this in a directory that holds `jev.py`. It uses the free default and needs no key.

```sh
cat > questions.json <<'EOF'
{
  "schema_version": 1,
  "questions": {
    "topic": {
      "type": "choice",
      "instructions": "Pick the topic of the message.",
      "criteria": {"billing": "Payments and charges.", "bug": "Software defect.", "other": "Anything else."}
    }
  }
}
EOF
printf '%s\n' '{"id":"ticket-1","state":"I was charged twice this month."}' |
  python3 jev.py classify --questions questions.json
```

It prints one JSON line with the record `id` and the full System One response.

## What only jev-batch does

Compared with [tumf/jev-cli](https://github.com/tumf/jev-cli), [shaharia-lab/jev-cli](https://github.com/shaharia-lab/jev-cli), and [nasrallah-al/jev-cli](https://github.com/nasrallah-al/jev-cli), checked on 2026-11-25:

- The default backend is free and needs no key. All three default to TypeSafe and need a key.
- `eval` scores saved results against hand labels with a confusion matrix and calibration buckets.
- `summarize` counts labels offline.
- It is one stdlib Python file with no runtime dependency. tumf needs the `mcp` package and Python 3.13.

Unofficial. Not affiliated with TypeSafe.

License: [MIT](LICENSE).

## Classify records

Use `--base-url` or `JEV_BASE_URL` to select the API base URL and `--model` or `JEV_MODEL` to select the model. Flags override environment values, which override defaults. The CLI appends `/systemone` after removing trailing slashes. The base URL must be printable ASCII with no spaces or control characters, so write an internationalized host in punycode. Any other base URL exits 1 before credentials are read, in a dry run too.

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

Flags follow the command, and each command accepts only its own: `python3 jev.py <command> --help` lists them. A flag the command does not take, such as `--cache` on `eval` or `--concurrency` on `classify`, exits 2 as a usage error before anything is read or sent. Flags are never abbreviated. Stdin, stdout, and stderr are UTF-8 whatever the locale or `PYTHONIOENCODING` says.

Each input is an object with `id` and `state`. IDs are any JSON value and pass through unchanged. State accepts a string, object, or array. Optional `assets` passes through the documented base64 image asset mapping. Other extra record fields are ignored. Blank lines are errors.

Each output is `{ "id": <input id>, ...<full API response> }`. It includes `model`, `answers`, `usage`, all probabilities, confidence, and future response fields. No rounding or thresholding. Output order follows input order. Duplicate IDs remain duplicates.

Each request gets `--timeout 60` seconds. Batch keeps up to `--concurrency` requests in flight (default 1, max 32) and still writes results strictly in input order. It buffers at most that many finished results while it waits for an earlier line.

Each request is retried up to `--retries` times (default 3, max 10) after HTTP 429 or 5xx, a connection error or timeout, or an 'Endpoint is unavailable' error body on a status other than 401/403/422. The body must contain that exact phrase, so a 400 or 404 carrying it is retried, and the same status without it is not. HTTP 401, 403, and 422 are never retried, whatever the body says. Other 4xx statuses without the phrase, invalid records, and invalid responses are not retried either. The CLI reads at most 4 KiB of an error body to look for the phrase and never prints it. The wait is capped exponential backoff with full jitter, starting at 0.5 seconds and capped at 30. A `Retry-After` header in seconds or as an HTTP date replaces the backoff for that wait, up to the same 30-second cap. Out-of-range `--retries` or `--concurrency` exits 1 before any request is sent; `--dry-run` ignores in-range values. `classify` uses the same retry policy.

On the first invalid record or exhausted API failure, exit 1; previous successful JSONL lines remain valid and flushed. Stderr names the first line without output, for example `jev: batch stopped at line 2; System One HTTP 503; gave up after 4 attempts`. It never includes HTTP bodies. `invalid JSON record` means the input line itself failed to parse, while a response that is not UTF-8 JSON with finite numbers reads `System One returned invalid JSON`. An invalid line is never sent, and nothing after it is read. With concurrency above 1, requests for up to `--concurrency - 1` later lines may already be running when an earlier line fails. Those requests finish or time out, pending retries stop, and their results are discarded, so resuming from the reported line sends them again, unless `--cache` is on and they already stored an entry.

Ctrl-C works the same way. It exits 130 with `batch stopped at line N; interrupted`, stops new requests and pending retries, and waits for already-running requests to finish or reach their timeout. Resume from line N explicitly, or rerun the full input with `--cache` so finished records send nothing. [Exit codes](#exit-codes) lists every code.

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

### Gate on an answer

`classify` can turn one answer into an exit code, so a script branches without parsing JSON:

```sh
printf '%s\n' '{"id":"ticket-1","state":"Charged twice"}' |
  python3 jev.py classify --questions questions.json --fail-under 0.5 --abstain-band 0.4,0.6
```

- `--question NAME` picks the question to gate. It may be omitted when the mapping holds one question.
- `--fail-under P` gates a Noul answer: `noul < P` exits 10, so `noul` equal to `P` passes.
- `--expect KEY` gates a Choice answer: any winner other than `KEY` exits 10. `KEY` must be a `criteria` key.
- `--min-confidence C` gates a Choice or Score answer: `confidence < C` exits 10.
- `--abstain-band LO,HI` gates a Noul answer: `LO <= noul <= HI` exits 11. Both ends are included, and abstain wins over every other gate flag.

Flags combine; the gate passes, with exit 0, only when no flag fails. Values must be numbers in [0, 1], and the band needs `LO <= HI`. Anything else exits 2 as a usage error. A gate flag that does not fit the question type, an `--expect` key outside `criteria`, an unknown `--question`, or gate flags on a mapping of several questions without `--question` exit 1 before credentials are read or any request is sent, in a dry run too. The full response is always printed before the gate exit. An answer without the field a flag reads, such as a missing or boolean `noul`, is printed and then exits 1. A chunked record is gated on its aggregated answer. `--dry-run` validates the gate flags, evaluates no gate, and exits 0. `batch` takes no gate flags.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success; with a classify or ask gate, the gate passed |
| 1 | error: bad input or questions file, missing credentials, failed request, or an unreadable gated or --value answer |
| 2 | usage error: unknown flag, missing required flag, or a flag value out of range |
| 10 | classify or ask gate false: --fail-under, --expect, or --min-confidence failed |
| 11 | classify or ask abstain: noul inside --abstain-band |
| 130 | interrupted by Ctrl-C |

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

## Chunk long text

Jev's documented default limits are 32k tokens per question and 64k per request. Without `--chunk-tokens`, the CLI sends state unchanged and performs no context check. Opt in to chunking for long string state:

```sh
python3 jev.py batch --questions questions.json --chunk-tokens 30000 < records.jsonl
```

`N` is the estimated token budget for one request, at most 32000, including question overhead. The state capacity per chunk is `N - 512 - utf8_bytes(questions JSON)`, measured on the unwrapped mapping that is sent, so a version-1 file and its legacy mapping chunk identically. The 512-token allowance for the model name, request framing, and server-side prompt is a guess, not a documented value. Every question shares each request, so a budget under 32k also keeps requests under 64k. A budget that leaves fewer than 4 bytes for state is rejected.

Tokens are estimated as UTF-8 bytes. That is an upper bound only for byte-level BPE tokenizers, where each token spans at least one byte. Jev's tokenizer is not documented, so the bound is an assumption. English text uses roughly a quarter of its estimate, which makes chunks smaller and more numerous than necessary.

State whose estimate fits the capacity, including empty state, keeps the existing single-request behavior and output. Longer state is split into consecutive chunks with no overlap at code-point boundaries, so a chunk boundary can split a word or sentence. Each chunk is one request with the same questions. The final record is:

```json
{"id": "<input id>",
 "answers": {"route": {"type": "choice", "choice": "b", "confidence": 0.5625, "probabilities": {"a": 0.4375, "b": 0.5625}}},
 "aggregation": {"method": "mean", "weights": "equal", "tie_break": "criteria order", "confidence": "mean probability of chosen label"},
 "chunking": {"token_budget": 30000, "state_capacity": 29425, "estimator": "utf8-bytes", "overlap": 0, "count": 2},
 "chunks": [{"index": 0, "start": 0, "end": 29425, "estimated_tokens": 29425, "response": {"...": "full API response"}}]}
```

Each label's aggregate probability is the equal-weight mean of that label's probabilities across chunks, computed exactly with `fractions.Fraction` and rounded to a float once. The choice is the label with the highest exact mean, so near-equal labels are not decided by float rounding. Exact ties go to the label listed first in the question's `criteria`. Aggregate `confidence` is the mean probability of the chosen label. It is not the mean of per-chunk API `confidence` values. `start` and `end` are character offsets into the original state. Every chunk's full API response, including `model` and `usage`, stays in `chunks` for audit.

The mean is a heuristic. A label strongly present in one chunk and absent from the rest is diluted, and chunks are judged without each other's context. Inspect `chunks` before trusting an aggregate.

The first slice supports only Choice questions with `criteria` objects and string state without `assets`. Other question types, object or array state, and out-of-range budgets exit 1 before any request. Text is never truncated. If any chunk request fails, returns invalid JSON, or answers without probabilities for exactly the criteria labels, the record fails with the chunk number. No later chunk is sent and no aggregate is printed. Each chunk request gets the same `--retries` policy as any request, and the chunk number names only the attempt that finally failed. Earlier chunk requests for that record were already sent and billed. Batch stops at that line as usual. In batch, `--concurrency` counts records: the chunks of one record are sent one after another, so requests in flight never exceed `--concurrency`.

With `--cache`, each chunk request is cached on its own, keyed like any other request. A rerun after a failed chunk sends only the chunks that never succeeded, and an identical rerun sends nothing. Identical chunk text within a record hits the same entry. A chunk answer rejected for its labels is never cached, so the rerun asks again.

`--dry-run --chunk-tokens N` prices the chunked requests: a row for a chunked record carries `chunks` and the sums of its chunk requests' bytes and token estimates, so question tokens count once per chunk. Records that fit keep the plain row. `summarize` reads chunked output unchanged, because each record's aggregated `answers` keep the Choice shape.

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

Empty input with a selected question prints `records: 0`, zero counts for any seeded labels, and zero bins. Invalid JSON, a line without `id` and an `answers` object, or a selected answer that is not a Choice stops with exit 1 and the line number, printing no summary. The summary never rewrites results, so full probability maps stay in the saved JSONL. Score, Noul, and Box answers are not summarized: Score levels and Noul probabilities have no label to count, and Box answers hold coordinates. Read those from the saved results with your own code.

## Question contracts

Question objects in the mapping pass through to the API, which validates their detailed schema:

- `choice` uses `criteria` as an object mapping option names to descriptions. Answers include `choice`, `confidence`, and the complete `probabilities` map.
- `score` uses `criteria` as an ordered array of level descriptions, indexed from zero. Answers include `score`, `legend`, `confidence`, and `probabilities`.
- `noul` asks a yes/no question through `instructions`, with optional `criteria.true` and `criteria.false`. Answers contain `noul`, the probability of yes.

Instructions and descriptions can be strings, objects, or arrays where documented. Free OpenCode and Jev 1.13 reject Box questions and image assets locally. With a supporting backend and model, Box questions pass through with optional record `assets` containing image `type`, `format`, and `data_base64`. The CLI automatically sends `TypeSafe-Danger-Zone-Images: true` for assets or Box questions. Box answers retain coordinates and confidence. Multipart, long-context headers, and chunking beyond Choice text are outside this version.

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

Duplicate IDs in either file, a label outside the criteria keys, an empty labels file, and malformed or blank lines exit 1 with no report. Errors name the file and line. Empty predictions report zero scored rows.

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
python3 scripts/lint.py
python3 -m unittest discover -s tests -v
python3 -m compileall -q jev.py scripts tests
python3 jev.py --help
python3 jev.py batch --help
git diff --check
python3 scripts/check_install.py
```

`check_install.py` installs the package into a fresh venv and checks that `jev-batch --version` and `jev-batch classify --help` work, that no `jev` command exists, and that `pip show` lists no requirements. It needs network access to fetch setuptools.

Tests mock HTTP at the CLI boundary, checking request shape/auth, complete responses, failures, retries and backoff, the concurrency bound, partial batch output, and credential selection. Eval tests check the example fixture's exact metrics through a fresh process, edge cases, and that eval sends nothing and reads no credentials. No live API credential is required for tests. Summarize tests run the CLI on synthetic result files.

Two agent skills under `.claude/skills/` (mirrored in `.agents/skills/`) go further, both stdlib only. `verify-jev` drives a fresh copy of the CLI and the session demo against a loopback System One stub, with an opt-in `--live` journey of six synthetic OpenCode requests. `formal-verify-jev` exhaustively checks request shape, credential isolation, probability passthrough, retry policy and backoff, batch order, window, and concurrency invariants, question-file versioning, response cache identity and resume, classify gate exit codes, and demo redaction over bounded inputs, and proves each check can fail on a broken `jev.py` or demo script:

```sh
python3 .claude/skills/verify-jev/verify.py run
python3 .claude/skills/formal-verify-jev/check.py --mutants
```

Evidence lands in ignored `.private/verify/`.
