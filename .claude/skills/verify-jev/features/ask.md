# Ask one question

`ask noul|choice|score QUESTION` asks one question about plain text with no JSON files. It builds the question `{"answer": {...}}` and the record `{"id": null, "state": TEXT}` in memory and sends them the way `classify` would, so it prints the same JSON line. `--value` prints only `answers.answer.<type>`.

## Sub-features

- `ask-like-classify` `printf 'Charged twice' | python3 jev.py ask noul "Is this about billing?" --value` posts exactly the body, byte count, and headers that `classify --questions ask-noul.json` posts for `{"id":null,"state":"Charged twice"}`, and no `Authorization` off TypeSafe.
- `ask-value` `--value` prints the stub's noul (`0.8125`), the choice key bare (`billing`), and exits 0.
- `ask-choice` repeated `-o KEY=DESCRIPTION` become `criteria` in the given order. Without `-o`, exit 2.
- `ask-score` repeated `-l DESCRIPTION` become the `criteria` list in order. Without `--value`, the output is classify's JSON line with `"id": null`.
- `ask-state` `--state TEXT`, `--state-file PATH`, or raw stdin, sent unchanged: the `--state-file` step keeps CRLF. Both flags together exit 2. Empty stdin exits 1 with `jev: state is empty` and sends nothing.
- `ask-flag-scope` `-o` on `ask noul` exits 2. Each type takes only its gate flags: `--expect` on `ask noul` exits 2.
- `ask-dry-run` `--dry-run --value` prints the dry-run report, whose `request_bytes` equals the body the send step posts, and sends nothing.
- `ask-gate` noul 0.2 under `--fail-under 0.5` prints the `--value` and exits 10.

## How to get to it (user POV)

- `printf 'Charged twice' | python3 jev.py ask noul "Is this about billing?" --value`
- `python3 jev.py ask choice "Pick the topic." -o billing=Payments -o bug=Defects --state "I was charged twice"`
- `python3 jev.py ask score "How urgent?" -l low -l high --state-file ticket.txt`

## Driving it with verify.py

Preconditions:

- Doctor passes. The stub answers each step with a reply under the question name `answer`.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run ask`. Every step prints `PASS`.
- **Request proof.** In the transcript, `stub saw` for the first two steps shows the same body and `bytes`, one from `ask`, one from `classify` with `ask-noul.json`.
- **Output proof.** The first step's stdout is `0.8125` and a newline, nothing else.

## Gotchas

- The question name is always `answer`. A stub reply under any other name makes `--value` exit 1.
- `state.txt` holds CRLF line ends on purpose. Do not normalize the fixture.
