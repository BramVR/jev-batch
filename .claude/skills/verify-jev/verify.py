#!/usr/bin/env python3
"""Drive a fresh copy of jev.py as a user would and keep the evidence. Stdlib only."""

import argparse
from dataclasses import dataclass, field
import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / ".private" / "verify"
SECRET_BODY = "stub-private-body-must-not-leak"
SENTINEL_KEY = "sentinel-not-a-real-key"

NOUL = {"billing": {"type": "noul", "instructions": "Is this message about billing?"}}
CHOICE = {"topic": {"type": "choice", "instructions": "Pick the topic of the message.",
                    "criteria": {"billing": "Payments, charges, invoices.", "bug": "Software defect.",
                                 "other": "Anything else."}}}
BOX = {"logo": {"type": "box", "instructions": "Find the logo."}}
ASSETS = {"shot": {"type": "image", "format": "png", "data_base64": "iVBORw0KGgo="}}
EVAL_LABELS = "".join(json.dumps({"id": i, "label": v}) + "\n" for i, v in
                      (("e1", "billing"), ("e2", "bug"), ("e3", "other"), ("e4", "bug")))
EVAL_RESULTS = "".join(json.dumps({"id": i, "answers": {"topic": {"type": "choice", "choice": c, **conf}}}) + "\n"
                       for i, c, conf in (("e1", "billing", {"confidence": 1.0}), ("e2", "billing", {"confidence": 0.5}),
                                          ("e3", "other", {}), ("e5", "bug", {"confidence": 0.9})))
def choice_result(id_, choice, **answer):
    return json.dumps({"id": id_, "model": "stub-model", "answers": {"topic": {
        "type": "choice", "choice": choice, **answer, "probabilities": {"billing": 0.2, "bug": 0.5, "other": 0.3}}}},
        ensure_ascii=False)


RESULTS = "".join(line + "\n" for line in [
    choice_result("r-1", "bug", confidence=0), choice_result("r-2", "bug", confidence=1),
    choice_result("r-3", "bug", confidence=0.1), choice_result("r-4", "bug", confidence=0.95),
    choice_result("r-5", "billing"), choice_result("r-6", "billing", confidence="high"),
    choice_result("r-7\u2028", "billing", confidence=1.5)])
STUB_RESPONSE = ('{"model":"stub-model","answers":{"topic":{"type":"choice","choice":"bug",'
                 '"confidence":0.30000000000000004,"probabilities":{"billing":0.1,"bug":0.30000000000000004,'
                 '"other":0.6}}},"usage":{"input_tokens":12,"output_tokens":3},"future_field":[1e-7,-0.0]}')
DEMO_RESPONSE = '{"model":"stub-model","answers":{"failure_pattern":{"type":"choice","choice":"fine","probabilities":{"fine":1.0}}}}'
# Synthetic look-alikes planted in a fake ~/.codex session; none may reach the stub unless --no-redact.
DEMO_SECRETS = ["synthBearerTokenValue0123456789", "sk-synthetic0000aaaa1111bbbb2222", "synthPasswordValue42",
                "fixture.person@example.test", "fixtureunixuser", "FixtureWinUser"]
DEMO_TEXT = (f"curl -H 'Authorization: Bearer {DEMO_SECRETS[0]}' key {DEMO_SECRETS[1]} password={DEMO_SECRETS[2]} "
             f"mail {DEMO_SECRETS[3]} at /Users/{DEMO_SECRETS[4]}/notes and C:\\Users\\{DEMO_SECRETS[5]}\\Desktop")
DEMO_PLACEHOLDERS = ["[REDACTED:bearer]", "[REDACTED:token]", "[REDACTED:secret]", "[REDACTED:email]", "[REDACTED:home]"]


@dataclass
class Step:
    name: str
    argv: list
    stdin: str = ""
    env: dict = field(default_factory=dict)
    stub: list = field(default_factory=list)
    code: int = 0
    stdout: object = None
    stderr: str = ""
    requests: object = None
    timeout: float = 30
    script: str = "jev.py"
    setup: object = None
    files: object = None
    before: object = None


# Stub System One: a real HTTP server on 127.0.0.1 reached through the user-facing --base-url.

class Stub:
    def __init__(self):
        self.queue, self.seen = [], []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                stub.seen.append({"method": "POST", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                  "body": json.loads(raw), "bytes": len(raw)})
                status, body, delay = stub.queue.pop(0) if stub.queue else (500, SECRET_BODY, 0)
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body.encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def ok(body=STUB_RESPONSE):
    return (200, body, 0)


def lines_json(out):
    # Split on "\n" only: jev writes ensure_ascii=False, so U+2028 inside a JSON string must not end a line.
    return [json.loads(x) for x in out.split("\n") if x]


def paths(reqs):
    return [r["path"] for r in reqs]


def clean(text):
    return not any(secret in text for secret in DEMO_SECRETS)


def sample(build_dir):
    return (build_dir / ".private/demo/sample.jsonl").read_text()


def write_unicode_sample(build_dir):
    path = build_dir / ".private/demo/sample.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": "é-1", "state": "naïve café ✓"}, ensure_ascii=False) + "\n", encoding="utf-8")


def demo_cache_clean(build_dir):
    entries = [p.read_text(encoding="utf-8") for p in (build_dir / ".private/demo/cache").rglob("*.json")]
    return bool(entries) and all(clean(e) for e in entries)


def write_old_sample(build_dir):
    path = build_dir / ".private/demo/sample.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": "old-1", "state": {"messages": [{"role": "user", "text": DEMO_TEXT}]}}) + "\n")


def corrupt_cache(work):
    for entry in (work / "cache-c").rglob("*.json"):
        entry.write_text('{"version": 1, "key": "trunc')


def no_cache_entries(name):
    return lambda work: not any(p.is_file() for p in (work / name).rglob("*"))


