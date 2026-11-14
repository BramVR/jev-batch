---
read_when: Checking the approved eight-session demo or reproducing its aggregate results.
---

# Session demo, 2026-11-14

Ran `env -u JEV_BASE_URL -u JEV_MODEL python3 scripts/demo_sessions.py --send` against OpenCode, with model `jev-1.13-free` and no Authorization header. The user approved this sample's transmission. The command reused the eight existing records without resampling. Sample SHA-256 before and after was `2ef337d641fdcaa88d6159af90b27852ff1e502cd9dd57d329691b3108becd26`.

The first invocation exited 1 with the exact diagnostic `jev: batch stopped at line 1; System One HTTP 403; no retry performed`. It classified zero records. Synthetic requests isolated the client User-Agent difference: Python's default received HTTP 403, while `jev-cli/0.1` received HTTP 200. After adding the client header, a new invocation completed all eight records. No automatic retries.

Exact successful stdout:

```json
{
  "sample_count": 8,
  "session_ids": [
    "01a1d6b1-1fe7-7281-98f1-161956d47479",
    "01a1d6c9-b4f4-77e2-a94f-62bb3b85b4f9",
    "01a1d6f5-b8f2-7d21-8f44-a41081f74156",
    "01a1d6b1-aa6e-71b0-8f84-7e70364ac885",
    "01a1d67e-77d9-7ce0-9694-ec627ce2d87d",
    "01a158aa-8835-74e2-933e-45e5d1c2787f",
    "01a1d59a-efb2-7831-ab95-f3b26a4dee9c",
    "019ff045-3d4c-73f0-a269-ae5d08be826f"
  ],
  "classification_counts": {
    "ignored_agents": 1,
    "repeated_correction": 0,
    "loop_or_gave_up": 1,
    "fine": 6
  },
  "example_ids_by_category": {
    "ignored_agents": [
      "01a1d6b1-aa6e-71b0-8f84-7e70364ac885"
    ],
    "repeated_correction": [],
    "loop_or_gave_up": [
      "019ff045-3d4c-73f0-a269-ae5d08be826f"
    ],
    "fine": [
      "01a1d6b1-1fe7-7281-98f1-161956d47479",
      "01a1d6c9-b4f4-77e2-a94f-62bb3b85b4f9",
      "01a1d6f5-b8f2-7d21-8f44-a41081f74156"
    ]
  },
  "unclassified": 0,
  "api_exit_code": 0,
  "diagnostic": ""
}
```

All eight responses reported `jev-1.13-free`. Checked all eight probability maps for the four expected categories, finite values between 0 and 1, and sums within 0.000001 of 1. All eight `usage.cost` values were 0. Full responses, probabilities, and raw excerpts remain ignored under `.private/demo/`; this report contains no source text.

The original sample used recent file modification times, deduplicated session IDs, excluded this repository, and retained trailing user and assistant excerpts. Each message was capped at 4,000 characters and each excerpt at 24,000. Tools, reasoning, and earlier context were omitted. Modification order need not match session start order. These are model judgments over a convenience sample, not verified failures or an accuracy evaluation. `fine` also includes insufficient evidence.
