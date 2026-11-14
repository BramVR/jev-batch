# Changelog

## Unreleased

- Add opt-in `--cache DIR` that stores validated responses keyed by endpoint and exact request body, so reruns and resumed batches send only missing records; the session demo caches under `.private/demo/cache`.
- Redact secrets, bearer and API tokens, emails, and Unix/Windows home paths from demo samples before caching and again before sending, on by default with `--no-redact` to opt out; summaries report only policy and counts.

- Accept version-1 question documents (`schema_version` plus `questions`) alongside legacy raw mappings, and reject unknown versions, empty mappings, and malformed question files before any request with non-private errors.

- Reuse the approved private session sample for demo sends, cap samples at 20, and report category counts and example IDs; verify eight real OpenCode classifications without committing excerpts.

- Default to free OpenCode Jev 1.13 with configurable base URL and model, isolate TypeSafe credentials to its HTTPS origin, and reject unsupported Box requests locally.

- Add a dependency-free Python Jev CLI for single records and streaming JSONL batches, preserving IDs and complete API responses with environment or macOS Keychain authentication.
- Add a classify-many agent skill, failure-pattern question mapping, and private session demo; document the missing-key blocker without fabricated classifications.

- Support Box questions with base64 image assets and the required images header.