UNICODE_RECORD = '{"id":"d-7","state":"日本語🙂 refund ünï"}'
UNICODE_BYTES = len(json.dumps({"model": "jev-1.13-free", "state": "日本語🙂 refund ünï", "questions": NOUL}).encode())


def report(out, status, cost=None, rows=None):
    """One dry-run JSON report: labelled estimate, pricing status, totals equal the rows, cost as stated."""
    lines = out.splitlines()
    if len(lines) != 1:
        return False
    r = json.loads(lines[0])
    records = r["records"]
    price = r["pricing"]["input_usd_per_million_tokens"]
    total = r["total_input_tokens_estimate"]
    got = r["estimated_cost_usd"]
    cost_ok = Decimal(got) == Decimal(price) * total / 1000000 if cost == "computed" else got == cost
    return (r["dry_run"] is True and "not a tokenizer count" in r["token_method"]
            and r["pricing"]["date"] and r["pricing"]["currency"] == "USD" and r["pricing"]["status"] == status
            and total == sum(x["input_tokens_estimate"] for x in records)
            and all(x["request_bytes"] >= 4 * x["input_tokens_estimate"] - 3 for x in records)
            and (rows is None or [(x.get("line"), x["id"]) for x in records] == rows) and cost_ok)


def journeys(stub_url):
    base = stub_url + "/zen/v1/"
    stub_args = ["--base-url", base]
    response = json.loads(STUB_RESPONSE)
    return {
        "cli-surface": [
            Step("help", ["--help"], stdout=lambda o: "classify" in o and "summarize" in o and "--question " in o
                 and "--questions" in o and "--dry-run" in o),
            Step("missing questions is a usage error", ["classify"], code=2, stderr="--questions"),
            Step("nonfinite timeout rejected", ["classify", "--questions", "noul.json", "--timeout", "inf"],
                 code=1, stderr="jev: timeout must be positive and finite"),
            Step("batch refuses --input", ["batch", "--questions", "noul.json", "--input", "record.json"],
                 code=1, stderr="jev: batch reads JSONL from stdin"),
            Step("eval refuses --input", ["eval", "--questions", "choice.json", "--labels", "x", "--input", "record.json"],
                 code=1, stderr="jev: eval reads JSONL from stdin"),
            Step("empty question mapping rejected", ["classify", "--questions", "empty.json"],
                 '{"id":1,"state":"x"}', code=1, stderr="jev: questions must be a nonempty"),
            Step("unknown question type rejected", ["classify", "--questions", "weird.json"],
                 '{"id":1,"state":"x"}', code=1, stderr="jev: supported question types"),
            Step("dry run gets the same question guard", ["classify", "--dry-run", "--questions", "empty.json"],
                 '{"id":1,"state":"x"}', code=1, stderr="jev: questions must be a nonempty", stdout=lambda o: o == ""),
        ],
        "question-files": [
            Step("legacy mapping sends its questions", ["classify", "--questions", "noul.json", *stub_args],
                 '{"id":1,"state":"x"}', stub=[ok()],
                 requests=lambda r: len(r) == 1 and r[0]["body"]["questions"] == NOUL),
            Step("version-1 document sends identical questions, no wrapper",
                 ["classify", "--questions", "noul.v1.json", *stub_args], '{"id":1,"state":"x"}', stub=[ok()],
                 requests=lambda r: len(r) == 1 and r[0]["body"]["questions"] == NOUL
                 and sorted(r[0]["body"]) == ["model", "questions", "state"]),
            Step("unknown schema_version rejected before HTTP", ["classify", "--questions", "v2.json", *stub_args],
                 '{"id":1,"state":"x"}', code=1, stderr="jev: unsupported questions schema_version; supported: 1",
                 requests=lambda r: not r),
            Step("extra wrapper key rejected without echo", ["classify", "--questions", "extra.v1.json", *stub_args],
                 '{"id":1,"state":"x"}', code=1, stderr="jev: versioned questions document must contain exactly",
                 requests=lambda r: not r),
            Step("empty version-1 mapping rejected", ["batch", "--questions", "empty.v1.json", *stub_args],
                 '{"id":1,"state":"x"}\n', code=1, stderr="jev: questions must be a nonempty",
                 requests=lambda r: not r),
            Step("malformed JSON rejected before HTTP", ["classify", "--questions", "broken.json", *stub_args],
                 '{"id":1,"state":"x"}', code=1, stderr="jev: questions file must be readable UTF-8 JSON",
                 requests=lambda r: not r),
        ],
        "classify": [
            Step("stdin record, full response verbatim, no auth off TypeSafe",
                 ["classify", "--questions", "choice.json", *stub_args], '{"id":"t-1","state":"Charged twice"}',
                 env={"TYPESAFE_API_KEY": SENTINEL_KEY}, stub=[ok()],
                 stdout=lambda o: o == '{"id": "t-1", ' + json.dumps(response)[1:] + "\n",
                 requests=lambda r: paths(r) == ["/zen/v1/systemone"]
                 and "authorization" not in r[0]["headers"]
                 and r[0]["headers"].get("user-agent") == "jev-cli/0.1"
                 and r[0]["body"] == {"model": "jev-1.13-free", "state": "Charged twice", "questions": CHOICE}),
            Step("--input file record", ["classify", "--questions", "choice.json", "--input", "record.json", *stub_args],
                 stub=[ok()], stdout=lambda o: lines_json(o)[0]["id"] == {"ticket": 7},
                 requests=lambda r: r[0]["body"]["state"] == {"subject": "App crashes on save"}),
            Step("HTTP 503 fails once, no retry, no body echo", ["classify", "--questions", "choice.json", *stub_args],
                 '{"id":1,"state":"x"}', stub=[(503, SECRET_BODY, 0)], code=1,
                 stderr="jev: System One HTTP 503; no retry performed", requests=lambda r: len(r) == 1),
            Step("timeout fails once", ["classify", "--questions", "choice.json", "--timeout", "0.5", *stub_args],
                 '{"id":1,"state":"x"}', stub=[(200, STUB_RESPONSE, 2)], code=1,
                 stderr="jev: System One connection failed or timed out", requests=lambda r: len(r) == 1),
        ],
        "batch": [
            Step("three lines in order, duplicate ids kept", ["batch", "--questions", "choice.json", *stub_args],
                 '{"id":"a","state":"one"}\n{"id":"a","state":"two"}\n{"id":"c\u2028","state":["three"]}\n',
                 stub=[ok(), ok(), ok()],
                 stdout=lambda o: [x["id"] for x in lines_json(o)] == ["a", "a", "c\u2028"]
                 and all(x["answers"] == response["answers"] for x in lines_json(o)),
                 requests=lambda r: [x["body"]["state"] for x in r] == ["one", "two", ["three"]]),
            Step("API failure on line 2 keeps line 1, stops", ["batch", "--questions", "choice.json", *stub_args],
                 '{"id":1,"state":"a"}\n{"id":2,"state":"b"}\n{"id":3,"state":"c"}\n',
                 stub=[ok(), (500, SECRET_BODY, 0), ok()], code=1,
                 stderr="jev: batch stopped at line 2; System One HTTP 500",
                 stdout=lambda o: [x["id"] for x in lines_json(o)] == [1], requests=lambda r: len(r) == 2),
            Step("invalid line 2 stops before sending it", ["batch", "--questions", "choice.json", *stub_args],
                 '{"id":1,"state":"a"}\n{not json\n{"id":3,"state":"c"}\n', stub=[ok(), ok()], code=1,
                 stderr="jev: batch stopped at line 2; invalid JSON record",
                 stdout=lambda o: len(lines_json(o)) == 1, requests=lambda r: len(r) == 1),
            Step("blank line is an error", ["batch", "--questions", "choice.json", *stub_args],
                 '{"id":1,"state":"a"}\n\n', stub=[ok()], code=1, stderr="jev: batch stopped at line 2",
                 requests=lambda r: len(r) == 1),
        ],
        "cache": [
            Step("first run with --cache sends every record", ["batch", "--questions", "choice.json", "--cache", "cache-a", *stub_args],
                 '{"id":"a","state":"one"}\n{"id":"b","state":["two"]}\n', stub=[ok(), ok()],
                 stdout=lambda o: [x["id"] for x in lines_json(o)] == ["a", "b"], requests=lambda r: len(r) == 2),
            Step("rerun with new ids sends zero requests, full answers, current ids",
                 ["batch", "--questions", "choice.json", "--cache", "cache-a", *stub_args],
                 '{"id":"a2","state":"one"}\n{"id":{"n":2},"state":["two"]}\n', stub=[ok(), ok()],
                 stdout=lambda o: lines_json(o) == [{"id": "a2", **response}, {"id": {"n": 2}, **response}],
                 requests=lambda r: r == []),
            Step("model change misses", ["classify", "--questions", "choice.json", "--cache", "cache-a",
                                         "--model", "other-model", *stub_args],
                 '{"id":"a","state":"one"}', stub=[ok()], requests=lambda r: len(r) == 1),
            Step("backend change misses", ["classify", "--questions", "choice.json", "--cache", "cache-a",
                                           "--base-url", stub_url + "/other/"],
                 '{"id":"a","state":"one"}', stub=[ok()], requests=lambda r: paths(r) == ["/other/systemone"]),
            Step("question change misses", ["classify", "--questions", "noul.json", "--cache", "cache-a", *stub_args],
                 '{"id":"a","state":"one"}', stub=[ok()], requests=lambda r: len(r) == 1),
            Step("version-1 file hits the legacy file's entry",
                 ["classify", "--questions", "noul.v1.json", "--cache", "cache-a", *stub_args],
                 '{"id":"v1","state":"one"}', stub=[ok()],
                 stdout=lambda o: lines_json(o) == [{"id": "v1", **response}], requests=lambda r: r == []),
            Step("assets change misses", ["classify", "--questions", "choice.json", "--cache", "cache-a",
                                          "--model", "jev-1.14.0", *stub_args],
                 json.dumps({"id": "a", "state": "one", "assets": ASSETS}), stub=[ok()], requests=lambda r: len(r) == 1),
            Step("interrupted batch keeps the prefix", ["batch", "--questions", "choice.json", "--cache", "cache-b", *stub_args],
                 '{"id":1,"state":"r1"}\n{"id":2,"state":"r2"}\n{"id":3,"state":"r3"}\n{"id":4,"state":"r4"}\n',
                 stub=[ok(), ok(), (503, SECRET_BODY, 0)], code=1, stderr="jev: batch stopped at line 3",
                 stdout=lambda o: [x["id"] for x in lines_json(o)] == [1, 2], requests=lambda r: len(r) == 3),
            Step("rerun after interrupt sends only missing records",
                 ["batch", "--questions", "choice.json", "--cache", "cache-b", *stub_args],
                 '{"id":1,"state":"r1"}\n{"id":2,"state":"r2"}\n{"id":3,"state":"r3"}\n{"id":4,"state":"r4"}\n',
                 stub=[ok(), ok()], stdout=lambda o: [x["id"] for x in lines_json(o)] == [1, 2, 3, 4],
                 requests=lambda r: [x["body"]["state"] for x in r] == ["r3", "r4"]),
            Step("failed response is not cached", ["classify", "--questions", "choice.json", "--cache", "cache-c", *stub_args],
                 '{"id":1,"state":"x"}', stub=[(200, '{"model":"m"}', 0)], code=1,
                 stderr="jev: System One returned an invalid response object", requests=lambda r: len(r) == 1),
            Step("retry after failure sends again", ["classify", "--questions", "choice.json", "--cache", "cache-c", *stub_args],
                 '{"id":1,"state":"x"}', stub=[ok()], before=no_cache_entries("cache-c"),
                 stdout=lambda o: lines_json(o) == [{"id": 1, **response}], requests=lambda r: len(r) == 1),
            Step("corrupt entry refetches cleanly", ["classify", "--questions", "choice.json", "--cache", "cache-c", *stub_args],
                 '{"id":1,"state":"x"}', stub=[ok()], before=corrupt_cache,
                 stdout=lambda o: lines_json(o) == [{"id": 1, **response}], requests=lambda r: len(r) == 1),
            Step("refetched entry is reused", ["classify", "--questions", "choice.json", "--cache", "cache-c", *stub_args],
                 '{"id":1,"state":"x"}', stub=[ok()], requests=lambda r: r == []),
            Step("dry run ignores a warm cache: every record priced, nothing sent",
                 ["batch", "--dry-run", "--questions", "choice.json", "--cache", "cache-a", *stub_args],
                 '{"id":"a","state":"one"}\n{"id":"b","state":["two"]}\n', stub=[ok(), ok()],
                 stdout=lambda o: report(o, "unknown", None, [(1, "a"), (2, "b")]), requests=lambda r: r == []),
            Step("warm cache still serves after the dry run",
                 ["batch", "--questions", "choice.json", "--cache", "cache-a", *stub_args],
                 '{"id":"a","state":"one"}\n{"id":"b","state":["two"]}\n', stub=[ok(), ok()],
                 stdout=lambda o: lines_json(o) == [{"id": "a", **response}, {"id": "b", **response}],
                 requests=lambda r: r == []),
            Step("dry run on a cold cache writes nothing",
                 ["classify", "--dry-run", "--questions", "choice.json", "--cache", "cache-d", *stub_args],
                 '{"id":1,"state":"x"}', stub=[ok()], stdout=lambda o: report(o, "unknown", None), requests=lambda r: r == []),
            Step("so the next real run misses and sends", ["classify", "--questions", "choice.json", "--cache", "cache-d", *stub_args],
                 '{"id":1,"state":"x"}', stub=[ok()], before=no_cache_entries("cache-d"), requests=lambda r: len(r) == 1),
        ],
        "eval": [
            Step("scores fixture exactly, sends nothing", ["eval", "--questions", "choice.json", "--labels", "labels.jsonl"],
                 EVAL_RESULTS, env={"JEV_BASE_URL": base, "TYPESAFE_API_KEY": SENTINEL_KEY}, stub=[ok()],
                 stdout=lambda o: eval_ok(json.loads(o)), requests=lambda r: r == []),
            Step("version-1 questions document scores identically",
                 ["eval", "--questions", "choice.v1.json", "--labels", "labels.jsonl"], EVAL_RESULTS,
                 stdout=lambda o: eval_ok(json.loads(o))),
            Step("unknown schema_version rejected before scoring", ["eval", "--questions", "v2.json", "--labels",
                                                                    "labels.jsonl"], EVAL_RESULTS, code=1,
                 stderr="jev: unsupported questions schema_version", stdout=lambda o: o == ""),
            Step("duplicate label id rejected", ["eval", "--questions", "choice.json", "--labels", "dup-labels.jsonl"],
                 EVAL_RESULTS, code=1, stderr="jev: labels line 2: duplicate id", stdout=lambda o: o == ""),
            Step("label outside criteria rejected", ["eval", "--questions", "choice.json", "--labels", "bad-labels.jsonl"],
                 "", code=1, stderr="jev: labels line 1: label must be one of the topic options"),
            Step("duplicate prediction id rejected", ["eval", "--questions", "choice.json", "--labels", "labels.jsonl"],
                 EVAL_RESULTS + EVAL_RESULTS.splitlines(True)[0], code=1, stderr="jev: predictions line 5: duplicate id"),
            Step("eval requires --labels", ["eval", "--questions", "choice.json"], code=1, stderr="jev: eval requires --labels"),
            Step("non-Choice question rejected", ["eval", "--questions", "noul.json", "--labels", "labels.jsonl"],
                 code=1, stderr="jev: eval needs --question naming a Choice question"),
            Step("--cache rejected with eval", ["eval", "--questions", "choice.json", "--labels", "labels.jsonl",
                                                 "--cache", "eval-cache"], EVAL_RESULTS, code=1,
                 stderr="jev: eval sends no requests; --cache is for classify and batch", stdout=lambda o: o == ""),
            Step("--dry-run rejected with eval", ["eval", "--questions", "choice.json", "--labels", "labels.jsonl",
                                                   "--dry-run"], EVAL_RESULTS, code=1,
                 stderr="jev: eval sends no requests; --dry-run is for classify and batch", stdout=lambda o: o == ""),
            Step("--labels is eval-only", ["batch", "--questions", "choice.json", "--labels", "labels.jsonl", *stub_args],
                 '{"id":1,"state":"x"}', stub=[ok()], code=1, stderr="jev: --labels is for eval; --question is for eval and summarize",
                 requests=lambda r: r == []),
        ],
        "summarize": [
            Step("counts, first three ids, bins with 0 and 1 edges, offline",
                 ["summarize", "--questions", "choice.json", "--input", "results.jsonl", *stub_args],
                 env={"TYPESAFE_API_KEY": SENTINEL_KEY}, stub=[ok()],
                 stderr='jev: line 6: confidence "high" is not a number\njev: line 7: confidence 1.5 is outside [0, 1]\n',
                 stdout=lambda o: (lambda s: s["question"] == "topic" and s["records"] == 7
                                   and s["counts"] == {"billing": 3, "bug": 4, "other": 0}
                                   and s["examples"] == {"billing": ["r-5", "r-6", "r-7\u2028"], "bug": ["r-1", "r-2", "r-3"],
                                                         "other": []}
                                   and s["confidence"]["bins"]["0.0-0.1"] == 1 and s["confidence"]["bins"]["0.1-0.2"] == 1
                                   and s["confidence"]["bins"]["0.9-1.0"] == 2 and sum(s["confidence"]["bins"].values()) == 4
                                   and (s["confidence"]["missing"], s["confidence"]["malformed"],
                                        s["confidence"]["out_of_range"]) == (1, 1, 1))(json.loads(o)),
                 requests=lambda r: r == []),
            Step("version-1 question file seeds labels like a legacy file",
                 ["summarize", "--questions", "choice.v1.json", "--input", "results.jsonl"], stderr="jev: line 6",
                 stdout=lambda o: list(json.loads(o)["counts"]) == ["billing", "bug", "other"]
                 and json.loads(o)["counts"]["other"] == 0),
            Step("unsupported schema_version rejected before summarizing",
                 ["summarize", "--questions", "v2.json", "--input", "results.jsonl"], code=1,
                 stderr="jev: unsupported questions schema_version", stdout=lambda o: o == ""),
            Step("question inferred from input, labels seeded from multi-question file",
                 ["summarize", "--questions", "mixed.json", "--input", "results.jsonl"], stderr="jev: line 6",
                 stdout=lambda o: json.loads(o)["counts"] == {"billing": 3, "bug": 4, "other": 0}),
            Step("stdin works without --questions", ["summarize"], RESULTS,
                 stderr="jev: line 6", stdout=lambda o: json.loads(o)["counts"] == {"bug": 4, "billing": 3}),
            Step("several question names need --question", ["summarize"],
                 '{"id":1,"answers":{"a":{"type":"choice","choice":"x"},"b":{"type":"choice","choice":"y"}}}\n',
                 code=1, stderr="jev: select one question with --question; input has a, b", stdout=lambda o: o == ""),
            Step("--question selects one of several", ["summarize", "--question", "b"],
                 '{"id":1,"answers":{"a":{"type":"choice","choice":"x"},"b":{"type":"choice","choice":"y"}}}\n',
                 stdout=lambda o: json.loads(o)["counts"] == {"y": 1}),
            Step("empty input gives zero summary", ["summarize", "--questions", "choice.json"], "",
                 stdout=lambda o: (lambda s: s["records"] == 0 and s["counts"] == {"billing": 0, "bug": 0, "other": 0}
                                   and not any(s["confidence"]["bins"].values()))(json.loads(o))),
            Step("Score answer rejected with line", ["summarize"],
                 '{"id":1,"answers":{"level":{"type":"score","score":2}}}\n', code=1,
                 stderr="jev: summarize stopped at line 1; 'level' is not a Choice answer", stdout=lambda o: o == ""),
        ],
        "backend-select": [
            Step("env selects base URL and model", ["classify", "--questions", "noul.json"], '{"id":1,"state":"x"}',
                 env={"JEV_BASE_URL": stub_url + "/env", "JEV_MODEL": "env-model"}, stub=[ok()],
                 requests=lambda r: paths(r) == ["/env/systemone"] and r[0]["body"]["model"] == "env-model"),
            Step("flags beat env", ["classify", "--questions", "noul.json", "--base-url", stub_url + "/flag",
                                    "--model", "flag-model"], '{"id":1,"state":"x"}',
                 env={"JEV_BASE_URL": stub_url + "/env", "JEV_MODEL": "env-model"}, stub=[ok()],
                 requests=lambda r: paths(r) == ["/flag/systemone"] and r[0]["body"]["model"] == "flag-model"),
            Step("Box on free model rejected locally", ["classify", "--questions", "box.json", *stub_args],
                 '{"id":1,"state":"x"}', stub=[ok()], code=1, stderr="free Jev 1.13 is unsupported",
                 requests=lambda r: r == []),
            Step("Box with image model sends assets and header",
                 ["classify", "--questions", "box.json", "--model", "jev-1.14.0", *stub_args],
                 json.dumps({"id": 1, "state": "x", "assets": ASSETS}), stub=[ok()],
                 requests=lambda r: r[0]["headers"].get("typesafe-danger-zone-images") == "true"
                 and r[0]["body"]["assets"] == ASSETS),
            Step("credential-bearing URL rejected", ["classify", "--questions", "noul.json",
                                                     "--base-url", "https://u:p@api.typesafe.ai/v1"],
                 '{"id":1,"state":"x"}', code=1, stderr="jev: base URL must be an HTTP(S) URL without credentials"),
        ],
        "demo-redaction": [
            Step("prepare saves only placeholders and reports counts", [], script="scripts/demo_sessions.py",
                 stdout=lambda o: clean(o) and json.loads(o)["redaction"] == {"policy": "jev-demo-redact-1", "counts": {
                     "assignment": 1, "bearer": 1, "email": 1, "home_path": 2, "token": 1}},
                 files=lambda b: clean(sample(b)) and all(p in sample(b) for p in DEMO_PLACEHOLDERS)),
            Step("send keeps sample, stub sees no fixture value", ["--send"], script="scripts/demo_sessions.py",
                 env={"JEV_BASE_URL": base}, stub=[ok(DEMO_RESPONSE)],
                 stdout=lambda o: clean(o) and json.loads(o)["classification_counts"]["fine"] == 1
                 and json.loads(o)["redaction"]["counts"] == {},
                 requests=lambda r: len(r) == 1 and clean(json.dumps(r[0]["body"]))
                 and "[REDACTED:home]" in json.dumps(r[0]["body"]),
                 files=lambda b: clean(sample(b))),
            Step("send redacts outgoing copy of an older raw sample", ["--send"], script="scripts/demo_sessions.py",
                 env={"JEV_BASE_URL": base}, stub=[ok(DEMO_RESPONSE)], setup=write_old_sample,
                 stdout=lambda o: clean(o) and json.loads(o)["redaction"]["counts"]["bearer"] == 1,
                 requests=lambda r: len(r) == 1 and clean(json.dumps(r[0]["body"])),
                 files=lambda b: DEMO_SECRETS[0] in sample(b)),
            Step("--no-redact sends the sample as extracted", ["--send", "--no-redact"],
                 script="scripts/demo_sessions.py", env={"JEV_BASE_URL": base}, stub=[ok(DEMO_RESPONSE)],
                 stdout=lambda o: json.loads(o)["redaction"] == {"policy": "disabled", "counts": {}},
                 requests=lambda r: len(r) == 1 and DEMO_SECRETS[3] in json.dumps(r[0]["body"]),
                 setup=write_old_sample),
            Step("send is UTF-8 even when inherited stdio is ASCII", ["--send"], script="scripts/demo_sessions.py",
                 env={"JEV_BASE_URL": base, "PYTHONIOENCODING": "ascii"}, stub=[ok(DEMO_RESPONSE)],
                 setup=write_unicode_sample, stdout=lambda o: json.loads(o)["api_exit_code"] == 0,
                 requests=lambda r: len(r) == 1 and r[0]["body"]["state"] == "naïve café ✓",
                 files=lambda b: json.loads((b / ".private/demo/results.jsonl").read_text(encoding="utf-8"))["id"] == "é-1"),
            Step("rerun send reuses the demo cache, no request", ["--send"], script="scripts/demo_sessions.py",
                 env={"JEV_BASE_URL": base}, setup=write_unicode_sample,
                 stdout=lambda o: json.loads(o)["api_exit_code"] == 0 and json.loads(o)["unclassified"] == 0,
                 requests=lambda r: r == [], files=demo_cache_clean),
            Step("--limit above 20 rejected before any request", ["--send", "--limit", "21"],
                 script="scripts/demo_sessions.py", env={"JEV_BASE_URL": base}, stub=[ok(DEMO_RESPONSE)], code=2,
                 stderr="--limit must be between 1 and 20", requests=lambda r: r == []),
        ],
        "dry-run": [
            Step("free default: zero cost, per-line rows, Unicode, no key needed",
                 ["batch", "--dry-run", "--questions", "noul.json"],
                 '{"id":"d-1","state":"Charged twice"}\n{"id":"d-2","state":"日本語🙂 refund"}\n',
                 stdout=lambda o: report(o, "free", "0", [(1, "d-1"), (2, "d-2")])),
            Step("stub backend: unknown price stays null, nothing sent, key untouched",
                 ["classify", "--dry-run", "--questions", "choice.json", *stub_args], '{"id":"d-3","state":"x"}',
                 env={"TYPESAFE_API_KEY": SENTINEL_KEY}, stub=[ok()],
                 stdout=lambda o: report(o, "unknown", None, [(None, "d-3")]), requests=lambda r: r == []),
            Step("TypeSafe jev-1.14.0 without any key: priced estimate",
                 ["batch", "--dry-run", "--questions", "choice.json", "--base-url", "https://api.typesafe.ai/v1",
                  "--model", "jev-1.14.0"], '{"id":"d-4","state":"one"}\n{"id":"d-5","state":["two"]}\n',
                 stdout=lambda o: report(o, "priced", "computed", [(1, "d-4"), (2, "d-5")])),
            Step("image assets: not estimated, cost null, nothing sent",
                 ["classify", "--dry-run", "--questions", "box.json", "--model", "jev-1.14.0", *stub_args],
                 json.dumps({"id": "d-6", "state": "x", "assets": ASSETS}), stub=[ok()],
                 stdout=lambda o: report(o, "unknown", None) and json.loads(o)["records"][0]["assets_not_estimated"],
                 requests=lambda r: r == []),
            Step("Unicode dry run measures the exact bytes the send then posts (dry step)",
                 ["classify", "--dry-run", "--questions", "noul.json", *stub_args], UNICODE_RECORD,
                 stdout=lambda o: report(o, "unknown", None) and json.loads(o)["records"][0]["request_bytes"] == UNICODE_BYTES),
            Step("Unicode send posts that many bytes (send step)", ["classify", "--questions", "noul.json", *stub_args],
                 UNICODE_RECORD, stub=[ok()], requests=lambda r: r[0]["bytes"] == UNICODE_BYTES),
            Step("version-1 file prices the unwrapped mapping, like its legacy twin",
                 ["classify", "--dry-run", "--questions", "noul.v1.json"], '{"id":"d-8","state":"x"}',
                 stdout=lambda o: report(o, "free", "0") and json.loads(o)["records"][0]["request_bytes"]
                 == len(json.dumps({"model": "jev-1.13-free", "state": "x", "questions": NOUL}).encode())),
            Step("unknown schema_version rejected as a send would be", ["classify", "--dry-run", "--questions", "v2.json"],
                 '{"id":1,"state":"x"}', code=1, stderr="jev: unsupported questions schema_version", stdout=lambda o: o == ""),
            Step("invalid line 2 stops with no report", ["batch", "--dry-run", "--questions", "noul.json"],
                 '{"id":1,"state":"a"}\n{not json\n', code=1,
                 stderr="jev: batch stopped at line 2; invalid JSON record", stdout=lambda o: o == ""),
            Step("Box on free model rejected as a send would be", ["classify", "--dry-run", "--questions", "box.json"],
                 '{"id":1,"state":"x"}', code=1, stderr="free Jev 1.13 is unsupported", stdout=lambda o: o == ""),
        ],
    }


