---
name: jev-classify
description: "Classify many records with Jev."
---

# Classify with Jev

Use the repository's stdlib `jev.py` for judgment over records. Read [README.md](README.md) for CLI contracts and supported question types.

For classify-many work, script the loop. Extract records to JSONL `{id,state}`, write one raw API question mapping, then pipe records through `python3 jev.py batch --questions questions.json`. Keep related questions in the same mapping. Each record is one request. Preserve IDs and full responses; aggregate counts with code after saving results. Do not replace probability maps with just winning labels.

Use Choice for categories, Score for ordered levels, and Noul for yes/no probabilities. Verify unfamiliar fields against the linked TypeSafe docs before writing questions. The CLI supports text only; Box and assets require a different integration.

Use `TYPESAFE_API_KEY` or macOS Keychain service `typesafe`. Never put secrets in records, question files, command arguments, or commits. Private source data needs authorization before API transmission. This skill grants none.

Keep source excerpts and results under ignored `.private/`. On failure, preserve successful output, inspect the reported line, and resume deliberately. The CLI performs no retries. Report sample selection, omitted context, counts, IDs, and uncertainty; never present predicted labels as verified failures.
