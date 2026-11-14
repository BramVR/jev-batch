# jev-cli

Python 3.9+ stdlib CLI for TypeSafe's `POST https://api.typesafe.ai/v1/systemone`. No installation or dependencies. Default model `jev-1.14.0` is pinned for repeatable model selection; `--model jev-latest` follows releases.

## Classify records

Set `TYPESAFE_API_KEY` in your environment, or store a generic password under macOS Keychain service `typesafe`. The environment takes precedence. The CLI never reads credential files or accepts a key argument.

Write a raw API question mapping to `questions.json`:

```json
{
  "billing": {"type": "noul", "instructions": "Is this message about billing?"}
}
```

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

Requests run sequentially, with `--timeout 60` seconds per request. No automatic retries. On the first invalid record or API failure, exit 1; previous successful JSONL lines remain valid and flushed. Batch stderr identifies the failed line; it omits private HTTP bodies. Resume from that line explicitly after resolving the error. CLI argument errors exit 2. Help and successful runs exit 0.

## Question contracts

The file passed to `--questions` is the questions mapping itself, without a `questions` wrapper. Text question objects pass through to the API, which validates their detailed schema:

- `choice` uses `criteria` as an object mapping option names to descriptions. Answers include `choice`, `confidence`, and the complete `probabilities` map.
- `score` uses `criteria` as an ordered array of level descriptions, indexed from zero. Answers include `score`, `legend`, `confidence`, and `probabilities`.
- `noul` asks a yes/no question through `instructions`, with optional `criteria.true` and `criteria.false`. Answers contain `noul`, the probability of yes.

Instructions and descriptions can be strings, objects, or arrays where documented. Box questions pass through with optional record `assets` containing image `type`, `format`, and `data_base64`. The CLI automatically sends `TypeSafe-Danger-Zone-Images: true` for assets or Box questions. Box answers retain coordinates and confidence. Multipart, long-context headers, retry policies, concurrency, and packaging are outside this version.

Contracts checked 2026-11-14 against [models](https://docs.typesafe.ai/models), [System One](https://docs.typesafe.ai/api/system-one), [Choice](https://docs.typesafe.ai/primitives/choice), [Score](https://docs.typesafe.ai/primitives/score), [Noul](https://docs.typesafe.ai/primitives/noul), [Box](https://docs.typesafe.ai/primitives/box), and [OpenAPI](https://api.typesafe.ai/openapi.json). Jev's documented default limits are 64k tokens per request and 32k per question. Keep transcripts bounded.

## Run the session demo

Prepare the eight most recently modified local session files with usable messages, excluding this repository's sessions:

```sh
python3 scripts/demo_sessions.py
```

After authorization to send the excerpts to TypeSafe:

```sh
python3 scripts/demo_sessions.py --send
```

The script retains trailing user and assistant messages up to 24,000 characters, caps each message at 4,000 characters, and omits tools and reasoning. It writes raw inputs, API results, and the count/session-ID summary beneath ignored `.private/demo/`. Do not commit those files. Review sample excerpts for secrets before sending. The four Choice labels are heuristic, mutually exclusive judgments over excerpts; `fine` also includes insufficient evidence. Counts are not an accuracy evaluation or a full-session audit.

The agent workflow lives in [SKILL.md](SKILL.md). Local demo status lives in [docs/demo.md](docs/demo.md).

## Verify

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q jev.py scripts tests
python3 jev.py --help
git diff --check
```

Tests mock HTTP at the CLI boundary, checking request shape/auth, complete responses, failures, partial batch output, and credential selection. No live API credential is required for tests.