def eval_ok(report):
    filled = {b["low"]: (b["count"], b["mean_confidence"], b["accuracy"]) for b in report["calibration"] if b["count"]}
    return (report["counts"] == {"labeled": 4, "scored": 3, "unscored": {"missing_prediction": 1, "unknown_choice": 0},
                                 "extra_predictions": 1, "calibrated": 2, "missing_confidence": 1}
            and (report["correct"], report["accuracy"]) == (2, 2 / 3)
            and report["confusion"]["bug"] == {"billing": 1, "bug": 0, "other": 0}
            and report["confusion"]["billing"]["billing"] == report["confusion"]["other"]["other"] == 1
            and len(report["calibration"]) == 10 and filled == {0.5: (1, 0.5, 0.0), 0.9: (1, 1.0, 1.0)})


def probability_map_ok(answer, labels):
    probs = answer.get("probabilities", {})
    values = list(probs.values())
    return (set(probs) == set(labels) and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in values)
            and abs(sum(values) - 1) < 0.01 and answer.get("choice") in labels)


LIVE_BATCH = ('{"id":"verify-live-2","state":"The app crashes when I press save."}\n'
              '{"id":"verify-live-3","state":"Please refund the duplicate charge on my card."}\n')
LIVE = [
    Step("dry run of the live batch: free, zero cost, sends nothing", ["batch", "--dry-run", "--questions", "choice.json"],
         LIVE_BATCH, stdout=lambda o: report(o, "free", "0", [(1, "verify-live-2"), (2, "verify-live-3")])),
    Step("live classify noul", ["classify", "--questions", "noul.v1.json"],
         '{"id":"verify-live-1","state":"I was charged twice for my subscription this month."}',
         stdout=lambda o: lines_json(o)[0]["id"] == "verify-live-1"
         and 0 <= lines_json(o)[0]["answers"]["billing"]["noul"] <= 1, timeout=90),
    Step("live batch choice", ["batch", "--questions", "choice.json"], LIVE_BATCH,
         stdout=lambda o: [x["id"] for x in lines_json(o)] == ["verify-live-2", "verify-live-3"]
         and all(probability_map_ok(x["answers"]["topic"], CHOICE["topic"]["criteria"]) for x in lines_json(o)),
         timeout=180),
]


