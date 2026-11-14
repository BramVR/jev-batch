# Response cache

`--cache DIR` on `classify` or `batch` reuses successful responses. A rerun sends nothing for records already answered, and prints each record's current `id` on the stored response.

## Sub-features

- `cache-rerun` a second batch with the same states and new IDs sends zero requests and prints complete answers with the new IDs.
- `cache-miss` a different model, backend, question mapping, or assets sends a request.
- `cache-question-forms` a version-1 question file hits the entry its equivalent legacy mapping stored, because only the unwrapped mapping is sent.
- `cache-resume` after a batch stops at line N, a rerun of the full input sends only lines N onward.
- `cache-no-failure` an invalid or failed response writes no entry, so the retry sends again.
- `cache-corrupt` a corrupt entry is refetched and rewritten, and the next run hits it.
- `cache-dry-run` `--dry-run` with a warm cache still prices every record and sends nothing, the warm cache keeps serving afterward, and a dry run on a cold cache leaves no entry, so the next real run misses.
- `cache-demo` `demo_sessions.py --send` caches under `.private/demo/cache`; rerunning the same send makes no request, and no entry holds a redacted fixture value.

## How to get to it (user POV)

- `python3 jev.py batch --questions q.json --cache .private/jev-cache < records.jsonl > results.jsonl`
- `python3 scripts/demo_sessions.py --send` uses `.private/demo/cache`.

## Driving it with verify.py

Preconditions:

- Doctor passes. Steps run in order and share cache dirs `cache-a`, `cache-b`, `cache-c` under the run's `work/`, so run the whole journey, not one step.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run cache`. Every step prints `PASS`.
- **Zero-request proof.** The `rerun with new ids` step has no `stub saw` block, and its stdout lines equal the stub body with ids `a2` and `{"n": 2}`.
- **Question-file forms.** `version-1 file hits the legacy file's entry` runs `noul.v1.json` after `question change misses` stored `noul.json`, and has no `stub saw` block.
- **Resume proof.** `interrupted batch keeps the prefix` shows three stub requests and stderr naming line 3. The next step's `stub saw` holds only states `r3` and `r4`.
- **Corruption.** The `before` hook of `corrupt entry refetches cleanly` truncates every entry in `cache-c`. The `retry after failure` hook fails the step if the failed response left any file.
- **Demo cache.** The `demo-redaction` journey's `rerun send reuses the demo cache, no request` step repeats the UTF-8 send with no stub reply queued, so any request would get the stub's 500. Its files check reads every entry under the build's `.private/demo/cache`.
- **Exhaustive cover.** Hit identity over 90 request configs, every failure kind, eleven corruptions, and resume over short line sequences live in the `formal-verify-jev` skill.

## Gotchas

- Reordering keys in the questions file is a miss by design. The key is the exact request body.
- Concurrent writers and file modes are not driven here. The README documents the last-rename-wins behavior and owner-only entries.
- Demo cache entries persist across `demo-redaction` steps in one build. Each earlier send uses a different outgoing body, so none of them hits the cache.
