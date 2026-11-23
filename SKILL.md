---
name: jev-classify
description: "Classify many records with Jev."
---

# Classify with Jev

Use the repository's stdlib `jev.py` for judgment over records. Read [README.md](README.md) for CLI contracts and supported question types.

For classify-many work, script the loop. Extract records to JSONL `{id,state}`, write one version-1 question document (`{"schema_version": 1, "questions": {...}}`), then pipe records through `python3 jev.py batch --questions questions.json`. Keep related questions in the same mapping. Each record is one request. Preserve IDs and full responses. After saving results, count Choice labels, example IDs, and confidence bins with `python3 jev.py summarize --question NAME < results.jsonl`; aggregate Score, Noul, and Box with your own code. Do not replace probability maps with just winning labels.

Use Choice for categories, Score for ordered levels, and Noul for yes/no probabilities. Verify unfamiliar fields against the linked TypeSafe docs before writing questions. Free OpenCode Jev 1.13 supports text only. Box and assets require a supporting backend and model, such as TypeSafe Jev 1.14.

Default to OpenCode `https://opencode.ai/zen/v1` and `jev-1.13-free`, without authentication. Override with `--base-url`/`JEV_BASE_URL` and `--model`/`JEV_MODEL`; flags win. To select TypeSafe, use `--base-url https://api.typesafe.ai/v1 --model jev-1.14.0` and `TYPESAFE_API_KEY` or macOS Keychain service `typesafe`. TypeSafe credentials are read only for its exact HTTPS origin. Never put secrets in records, question files, command arguments, or commits. Private source data needs authorization before API transmission. This skill grants none.

Keep source excerpts and results under ignored `.private/`. The demo `--send` reuses the saved sample without resampling; prepare and review it separately. Demo redaction is on by default and also runs on the outgoing copy, but it is a fixed pattern list, not complete secret detection; still review the sample. `--no-redact` opts out. Demo `--limit` is capped at 20. On failure, preserve successful output, inspect the reported line, and resume deliberately. Pass `--cache .private/<dir>` on long runs so a rerun bills only records without a cached success. The CLI retries each request up to `--retries` times (default 3) after HTTP 429 or 5xx, a connection error or timeout, or an 'Endpoint is unavailable' error body on a status other than 401/403/422. Raise `--concurrency` (default 1, max 32) only when the backend's rate limit allows; output order stays the input order. Report sample selection, omitted context, counts, IDs, and uncertainty; never present predicted labels as verified failures. To measure accuracy, hand-label a sample to `{id,label}` JSONL and run `python3 jev.py eval --questions questions.json --labels labels.jsonl < results.jsonl`; README documents the report.