def build(run_dir, ref):
    """Copy the code under test into an empty dir: `worktree` = tracked + untracked-unignored files."""
    target = run_dir / "build"
    target.mkdir(parents=True)
    if ref == "worktree":
        files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "-co", "--exclude-standard"],
                               capture_output=True, check=True).stdout.decode().split("\0")
        for rel in filter(None, files):
            src = ROOT / rel
            if src.is_file() and not rel.startswith(".claude/"):
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target / rel)
    else:
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", ref], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(target, filter="data")
    subprocess.run([sys.executable, "-m", "compileall", "-q", "jev.py", "scripts"], cwd=target, check=True)
    return target


def write_fixtures(work):
    work.mkdir(parents=True, exist_ok=True)
    for name, value in {"noul.json": NOUL, "choice.json": CHOICE, "box.json": BOX, "empty.json": {},
                        "weird.json": {"q": {"type": "essay"}}, "results.jsonl": RESULTS,
                        "noul.v1.json": {"schema_version": 1, "questions": NOUL},
                        "choice.v1.json": {"schema_version": 1, "questions": CHOICE},
                        "v2.json": {"schema_version": 2, "questions": NOUL},
                        "extra.v1.json": {"schema_version": 1, "questions": NOUL, "note": SECRET_BODY},
                        "empty.v1.json": {"schema_version": 1, "questions": {}},
                        "broken.json": '{"billing": {"type": "noul"',
                        "mixed.json": {**CHOICE, **NOUL},
                        "record.json": {"id": {"ticket": 7}, "state": {"subject": "App crashes on save"}}}.items():
        (work / name).write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    # Fake HOME session read by scripts/demo_sessions.py; synthetic text only, never a real session.
    sessions = work / ".codex/sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "synthetic.jsonl").write_text("".join(json.dumps(line) + "\n" for line in (
        {"type": "session_meta", "payload": {"id": "synthetic-session", "cwd": "/elsewhere"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": DEMO_TEXT}]}})), encoding="utf-8")
    (work / "labels.jsonl").write_text(EVAL_LABELS, encoding="utf-8")
    (work / "dup-labels.jsonl").write_text(EVAL_LABELS.splitlines(True)[0] * 2, encoding="utf-8")
    (work / "bad-labels.jsonl").write_text('{"id": "e1", "label": "refund"}\n', encoding="utf-8")


def execute(step, build_dir, work, stub):
    stub.queue[:] = list(step.stub)
    stub.seen.clear()
    problems = []
    if step.before and step.before(work) is False:
        problems.append("precondition failed")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(work), "LANG": "C.UTF-8", **step.env}
    if step.setup:
        step.setup(build_dir)
    # Fixture files live in `work`; the CLI runs from there as `python3 <build>/<script> ...`.
    cmd = [sys.executable, str(build_dir / step.script), *step.argv]
    started = time.monotonic()
    proc = subprocess.run(cmd, input=step.stdin, capture_output=True, text=True, encoding="utf-8", cwd=work, env=env,
                          timeout=step.timeout)
    elapsed = time.monotonic() - started
    time.sleep(0.05)
    seen = list(stub.seen)
    if proc.returncode != step.code:
        problems.append(f"exit {proc.returncode}, expected {step.code}")
    if step.stderr and step.stderr not in proc.stderr:
        problems.append(f"stderr lacks {step.stderr!r}")
    if step.code == 0 and proc.stderr and not step.stderr:
        problems.append("stderr not empty on success")
    if "Traceback" in proc.stderr or SECRET_BODY in proc.stderr + proc.stdout or SENTINEL_KEY in proc.stdout:
        problems.append("traceback, private body, or key leaked to output")
    for label, check, value in (("stdout", step.stdout, proc.stdout), ("requests", step.requests, seen),
                                ("files", step.files, build_dir)):
        try:
            if check and not check(value):
                problems.append(f"{label} check failed")
        except (ValueError, KeyError, IndexError, TypeError, OSError) as exc:
            problems.append(f"{label} check raised {type(exc).__name__}: {exc}")
    shown = " ".join(["python3", step.script, *step.argv])
    return {"step": step.name, "command": shown, "stdin": step.stdin, "env": sorted(step.env),
            "exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "seconds": round(elapsed, 2),
            "stub_requests": [{k: r[k] for k in ("path", "bytes", "body")} | {"headers": {
                h: v for h, v in r["headers"].items() if h in ("user-agent", "authorization", "content-type",
                                                               "typesafe-danger-zone-images")}} for r in seen],
            "problems": problems}


def transcript(results, path):
    out = []
    for journey, steps in results.items():
        out.append(f"## {journey}\n")
        for r in steps:
            out.append(f"### {'PASS' if not r['problems'] else 'FAIL'} {r['step']}\n")
            out.append(f"```\n$ {r['command']}" + (f" <<< {r['stdin']!r}" if r["stdin"] else "")
                       + f"\nexit {r['exit']} in {r['seconds']}s\n--- stdout\n{r['stdout']}--- stderr\n{r['stderr']}```\n")
            if r["stub_requests"]:
                out.append("stub saw:\n```json\n" + json.dumps(r["stub_requests"], indent=1, ensure_ascii=False) + "\n```\n")
            for p in r["problems"]:
                out.append(f"- problem: {p}\n")
    path.write_text("\n".join(out), encoding="utf-8")


def cmd_run(args):
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = RUNS / run_id
    evidence = run_dir / "evidence"
    evidence.mkdir(parents=True)
    stub = Stub()
    try:
        build_dir = build(run_dir, args.ref)
        work = run_dir / "work"
        write_fixtures(work)
        table = journeys(stub.url)
        if args.live:
            table["live-opencode"] = LIVE
        chosen = args.journeys or list(table)
        unknown = [j for j in chosen if j not in table]
        if unknown:
            raise SystemExit(f"unknown journey {unknown}; known: {sorted(table)} (live-opencode needs --live)")
        results = {j: [execute(s, build_dir, work, stub) for s in table[j]] for j in chosen}
    finally:
        stub.close()
        if not args.keep_build:
            shutil.rmtree(run_dir / "build", ignore_errors=True)
            shutil.rmtree(run_dir / "work", ignore_errors=True)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    (evidence / "results.json").write_text(json.dumps({"ref": args.ref, "head": head, "live": args.live,
                                                       "journeys": results}, indent=1, ensure_ascii=False), encoding="utf-8")
    transcript(results, evidence / "transcript.md")
    failed = 0
    for journey, steps in results.items():
        for r in steps:
            mark = "PASS" if not r["problems"] else "FAIL"
            failed += bool(r["problems"])
            print(f"{mark}  {journey}: {r['step']}" + "".join(f"\n      {p}" for p in r["problems"]))
    print(f"evidence: {evidence.relative_to(ROOT)}/transcript.md")
    return 1 if failed else 0


def cmd_doctor(args):
    problems = []
    print(f"python {sys.version.split()[0]} at {sys.executable}")
    if sys.version_info < (3, 9):
        problems.append("python3 >= 3.9 required")
    head = subprocess.run(["git", "-C", str(ROOT), "status", "-sb", "--porcelain"], capture_output=True, text=True)
    print("git: " + (head.stdout.splitlines() or ["?"])[0] + f", {len(head.stdout.splitlines()) - 1} changed paths")
    compiled = subprocess.run([sys.executable, "-c", f"import ast;ast.parse(open({str(ROOT / 'jev.py')!r}).read())"],
                              capture_output=True, text=True)
    if compiled.returncode:
        problems.append("jev.py does not parse")
    try:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        probe.close()
        print("stub: can bind 127.0.0.1 ephemeral port")
    except OSError as exc:
        problems.append(f"cannot bind loopback for stub: {exc}")
    try:
        socket.getaddrinfo("opencode.ai", 443)
        print("live: opencode.ai resolves (only --live sends requests)")
    except OSError:
        print("live: opencode.ai does not resolve; live-opencode will fail, offline journeys still run")
    builds = sorted(RUNS.glob("*/build")) if RUNS.exists() else []
    print(f"runs: {len(list(RUNS.glob('*'))) if RUNS.exists() else 0} under {RUNS.relative_to(ROOT)}, "
          f"{len(builds)} leftover build dirs" + (" (run cleanup)" if builds else ""))
    for p in problems:
        print("PROBLEM " + p)
    return 1 if problems else 0


def cmd_cleanup(args):
    removed = 0
    for run_dir in sorted(RUNS.glob("*")) if RUNS.exists() else []:
        for scratch in ("build", "work"):
            if (run_dir / scratch).exists():
                shutil.rmtree(run_dir / scratch)
                removed += 1
    print(f"removed {removed} scratch dirs; evidence kept under {RUNS.relative_to(ROOT)}/*/evidence")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="build fresh, drive journeys, keep evidence")
    run.add_argument("journeys", nargs="*", help="cli-surface classify batch summarize cache eval backend-select demo-redaction live-opencode")
    run.add_argument("--live", action="store_true", help="add live-opencode: 3 tiny synthetic requests to OpenCode")
    run.add_argument("--ref", default="worktree", help="worktree (default) or a git ref such as HEAD")
    run.add_argument("--keep-build", action="store_true")
    sub.add_parser("doctor", help="read-only readiness check")
    sub.add_parser("cleanup", help="remove build/work scratch, keep evidence")
    args = parser.parse_args()
    return {"run": cmd_run, "doctor": cmd_doctor, "cleanup": cmd_cleanup}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
