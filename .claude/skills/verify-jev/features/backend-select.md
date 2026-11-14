# Backend selection

A user picks the backend with `--base-url`/`JEV_BASE_URL` and `--model`/`JEV_MODEL`. Flags beat env, which beats the OpenCode default. Box questions and image assets need an image-capable model, and TypeSafe credentials go only to TypeSafe. The same host and model pick the `--dry-run` price, and a dry run reads no key even on TypeSafe; see [Dry run](./dry-run.md).

## Sub-features

- `backend-env` env vars choose endpoint path and model.
- `backend-flag` flags override env.
- `box-free-reject` a Box question on the free `jev-1.13-free` model exits 1 before any request.
- `box-image-model` with `--model jev-1.14.0`, assets pass through and `TypeSafe-Danger-Zone-Images: true` is sent.
- `url-credentials-reject` a base URL with userinfo exits 1.

## How to get to it (user POV)

- `JEV_BASE_URL=... JEV_MODEL=... python3 jev.py classify --questions q.json`
- `python3 jev.py classify --questions box.json --model jev-1.14.0 --base-url ...`

## Driving it with verify.py

Preconditions:

- Doctor passes.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run backend-select`. Every step prints `PASS`.
- **Box proof.** The `Box with image model` step's `stub saw` shows `assets` in the body and the images header.
- **Gating proof.** The `Box on free model` step shows exit 1 and no stub requests.

## Gotchas

- The stub URL is loopback HTTP, so Box support is decided by the model name alone there. On `opencode.ai` the host also forces rejection.
- Real TypeSafe auth is not driven here; a send needs a real key. A TypeSafe `--dry-run` needs none and is driven in the `dry-run` journey. Credential isolation over many URL shapes is checked in `formal-verify-jev`.
- urllib sends header names capitalized like `Typesafe-danger-zone-images`; the stub records them lowercased.
