#!/usr/bin/env python3
"""Bounded exhaustive contract checks for jev.py and demo redaction. Stdlib only, no network."""

import argparse
from collections import Counter
import contextlib
from dataclasses import dataclass, field
from decimal import Decimal
import http.client
import io
import itertools
import json
import os
import shutil
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import traceback
import types
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
TARGET = ROOT / "jev.py"
DEMO = ROOT / "scripts" / "demo_sessions.py"
COVERED = ["jev.py", "scripts/demo_sessions.py"]
# Git blob ids of each covered file as last checked. Content, not a commit, so rebases and squash merges keep it valid.
SYNC_BLOBS = {
    "jev.py": "1131bd1990f2a7898f431a0f1f108681db2a7781",
    "scripts/demo_sessions.py": "5a45729868d435be631d5df31262a2218e158fb8",
}
KEY = "sentinel-key-not-real"
SECRET_BODY = "SECRET-BODY-must-not-leak"
TYPESAFE_HOST = "api.typesafe.ai"


@dataclass
class Run:
    code: object
    stdout: str
    stderr: str
    requests: list = field(default_factory=list)
    key_reads: int = 0
    crash: str = ""


def load(source, path=TARGET):
    module = types.ModuleType(path.stem)
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def drive(jev, argv, stdin="", env=None, respond=None):
    """Run jev.main in-process with urlopen replaced by `respond(index, request)`."""
    out_bytes = io.BytesIO()
    out = io.TextIOWrapper(out_bytes, encoding="utf-8", newline="\n", write_through=False)
    err = io.StringIO()
    run = Run(None, "", "")
    real_api_key = jev.api_key

    def api_key():
        run.key_reads += 1
        return real_api_key()

    def urlopen(request, timeout):
        # http.client validates the host when it builds a connection; mirror that without a socket.
        http.client.HTTPConnection(request.host)
        run.requests.append({"request": request, "timeout": timeout,
                             "lines_flushed": out_bytes.getvalue().count(b"\n")})
        body = respond(len(run.requests) - 1, request)
        return contextlib.closing(io.BytesIO(body))

    keychain = types.SimpleNamespace(returncode=1, stdout="", stderr="")
    with patch.dict(os.environ, env or {}, clear=True), \
         patch.object(jev, "api_key", api_key), \
         patch("subprocess.run", return_value=keychain), \
         patch("urllib.request.urlopen", side_effect=urlopen), \
         patch("sys.stdin", io.StringIO(stdin)), \
         patch("sys.stdout", out), contextlib.redirect_stderr(err):
        try:
            run.code = jev.main(argv)
        except SystemExit as exc:
            run.code = exc.code
        except BaseException:
            run.crash = traceback.format_exc(limit=1).strip().splitlines()[-1]
    out.flush()
    run.stdout = out_bytes.getvalue().decode("utf-8")
    run.stderr = err.getvalue()
    return run


def same(a, b):
    """Structural equality that also distinguishes 1 vs 1.0, -0.0 vs 0.0, and float bits."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        return a.hex() == b.hex()
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(map(same, a, b))
    return a == b


class Checker:
    def __init__(self, jev, tmp, demo=None):
        self.jev = jev
        self.demo = demo
        self.tmp = Path(tmp)
        self.violations = []
        self.cases = 0
        self._files = {}

    def questions_file(self, questions):
        data = questions if isinstance(questions, bytes) else json.dumps(questions).encode()
        if data not in self._files:
            path = self.tmp / f"q{len(self._files)}.json"
            path.write_bytes(data)
            self._files[data] = str(path)
        return self._files[data]

    def fail(self, prop, case, detail):
        self.violations.append((prop, case, detail))

    def clean_exit(self, prop, case, run):
        if run.crash:
            self.fail(prop, case, f"uncaught exception: {run.crash}")
            return False
        return True


# P1. JSONL batch: output is exactly the response-merged prefix before the first failing line.

def ok_response(index):
    return {"model": "m", "answers": {"q": {"type": "noul", "noul": 1 / (index + 3)}},
            "usage": {"input_tokens": index}, "request_index": index}


def http_error(index, request):
    raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(SECRET_BODY.encode()))


def raiser(exc):
    def respond(index, request):
        raise exc
    return respond


def body(text):
    return lambda index, request: text.encode("utf-8") if isinstance(text, str) else text


LINE_KINDS = {
    # name: (stage, line factory(position), responder or None)
    "ok": ("ok", lambda i: json.dumps({"id": [f"r{i}", i, None, [i], {"i": i}][i % 5], "state": f"s{i}"}), None),
    "ok_dup_id": ("ok", lambda i: '{"id":"dup","state":{"t":"x"}}', None),
    "blank": ("local", lambda i: "", None),
    "bad_json": ("local", lambda i: "{", None),
    "nan_in_record": ("local", lambda i: '{"id":1,"state":NaN}', None),
    "overflow_in_record": ("local", lambda i: '{"id":1,"state":{"n":1e999}}', None),
    "not_object": ("local", lambda i: "[1]", None),
    "no_state": ("local", lambda i: '{"id":1}', None),
    "number_state": ("local", lambda i: '{"id":1,"state":3}', None),
    "http_500": ("net", lambda i: '{"id":"h","state":"x"}', http_error),
    "conn_refused": ("net", lambda i: '{"id":"c","state":"x"}', raiser(urllib.error.URLError("refused"))),
    "timeout": ("net", lambda i: '{"id":"t","state":"x"}', raiser(TimeoutError())),
    "resp_not_json": ("net", lambda i: '{"id":"j","state":"x"}', body(SECRET_BODY)),
    "resp_not_utf8": ("net", lambda i: '{"id":"u","state":"x"}', body(b"\xff\xfe")),
    "resp_no_answers": ("net", lambda i: '{"id":"a","state":"x"}', body('{"model":"m"}')),
    "resp_id_collision": ("net", lambda i: '{"id":"i","state":"x"}', body('{"id":9,"answers":{}}')),
    "resp_infinity": ("net", lambda i: '{"id":"f","state":"x"}', body('{"answers":{"q":{"noul":1e999}}}')),
}


def check_batch(c, depth):
    questions = c.questions_file({"q": {"type": "noul", "instructions": "billing?"}})
    for n in range(1, depth + 1):
        for seq in itertools.product(LINE_KINDS, repeat=n):
            c.cases += 1
            lines = [LINE_KINDS[kind][1](i) for i, kind in enumerate(seq)]
            stages = [LINE_KINDS[kind][0] for kind in seq]
            k = next((i for i, s in enumerate(stages) if s != "ok"), n)

            def respond(index, request, seq=seq):
                kind = seq[index] if index < n else "ok"
                responder = LINE_KINDS[kind][2]
                return responder(index, request) if responder else json.dumps(ok_response(index)).encode()

            run = drive(c.jev, ["batch", "--questions", questions], "\n".join(lines) + "\n", respond=respond)
            case = "batch " + ",".join(seq)
            if not c.clean_exit("batch-clean-exit", case, run):
                continue
            expected = [{"id": json.loads(lines[i])["id"], **ok_response(i)} for i in range(k)]
            got = [json.loads(x) for x in run.stdout.splitlines()]
            if not (len(got) == len(expected) and all(map(same, got, expected))):
                c.fail("batch-prefix", case, f"stdout {run.stdout!r} != prefix of {k} merged responses")
            sent = k + (1 if k < n and stages[k] == "net" else 0)
            if len(run.requests) != sent:
                c.fail("batch-one-post-per-line", case, f"{len(run.requests)} POSTs, expected {sent}")
            for i, req in enumerate(run.requests):
                if req["lines_flushed"] != i:
                    c.fail("batch-flush-before-next", case, f"request {i} saw {req['lines_flushed']} flushed lines")
            if k == n:
                if (run.code, run.stderr) != (0, ""):
                    c.fail("batch-exit", case, f"exit {run.code} stderr {run.stderr!r}, expected 0 and silence")
            else:
                prefix = f"jev: batch stopped at line {k + 1}; "
                if run.code != 1 or not run.stderr.startswith(prefix) or run.stderr.count("\n") != 1:
                    c.fail("batch-exit", case, f"exit {run.code} stderr {run.stderr!r}, expected 1 and {prefix!r}")
                if stages[k] == "net" and "invalid JSON record" in run.stderr:
                    c.fail("batch-blames-right-party", case, f"API failure reported as {run.stderr.strip()!r}")
            if SECRET_BODY in run.stderr + run.stdout:
                c.fail("batch-no-body-leak", case, "HTTP or response body echoed")
            check_batch_dry_run(c, questions, seq, lines, stages)


def check_batch_dry_run(c, questions, seq, lines, stages):
    run = drive(c.jev, ["batch", "--dry-run", "--questions", questions], "\n".join(lines) + "\n",
                respond=lambda index, request: json.dumps(ok_response(index)).encode())
    c.cases += 1
    case = "batch --dry-run " + ",".join(seq)
    if not c.clean_exit("dry-run-clean-exit", case, run):
        return
    if run.requests or run.key_reads:
        c.fail("dry-run-no-network-no-credentials", case, f"{len(run.requests)} requests, {run.key_reads} key reads")
    k = next((i for i, s in enumerate(stages) if s == "local"), len(seq))
    if k < len(seq):
        prefix = f"jev: batch stopped at line {k + 1}; "
        if run.code != 1 or run.stdout or not run.stderr.startswith(prefix) or run.stderr.count("\n") != 1:
            c.fail("dry-run-batch-exit", case, f"exit {run.code} stdout {run.stdout[:80]!r} stderr {run.stderr!r}")
        return
    report = json.loads(run.stdout) if run.code == 0 and run.stdout.count("\n") == 1 else {}
    rows = report.get("records", [])
    if run.stderr or not (len(rows) == len(lines) and all(
            row["line"] == i + 1 and same(row["id"], json.loads(line)["id"]) for i, (row, line) in enumerate(zip(rows, lines)))):
        c.fail("dry-run-batch-report", case, f"exit {run.code} stderr {run.stderr!r} stdout {run.stdout[:120]!r}")
    elif report["total_input_tokens_estimate"] != sum(row["input_tokens_estimate"] for row in rows):
        c.fail("dry-run-total-is-sum", case, f"total {report['total_input_tokens_estimate']}")


# P2. Request shape and credential isolation for every backend, question set, and record in scope.

BASE_URLS = [
    "https://opencode.ai/zen/v1",
    "https://opencode.ai/zen/v1//",
    "http://127.0.0.1:8080/custom/",
    "https://api.typesafe.ai/v1",
    "HTTPS://API.TypeSafe.AI/v1",
    "https://api.typesafe.ai:443/v1",
    "http://api.typesafe.ai/v1",
    "https://api.typesafe.ai./v1",
    "https://api.typesafe.ai.evil.example/v1",
    "https://evil.example/api.typesafe.ai/v1",
    "https://api.typesafe.ai\\@evil.example/v1",
    "https://user:pw@api.typesafe.ai/v1",
    "https://api.typesafe.ai/v1?x=1",
    "https://api.typesafe.ai/v1#frag",
    "ftp://api.typesafe.ai/v1",
    "https://api.typesafe.\nai/v1",
    "https://api.typesafe.ai\t/@evil.example/v1",
]
MODELS = ["jev-1.13-free", "jev-1.14.0", " "]
NOUL = {"type": "noul", "instructions": "yes?"}
BOX = {"type": "box", "instructions": "find it"}
QUESTION_SETS = [
    {"n": NOUL},
    {"c": {"type": "choice", "criteria": {"a": "A", "b": "B"}}, "s": {"type": "score", "criteria": ["lo", "hi"]}},
    {"b": BOX},
    {"n": NOUL, "b": BOX},
]
ASSETS = {"img": {"type": "image", "format": "png", "data_base64": "AA=="}}
RECORDS = [
    '{"id":"a","state":""}',
    '{"id":0,"state":"ünïcode ✓","extra":"never sent"}',
    '{"id":null,"state":{"messages":[{"role":"user","text":"hi"}]}}',
    '{"id":[1],"state":["x",{"y":2}]}',
    json.dumps({"id": "img", "state": "look", "assets": ASSETS}),
    '{"id":"bad","state":{"n":1e999}}',
]


def urllib_target(url):
    try:
        request = urllib.request.Request(url.rstrip("/") + "/systemone")
        return request.type, request.host.lower()
    except ValueError:
        return None, None


def check_requests(c):
    for base, model, questions, raw, with_key in itertools.product(BASE_URLS, MODELS, QUESTION_SETS, RECORDS, (True, False)):
        c.cases += 1
        case = f"request base={base!r} model={model!r} questions={sorted(questions)} record={raw[:40]} key={with_key}"
        env = {"TYPESAFE_API_KEY": KEY} if with_key else {}
        response = {"answers": {}, "model": model}
        run = drive(c.jev, ["classify", "--questions", c.questions_file(questions), "--base-url", base,
                            "--model", model, "--timeout", "7"], raw, env, lambda i, r: json.dumps(response).encode())
        check_request_dry_run(c, case, base, model, questions, raw, env, run)
        if not c.clean_exit("request-clean-exit", case, run):
            continue
        record = json.loads(raw)
        scheme, host = urllib_target(base)
        to_typesafe = scheme == "https" and host == TYPESAFE_HOST
        if run.key_reads and not to_typesafe:
            c.fail("credential-read-only-for-typesafe", case, f"credentials read for urllib target {scheme}://{host!r}")
        for req in run.requests:
            r = req["request"]
            auth = r.get_header("Authorization")
            if auth is not None and not (r.type == "https" and r.host.lower() == TYPESAFE_HOST):
                c.fail("auth-only-to-typesafe", case, f"Authorization sent to {r.type}://{r.host!r}")
        if len(run.requests) > 1:
            c.fail("request-at-most-one", case, f"{len(run.requests)} requests")
        if run.code != 0:
            if run.requests and run.code != 1:
                c.fail("request-reject-before-network", case, f"exit {run.code} after a request")
            if not run.stderr.startswith("jev: ") and run.code != 2:
                c.fail("request-reject-message", case, f"stderr {run.stderr!r}")
            continue
        if len(run.requests) != 1:
            c.fail("request-exactly-one", case, f"exit 0 with {len(run.requests)} requests")
            continue
        free = urllib_target(base)[1] == "opencode.ai" or "1.13" in model or model.endswith("-free")
        images = "assets" in record or any(q["type"] == "box" for q in questions.values())
        if images and free:
            c.fail("box-rejected-locally-on-free", case, "image request sent to a text-only backend")
        r = run.requests[0]["request"]
        expected_body = {"model": model, "state": record["state"], "questions": questions}
        if "assets" in record:
            expected_body["assets"] = record["assets"]
        if not same(json.loads(r.data), expected_body):
            c.fail("request-body", case, f"body {r.data[:200]!r}")
        checks = {
            "request-endpoint": r.full_url == base.rstrip("/") + "/systemone",
            "request-method": r.get_method() == "POST",
            "request-timeout": run.requests[0]["timeout"] == 7,
            "request-content-type": r.get_header("Content-type") == "application/json",
            "request-user-agent": r.get_header("User-agent") == "jev-cli/0.1",
            "images-header-iff-images": (r.get_header("Typesafe-danger-zone-images") == "true") == images
            and r.get_header("Typesafe-danger-zone-images") in (None, "true"),
            "auth-sent-to-typesafe-when-keyed": (r.get_header("Authorization") == f"Bearer {KEY}") == (to_typesafe and with_key),
            "response-id-preserved": same(json.loads(run.stdout)["id"], record["id"]),
        }
        for prop, ok in checks.items():
            if not ok:
                c.fail(prop, case, f"headers {dict(r.header_items())} stdout {run.stdout[:120]!r}")


# P2b. Dry run: same local verdict as a real send, no credentials or network, README estimator and price table.

# README "Dry run" prices, restated independently of jev.PRICES for the models in MODELS.
DRY_RUN_PRICES = {("opencode.ai", "jev-1.13-free"): "0", (TYPESAFE_HOST, "jev-1.14.0"): "0.042"}


def readme_estimate(value):
    return -(-len(json.dumps(value).encode("utf-8")) // 4)


def check_request_dry_run(c, case, base, model, questions, raw, env, real):
    run = drive(c.jev, ["classify", "--dry-run", "--questions", c.questions_file(questions), "--base-url", base,
                        "--model", model], raw, env, lambda i, r: b'{"answers":{}}')
    c.cases += 1
    case = case.replace("request ", "request --dry-run ", 1)
    if not c.clean_exit("dry-run-clean-exit", case, run):
        return
    if run.requests or run.key_reads:
        c.fail("dry-run-no-network-no-credentials", case, f"{len(run.requests)} requests, {run.key_reads} key reads")
    # A send's missing-key failure is a credential step, which dry run skips by contract.
    missing_key = real.stderr.startswith("jev: missing TYPESAFE_API_KEY")
    if not real.crash and not missing_key and (run.code, run.stderr) != (real.code, real.stderr):
        c.fail("dry-run-validates-like-send", case, f"dry exit {run.code} {run.stderr!r}, send exit {real.code} {real.stderr!r}")
    if run.code != 0:
        if run.stdout:
            c.fail("dry-run-validates-like-send", case, f"stdout on rejection {run.stdout[:80]!r}")
        return
    record = json.loads(raw)
    report = json.loads(run.stdout)
    row, = report["records"]
    text_body = {"model": model, "state": record["state"], "questions": questions}
    tokens = readme_estimate(text_body)
    if not same(row["id"], record["id"]) or row["input_tokens_estimate"] != tokens \
            or report["total_input_tokens_estimate"] != tokens:
        c.fail("dry-run-tokens", case, f"row {row} expected {tokens} tokens over body without assets")
    sent = [r["request"].data for r in real.requests]
    if len(sent) == 1 and row["request_bytes"] != len(sent[0]):
        c.fail("dry-run-measures-sent-bytes", case, f"request_bytes {row['request_bytes']} != {len(sent[0])} bytes sent")
    scheme, host = urllib_target(base)
    if host == TYPESAFE_HOST and scheme != "https":
        host = None
    price = DRY_RUN_PRICES.get((host, model))
    cost = None if price is None or "assets" in record else Decimal(price) * tokens / 1000000
    got = report["estimated_cost_usd"]
    pricing = report["pricing"]
    if pricing["input_usd_per_million_tokens"] != price or (got is None) != (cost is None) \
            or (cost is not None and Decimal(got) != cost) \
            or pricing["status"] != ("unknown" if price is None else "free" if Decimal(price) == 0 else "priced"):
        c.fail("dry-run-cost", case, f"pricing {pricing} cost {got!r}, expected price {price!r} cost {cost!r}")


# P3. Probabilities and numbers pass through bit for bit; nonfinite values never reach stdout.

FINITE = ["0", "0.0", "-0.0", "5e-324", "0.1", "0.3333333333333333", "0.30000000000000004",
          "0.9999999999999999", "1.0", "1e308", "18446744073709551616", "1E-7"]
NONFINITE = ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"]
RESPONSE_TEMPLATE = ('{{"model":"jev-1.13-free","answers":{{"route":{{"type":"choice","choice":"a",'
                     '"confidence":{0},"probabilities":{{"a":{1},"b":0.125,"c":{0}}}}},'
                     '"bill":{{"type":"noul","noul":{1}}}}},"usage":{{"input_tokens":{2}}},"future":[{2}]}}')


def check_numbers(c):
    questions = c.questions_file({"route": {"type": "choice", "criteria": {"a": "A", "b": "B", "c": "C"}},
                                  "bill": NOUL})
    for command, slots in itertools.product(("classify", "batch"), itertools.product(FINITE + NONFINITE, repeat=3)):
        c.cases += 1
        raw = RESPONSE_TEMPLATE.format(*slots)
        case = f"numbers {command} slots={slots}"
        run = drive(c.jev, [command, "--questions", questions], '{"id":"n","state":"x"}\n',
                    respond=lambda i, r: raw.encode())
        if not c.clean_exit("numbers-clean-exit", case, run):
            continue
        if any(s in NONFINITE for s in slots):
            if run.code != 1 or run.stdout:
                c.fail("numbers-nonfinite-rejected", case, f"exit {run.code} stdout {run.stdout[:120]!r}")
            continue
        expected = {"id": "n", **json.loads(raw)}
        lines = run.stdout.splitlines()
        if run.code != 0 or len(lines) != 1 or not same(json.loads(lines[0]), expected):
            c.fail("numbers-passthrough-exact", case, f"exit {run.code} stdout {run.stdout[:200]!r}")


# P4. Question files: legacy mappings and version-1 documents send the same unwrapped mapping; bad files never reach HTTP.

QUESTION_TYPES = ("choice", "score", "noul", "box")
PRIVATE = "PRIVATE-question-content"
GENERIC_ERROR = "jev: could not read input, credentials, or API JSON"
EVAL_ENV = {"TYPESAFE_API_KEY": KEY, "JEV_BASE_URL": "https://api.typesafe.ai/v1"}
QNOUL = {"type": "noul", "instructions": PRIVATE}
MAPPINGS = [
    {"n": QNOUL},
    {"c": {"type": "choice", "criteria": {"a": PRIVATE, "b": "B"}}, "s": {"type": "score", "criteria": ["lo", "hi"]}},
    {}, [], [QNOUL], "x", None, {"q": "noul"}, {"q": {"type": "essay", "note": PRIVATE}},
]
VERSIONS = [1, 2, 0, -1, "1", 1.0, True, None, [1], QNOUL]
RAW_FILES = [b"", b"{", b"null", b'"s"', b"\xff\xfe", b'{"n":{"type":"noul","x":NaN}}',
             b'{"n":{"type":"noul","x":1e999}}', b"[" * 100000]


def question_documents():
    for m in MAPPINGS:
        yield f"legacy {json.dumps(m)[:50]}", m
        for v in VERSIONS:
            label = f"v={json.dumps(v)[:20]} m={json.dumps(m)[:50]}"
            yield "wrapped " + label, {"schema_version": v, "questions": m}
            yield "reordered " + label, {"questions": m, "schema_version": v}
            yield "extra-key " + label, {"schema_version": v, "questions": m, PRIVATE: PRIVATE}
        yield f"no-version {json.dumps(m)[:50]}", {"questions": m}
    for v in VERSIONS:
        yield f"no-questions v={json.dumps(v)[:20]}", {"schema_version": v}
    for raw in RAW_FILES:
        yield f"raw {raw[:30]!r}", raw


def expected_questions(document):
    """The README contract: the mapping a question file must send, or None if it must be rejected."""
    if isinstance(document, dict) and "schema_version" in document:
        version = document["schema_version"]
        if type(version) is not int or version != 1 or sorted(document) != ["questions", "schema_version"]:
            return None
        document = document["questions"]
    if not isinstance(document, dict) or not document:
        return None
    if any(not isinstance(q, dict) or q.get("type") not in QUESTION_TYPES for q in document.values()):
        return None
    return document


def check_question_files(c):
    # summarize reads the same file for label order: empty input and an absent --question name isolate file loading.
    for (label, document), command in itertools.product(question_documents(), ("classify", "batch", "summarize")):
        c.cases += 1
        case = f"questions {command} {label}"
        summarize = command == "summarize"
        argv = [command, "--questions", c.questions_file(document)] + (["--question", "zz"] if summarize else [])
        run = drive(c.jev, argv, "" if summarize else '{"id":"q","state":"x"}\n',
                    respond=lambda i, r: json.dumps(ok_response(i)).encode())
        if not c.clean_exit("questions-clean-exit", case, run):
            continue
        if not summarize:
            check_question_file_dry_run(c, case, command, document, run)
        expected = None if isinstance(document, bytes) else expected_questions(document)
        if (run.code == 0) != (expected is not None):
            c.fail("questions-accept-iff-valid", case, f"exit {run.code} stderr {run.stderr[:120]!r}")
            continue
        if expected is not None and summarize:
            if run.requests or json.loads(run.stdout).get("records") != 0:
                c.fail("questions-summarize-offline", case, f"{len(run.requests)} requests stdout {run.stdout[:120]!r}")
            continue
        if expected is not None:
            bodies = [json.loads(r["request"].data) for r in run.requests]
            if len(bodies) != 1 or list(bodies[0]) != ["model", "state", "questions"] \
                    or not same(bodies[0]["questions"], expected):
                c.fail("questions-unwrap-exact", case, f"bodies {bodies!r:.200}")
            continue
        if run.code != 1 or run.requests:
            c.fail("questions-reject-before-network", case, f"exit {run.code}, {len(run.requests)} requests")
        lines = run.stderr.splitlines()
        if len(lines) != 1 or not lines[0].startswith("jev: ") or lines[0] == GENERIC_ERROR:
            c.fail("questions-reject-message", case, f"stderr {run.stderr!r}")
        if PRIVATE in run.stderr + run.stdout:
            c.fail("questions-reject-no-echo", case, f"stderr {run.stderr[:200]!r}")
    labels = c.tmp / "eval-labels.jsonl"
    labels.write_text('{"id":"q","label":"a"}\n', encoding="utf-8")
    for label, document in question_documents():
        c.cases += 1
        case = f"questions eval {label}"
        run = drive(c.jev, ["eval", "--questions", c.questions_file(document), "--labels", str(labels), "--question", "c"],
                    '{"id":"q","answers":{"c":{"choice":"a","confidence":1.0}}}\n', EVAL_ENV)
        if not c.clean_exit("questions-clean-exit", case, run):
            continue
        if run.requests or run.key_reads:
            c.fail("eval-offline", case, f"{len(run.requests)} requests, {run.key_reads} credential reads")
        mapping = None if isinstance(document, bytes) else expected_questions(document)
        choice = (mapping or {}).get("c")
        if choice and choice["type"] == "choice" and isinstance(choice.get("criteria"), dict) and choice["criteria"]:
            report = json.loads(run.stdout) if run.code == 0 else {}
            if report.get("labels") != list(choice["criteria"]) or report.get("accuracy") != 1.0:
                c.fail("questions-eval-same-loader", case, f"exit {run.code} stdout {run.stdout[:120]!r} stderr {run.stderr!r}")
            continue
        lines = run.stderr.splitlines()
        if run.code != 1 or run.stdout or len(lines) != 1 or not lines[0].startswith("jev: ") \
                or lines[0] == GENERIC_ERROR or PRIVATE in run.stderr:
            c.fail("questions-eval-same-loader", case, f"exit {run.code} stderr {run.stderr[:200]!r}")


# P8. Summarize: offline, every record counted once, bins and diagnostics match an independent oracle.

SUMMARY_ABSENT = object()
CONFIDENCES = [SUMMARY_ABSENT, 0, 0.0, -0.0, 0.1, 0.09999999999999999, 0.5, 0.30000000000000004, 0.9, 0.9999999999999999,
               1, 1.0, -1e-9, 1.0000000000000002, 2, "0.5", True, None]
SUMMARY_KINDS = {f"{label}:{i}": (label, conf) for label in ("a", "b") for i, conf in enumerate(CONFIDENCES)}
SUMMARY_KINDS.update({"bad_json": "{", "no_id": '{"answers":{"q":{"choice":"a"}}}',
                      "score": '{"id":"s","answers":{"q":{"type":"score","score":1}}}'})
SUMMARY_LABELS = ["a", "b", "c"]
LONG_KINDS = ["a:0", "b:10", "score"]
BIN_NAMES = [f"{i / 10:.1f}-{(i + 1) / 10:.1f}" for i in range(10)]


def summary_line(kind, i):
    spec = SUMMARY_KINDS[kind]
    if isinstance(spec, str):
        return spec
    label, conf = spec
    answer = {"type": "choice", "choice": label, "probabilities": {"a": 0.5, "b": 0.5}}
    if conf is not SUMMARY_ABSENT:
        answer["confidence"] = conf
    return json.dumps({"id": [f"id{i}", i, None][i % 3], "answers": {"q": answer}})


def oracle_bin(value):
    return next(i for i in range(9, -1, -1) if value >= i / 10 or i == 0) if value < 1 else 9


def check_summarize(c, depth):
    choice = {"type": "choice", "criteria": {label: label for label in SUMMARY_LABELS}}
    files = {"seeded": c.questions_file({"q": choice}), "inferred": c.questions_file({"q": choice, "n": NOUL}),
             "versioned": c.questions_file({"schema_version": 1, "questions": {"q": choice, "n": NOUL}})}
    # Short sequences cover every record kind; four-record sequences make the three-ID example cap bind.
    sequences = [seq for n in range(depth + 1) for seq in itertools.product(SUMMARY_KINDS, repeat=n)]
    sequences += list(itertools.product(LONG_KINDS, repeat=4))
    # inferred: no --question, a two-question file, so the name comes from the input and labels still seed.
    for seq, mode in itertools.product(sequences, ("seeded", "plain", "inferred", "versioned")):
        n = len(seq)
        seeded = mode != "plain"
        c.cases += 1
        lines = [summary_line(kind, i) for i, kind in enumerate(seq)]
        argv = ["summarize", "--base-url", "https://api.typesafe.ai/v1"] + ([] if mode == "inferred" else ["--question", "q"])
        run = drive(c.jev, argv + (["--questions", files[mode]] if seeded else []),
                    "".join(line + "\n" for line in lines), {"TYPESAFE_API_KEY": KEY})
        case = f"summarize {mode} " + ",".join(seq)
        if not c.clean_exit("summarize-clean-exit", case, run):
            continue
        if run.requests or run.key_reads:
            c.fail("summarize-offline", case, f"{len(run.requests)} requests, {run.key_reads} credential reads")
        if mode == "inferred" and n == 0:
            if run.code != 1 or run.stdout or "select one question with --question" not in run.stderr:
                c.fail("summarize-selection", case, f"exit {run.code} stderr {run.stderr!r}")
            continue
        parse_bad = [i for i, kind in enumerate(seq) if kind in ("bad_json", "no_id")]
        answer_bad = [i for i, kind in enumerate(seq) if kind == "score"]
        if parse_bad or answer_bad:
            line = (parse_bad or answer_bad)[0] + 1
            if run.code != 1 or run.stdout or not run.stderr.startswith(f"jev: summarize stopped at line {line}; "):
                c.fail("summarize-stop-line", case, f"exit {run.code} stderr {run.stderr!r}, expected line {line}")
            continue
        counts = dict.fromkeys(SUMMARY_LABELS if seeded else [], 0)
        examples = {label: [] for label in counts}
        bins, classes, diagnostics = [0] * 10, {"missing": 0, "malformed": 0, "out_of_range": 0}, []
        for i, kind in enumerate(seq):
            label, conf = SUMMARY_KINDS[kind]
            counts[label] = counts.get(label, 0) + 1
            examples.setdefault(label, [])
            if len(examples[label]) < 3:
                examples[label].append(json.loads(lines[i])["id"])
            if conf is SUMMARY_ABSENT:
                classes["missing"] += 1
            elif type(conf) not in (int, float):
                classes["malformed"] += 1
                diagnostics.append(f"line {i + 1}: confidence {json.dumps(conf)} is not a number")
            elif not 0 <= conf <= 1:
                classes["out_of_range"] += 1
                diagnostics.append(f"line {i + 1}: confidence {json.dumps(conf)} is outside [0, 1]")
            else:
                bins[oracle_bin(conf)] += 1
        try:
            got = json.loads(run.stdout)
        except ValueError:
            c.fail("summarize-output", case, f"exit {run.code} stdout {run.stdout[:120]!r}")
            continue
        conf_out = got.get("confidence", {})
        checks = {
            "summarize-exit": run.code == 0 and run.stderr == "".join(f"jev: {d}\n" for d in diagnostics),
            "summarize-counts": got.get("records") == n and same(got.get("counts"), counts),
            "summarize-examples": same(got.get("examples"), examples),
            "summarize-bins": list(conf_out.get("bins", {})) == BIN_NAMES and list(conf_out["bins"].values()) == bins,
            "summarize-confidence-classes": all(conf_out.get(k) == v for k, v in classes.items()),
            "summarize-diagnostics": got.get("diagnostics") == diagnostics,
            "summarize-denominator": sum(conf_out.get("bins", {}).values()) + sum(conf_out.get(k, 0) for k in classes)
            == n == sum(got.get("counts", {}).values()),
        }
        for prop, ok in checks.items():
            if not ok:
                c.fail(prop, case, f"exit {run.code} stdout {run.stdout[:300]!r} stderr {run.stderr[:120]!r}")

    results = c.tmp / "summarize-flags.jsonl"
    results.write_text(summary_line("a:1", 0) + "\n", encoding="utf-8")
    for flag, expected in ((["--cache", str(c.tmp / "summarize-cache")], "jev: summarize sends no requests; --cache is for classify and batch\n"),
                           (["--dry-run"], "jev: summarize sends no requests; --dry-run is for classify and batch\n"),
                           (["--labels", str(results)], "jev: --labels is for eval; --question is for eval and summarize\n")):
        c.cases += 1
        run = drive(c.jev, ["summarize", "--input", str(results), *flag], "", {"TYPESAFE_API_KEY": KEY})
        case = f"summarize {flag[0]}"
        if not c.clean_exit("summarize-clean-exit", case, run):
            continue
        if (run.code, run.stdout, run.stderr) != (1, "", expected) or run.requests or run.key_reads \
                or (c.tmp / "summarize-cache").exists():
            c.fail("summarize-flag-errors", case, f"exit {run.code} stdout {run.stdout[:80]!r} stderr {run.stderr!r}")


# P5. Demo redaction: no planted value survives, structure and IDs stay, a second pass is a no-op,
# redaction precedes the 4000-character cap, and --send redacts its outgoing copy behind unchanged gates.

# (rule that must fire, full text, sensitive part that must not survive)
SECRETS = [
    ("bearer", "Authorization: Bearer abcDEF0123456789xyz", "abcDEF0123456789xyz"),
    ("bearer", "bearer sk-proj0000aaaa1111bbbb", "sk-proj0000aaaa1111bbbb"),
    ("token", "sk-ant-api03-AAAABBBBCCCCDDDD", "AAAABBBBCCCCDDDD"),
    ("token", "ghp_0123456789abcdefghijABCD", "0123456789abcdefghijABCD"),
    ("token", "github_pat_11AAAAAAA0123456789_abcdef", "11AAAAAAA0123456789_abcdef"),
    ("token", "xoxb-1234567890-abcdefghij", "1234567890-abcdefghij"),
    ("token", "AKIAABCDEFGHIJKLMNOP", "ABCDEFGHIJKLMNOP"),
    ("token", "AIza" + "S" * 35, "S" * 35),
    ("token", "eyJhbGciOiJI.eyJzdWIiOiIx.c2lnbmF0dXJl", "eyJzdWIiOiIx.c2lnbmF0dXJl"),
    ("assignment", "password=hunter2hunter2", "hunter2hunter2"),
    ("assignment", 'API_KEY: "abcd1234efgh"', "abcd1234efgh"),
    ("assignment", "secret='s3cr3tvalue'", "s3cr3tvalue"),
    ("token", "api_key=sk-live0000aaaa1111bbbb", "sk-live0000aaaa1111bbbb"),
    ("email", "jane.doe+tag@mail.example.co", "jane.doe+tag@mail.example.co"),
    ("home_path", "/Users/janedoe/Projects", "janedoe"),
    ("home_path", "/home/jdoe/.ssh/id_rsa", "jdoe"),
    ("home_path", "C:\\Users\\JaneD\\Desktop", "JaneD"),
    ("home_path", "D:/Users/JaneD/x", "JaneD"),
    ("private_key", "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY-----", "MIIEabc"),
    ("private_key", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNz", "b3BlbnNz"),
]
BENIGN = ["The token is useful.", "a secret sauce", "password reset flow", "version 1.2.3 at 3:00",
          "https://example.com/home/page", "src/Users/model.py", "Bearer short", "sk-short", "user@ host",
          "C:\\Program Files\\App", "50% off, a/b", "ghp_short", ""]
CONTEXTS = [("", ""), (" ", " "), ("x ", "."), ("(", ")"), ('"', '"'), ("\n", "\n"), ("see: ", ", ok")]
SHAPES = [lambda t: t, lambda t: [t, 7], lambda t: {"k": t, "n": None},
          lambda t: {"a": [{"b": [t, True]}], "c": 1.5}]


def out_shape(out, shape):
    """Replace the one string leaf of a SHAPES value with the marker, keeping every other value."""
    if isinstance(out, str):
        return "§"
    if isinstance(out, list):
        return [out_shape(x, shape) if isinstance(x, (str, list, dict)) else x for x in out]
    if isinstance(out, dict):
        return {k: out_shape(v, shape) if isinstance(v, (str, list, dict)) else v for k, v in out.items()}
    return out


def check_redaction(c):
    demo = c.demo
    for (rule, text, part), (pre, post), shape in itertools.product(SECRETS, CONTEXTS, SHAPES):
        value = shape(pre + text + post)
        case = f"{text!r} in {pre!r}..{post!r} shape {SHAPES.index(shape)}"
        c.cases += 1
        counts = Counter()
        try:
            out = demo.redact(value, counts)
            again = Counter()
            twice = demo.redact(out, again)
        except Exception as exc:
            c.fail("redact-clean-exit", case, repr(exc))
            continue
        dumped = json.dumps(out)
        if part in dumped:
            c.fail("redact-no-fixture-survives", case, dumped[:200])
        if dict(counts) != {rule: 1}:
            c.fail("redact-counts-exact", case, f"{dict(counts)} expected {{{rule!r}: 1}}")
        if not same(twice, out) or again:
            c.fail("redact-idempotent", case, f"second pass {dict(again)}")
        if not same(shape("§"), out_shape(out, shape)):
            c.fail("redact-structure-kept", case, dumped[:200])
        records, meta = demo.redact_records([{"id": text, "state": value, "extra": [text]}])
        if records[0]["id"] != text:
            c.fail("redact-id-kept", case, repr(records[0]["id"]))
        if part in json.dumps(meta) or part in json.dumps(records[0]["extra"]):
            c.fail("redact-metadata-no-leak", case, json.dumps(meta))
    for text, (pre, post), shape in itertools.product(BENIGN, CONTEXTS, SHAPES):
        c.cases += 1
        value = shape(pre + text + post)
        counts = Counter()
        if not same(demo.redact(value, counts), value) or counts:
            c.fail("redact-benign-unchanged", repr(pre + text + post), dict(counts))


# Oracle's own label map, written out rather than read from the code under test.
LABEL_RULES = {"private_key": "private_key", "bearer": "bearer", "token": "token", "secret": "assignment",
               "email": "email", "home": "home_path"}


def check_redact_before_cap(c):
    home = c.tmp / "home"
    sessions = home / ".codex/sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for rule, text, part in SECRETS:
        # cut 0: wholly past the cap; 1..len-1: straddles it; -500: wholly kept.
        for cut in [-500, *range(0, len(text))]:
            c.cases += 1
            message = "x" * (3999 - max(cut, 0) - (500 if cut < 0 else 0)) + " " + text
            (sessions / "s.jsonl").write_text("".join(json.dumps(x) + "\n" for x in (
                {"type": "session_meta", "payload": {"id": "s", "cwd": "/elsewhere"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                      "content": [{"type": "input_text", "text": message}]}})))
            counts = Counter()
            with patch.dict(os.environ, {"HOME": str(home)}):
                records = c.demo.prepare_sample(1, counts)
            saved = json.dumps(records)
            case = f"{text!r} with {cut} chars before the cap"
            leaked = [part[i:i + 6] for i in range(len(part) - 5) if part[i:i + 6] in saved]
            if leaked:
                c.fail("redact-before-cap", case, leaked[0])
            kept = Counter(LABEL_RULES[label] for m in records[0]["state"]["messages"]
                           for label in re.findall(r"\[REDACTED:(\w+)\]", m["text"]) if label in LABEL_RULES)
            if counts != kept or (cut < 0 and counts != {rule: 1}) or (cut == 0 and counts):
                c.fail("redact-counts-kept", case, f"counts {dict(counts)}, saved placeholders {dict(kept)}")


def drive_demo(c, argv, sample_lines):
    root = c.tmp / "demo-root"
    folder = root / ".private/demo"
    folder.mkdir(parents=True, exist_ok=True)
    (root / "examples").mkdir(exist_ok=True)
    (root / "examples/failure-patterns.json").write_text(json.dumps({"failure_pattern": {"type": "choice", "criteria": {"fine": "F"}}}))
    sample = folder / "sample.jsonl"
    if sample_lines is None:
        sample.unlink(missing_ok=True)
    else:
        sample.write_text("".join(json.dumps(r) + "\n" for r in sample_lines))
    before = sample.read_bytes() if sample.exists() else None
    sent, commands = [], []

    def send(command, input=None, stdout=None, encoding=None, env=None, check=False, **kwargs):
        # Model a hostile locale: anything not pinned to UTF-8 on either side of the pipe is ASCII.
        if command[2] == "summarize":
            # The real summarize runs in-process; its stdout crosses the same modeled pipe back to the demo.
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
                code = c.jev.main(command[2:])
            if check and code:
                raise subprocess.CalledProcessError(code, command)
            text = buffer.getvalue().encode((env or {}).get("PYTHONIOENCODING") or "ascii").decode(encoding or "ascii")
            return types.SimpleNamespace(returncode=code, stdout=text, stderr="")
        commands.append(command)
        try:
            received = input.encode(encoding or "ascii").decode((env or {}).get("PYTHONIOENCODING") or "ascii")
        except UnicodeError:
            sent.append(None)
            return types.SimpleNamespace(returncode=1, stderr="jev: could not read input, credentials, or API JSON")
        sent.append(received)
        for line in received.splitlines():
            stdout.write(json.dumps({"id": json.loads(line)["id"], "answers": {"failure_pattern": {"choice": "fine"}}}) + "\n")
        return types.SimpleNamespace(returncode=0, stderr="")

    out, err = io.StringIO(), io.StringIO()
    with patch.object(c.demo, "ROOT", root), patch("subprocess.run", side_effect=send), \
            patch.object(c.demo, "prepare_sample", side_effect=AssertionError("send resampled")), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = c.demo.main(argv)
        except SystemExit as exc:
            code = exc.code
        except BaseException:
            code = "crash: " + traceback.format_exc(limit=-1).strip()
    after = sample.read_bytes() if sample.exists() else None
    summary = (folder / "summary.json").read_text() if (folder / "summary.json").exists() else ""
    (folder / "summary.json").unlink(missing_ok=True)
    c.last_summary = json.loads(summary) if summary else {}
    return code, sent, before == after, out.getvalue() + err.getvalue() + summary, commands


def check_demo_send(c):
    raw = [{"id": f"user{i}@example.test ✓", "state": {"messages": [{"role": "user", "text": "café ✓ " + t}]}}
           for i, (_, t, _) in enumerate(SECRETS)]
    for opt_out in (False, True):
        c.cases += 1
        argv = ["--send", "--limit", "20"] + (["--no-redact"] if opt_out else [])
        code, sent, kept, output, commands = drive_demo(c, argv, raw)
        case = " ".join(argv)
        if [cmd[-2:] for cmd in commands] != [["--cache", str(c.tmp / "demo-root/.private/demo/cache")]]:
            c.fail("demo-send-caches", case, f"commands {commands}")
        if sent == [None]:
            c.fail("demo-send-utf8", case, "non-ASCII sample could not cross the pipe to jev.py")
            continue
        if code != 0 or len(sent) != 1:
            c.fail("demo-send-clean-exit", case, f"exit {code}, {len(sent)} sends")
            continue
        if not kept:
            c.fail("demo-send-sample-unchanged", case, "sample bytes changed")
        lines = [json.loads(x) for x in sent[0].splitlines()]
        if [r["state"]["messages"][0]["text"][:7] for r in lines] != ["café ✓ "] * len(raw):
            c.fail("demo-send-utf8", case, "non-ASCII text changed on the way to jev.py")
        if [r["id"] for r in lines] != [r["id"] for r in raw]:
            c.fail("redact-id-kept", case, "outgoing ids differ from sample ids")
        leaked = [part for _, _, part in SECRETS if part in json.dumps([r["state"] for r in lines])]
        if not opt_out and leaked:
            c.fail("demo-send-redacts-outgoing", case, leaked[:3])
        if opt_out and len(leaked) != len(SECRETS):
            c.fail("demo-opt-out-sends-raw", case, f"{len(leaked)} of {len(SECRETS)} values sent")
        if not opt_out and any(part in output for _, _, part in SECRETS):
            c.fail("redact-metadata-no-leak", case, output[:200])
        summary = c.last_summary
        if summary.get("classification_counts") != {"fine": len(raw)} \
                or summary.get("example_ids_by_category") != {"fine": [r["id"] for r in raw[:3]]} \
                or summary.get("confidence", {}).get("missing") != len(raw):
            c.fail("demo-send-summarize", case, f"summary {json.dumps(summary)[:200]}")
    for limit, count in itertools.product([-1, 0, 1, 2, 20, 21], [None, 0, 1, 2, 20, 21]):
        c.cases += 1
        records = None if count is None else [{"id": i, "state": "x"} for i in range(count)]
        code, sent, kept, _, _ = drive_demo(c, ["--send", "--limit", str(limit)], records)
        allowed = 1 <= limit <= 20 and count is not None and 1 <= count <= limit
        case = f"--limit {limit}, sample {count}"
        if (code == 0 and len(sent) == 1) != allowed or (not allowed and (code != 2 or sent)):
            c.fail("demo-send-gates", case, f"exit {code}, {len(sent)} sends")
        if not kept:
            c.fail("demo-send-sample-unchanged", case, "sample bytes changed")


# P6. Response cache: a hit iff the endpoint and request body match a stored success, never a failure.

CACHE_BASES = ["http://127.0.0.1:8080/a/", "http://127.0.0.1:8080/a", "http://127.0.0.1:8080/b"]
CACHE_MODELS = ["m1", "m2"]
CHOICE = {"type": "choice", "criteria": {"a": "A", "b": "B"}}
CACHE_QUESTIONS = [{"n": NOUL}, {"n": NOUL, "c": CHOICE}, {"c": CHOICE, "n": NOUL}]
CACHE_RECORDS = [
    '{"id":"a","state":"s"}',
    '{"id":["b"],"state":"s"}',
    '{"id":"a","state":"s","extra":1}',
    '{"id":"a","state":"t"}',
    json.dumps({"id": "a", "state": "s", "assets": ASSETS}),
]
CORRUPTIONS = {
    "empty": b"",
    "truncated": None,
    "not_utf8": b"\xff\xfe",
    "not_object": b"[]",
    "wrong_version": lambda e: {**e, "version": 2},
    "wrong_key": lambda e: {**e, "key": "0" * 64},
    "no_response": lambda e: {k: v for k, v in e.items() if k != "response"},
    "response_not_object": lambda e: {**e, "response": []},
    "response_no_answers": lambda e: {**e, "response": {"model": "m"}},
    "response_has_id": lambda e: {**e, "response": {**e["response"], "id": 1}},
    "nonfinite": lambda e: json.dumps({**e, "response": {**e["response"], "x": "NAN"}}).replace('"NAN"', "NaN"),
}


def request_identity(base, model, questions, raw):
    """The oracle's view of a request: endpoint plus body, independent of jev.py's key derivation."""
    record = json.loads(raw)
    body = {"model": model, "state": record["state"], "questions": questions}
    if "assets" in record:
        body["assets"] = record["assets"]
    return base.rstrip("/") + "/systemone", json.dumps(body)


def cache_entries(cache):
    return sorted(p for p in Path(cache).rglob("*") if p.is_file())


def check_cache(c, depth):
    responses = lambda i, r: json.dumps(ok_response(i)).encode()
    configs = list(itertools.product(CACHE_BASES, CACHE_MODELS, CACHE_QUESTIONS, CACHE_RECORDS))
    for first, second in itertools.product(configs, repeat=2):
        c.cases += 1
        cache = c.tmp / "cache-pair"
        shutil.rmtree(cache, ignore_errors=True)
        runs = []
        for (base, model, questions, raw), index in ((first, 0), (second, 5)):
            runs.append(drive(c.jev, ["classify", "--questions", c.questions_file(questions), "--base-url", base,
                                      "--model", model, "--cache", str(cache)], raw,
                              respond=lambda i, r, index=index: json.dumps(ok_response(index)).encode()))
        case = f"cache first={first} second={second}"
        if not all(c.clean_exit("cache-clean-exit", case, r) for r in runs):
            continue
        hit = request_identity(*first) == request_identity(*second)
        if (len(runs[0].requests), len(runs[1].requests)) != (1, 0 if hit else 1):
            c.fail("cache-hit-iff-same-request", case,
                   f"requests {len(runs[0].requests)},{len(runs[1].requests)}; expected 1,{0 if hit else 1}")
        expected = {"id": json.loads(second[3])["id"], **ok_response(0 if hit else 5)}
        if (runs[1].code, runs[1].stderr) != (0, "") or not same(json.loads(runs[1].stdout or "null"), expected):
            c.fail("cache-output-current-id", case, f"exit {runs[1].code} stdout {runs[1].stdout[:160]!r}")

    for mapping in CACHE_QUESTIONS:
        wrapped = {"schema_version": 1, "questions": mapping}
        for first, second in ((mapping, wrapped), (wrapped, mapping)):
            c.cases += 1
            cache = c.tmp / "cache-forms"
            shutil.rmtree(cache, ignore_errors=True)
            runs = [drive(c.jev, ["classify", "--questions", c.questions_file(doc), "--cache", str(cache)],
                          f'{{"id":{i},"state":"s"}}', respond=lambda r_i, r, i=i: json.dumps(ok_response(i)).encode())
                    for i, doc in enumerate((first, second))]
            case = f"cache forms first={json.dumps(first)[:60]} second={json.dumps(second)[:60]}"
            if not all(c.clean_exit("cache-clean-exit", case, r) for r in runs):
                continue
            if len(runs[1].requests) != 0 or not same(json.loads(runs[1].stdout or "null"), {"id": 1, **ok_response(0)}):
                c.fail("cache-hit-across-question-file-forms", case,
                       f"second run sent {len(runs[1].requests)}, stdout {runs[1].stdout[:120]!r}")

    questions = c.questions_file({"q": NOUL})
    for kind, (stage, line, responder) in LINE_KINDS.items():
        if stage != "net":
            continue
        c.cases += 1
        cache = c.tmp / "cache-failure"
        shutil.rmtree(cache, ignore_errors=True)
        argv = ["classify", "--questions", questions, "--cache", str(cache)]
        failed = drive(c.jev, argv, line(0), respond=responder)
        left = cache_entries(cache)
        retry = drive(c.jev, argv, line(0), respond=responses)
        if failed.code != 1 or left or len(retry.requests) != 1:
            c.fail("cache-never-stores-failure", f"cache failure={kind}",
                   f"exit {failed.code}, {len(left)} entries left, retry sent {len(retry.requests)}")

    for name, corrupt in CORRUPTIONS.items():
        c.cases += 1
        cache = c.tmp / "cache-corrupt"
        shutil.rmtree(cache, ignore_errors=True)
        argv = ["classify", "--questions", questions, "--cache", str(cache)]
        record = '{"id":"c","state":"x"}'
        drive(c.jev, argv, record, respond=responses)
        [entry] = cache_entries(cache)
        good = entry.read_bytes()
        stored = json.loads(good)
        damage = good[:len(good) // 2] if corrupt is None else corrupt if isinstance(corrupt, bytes) else corrupt(stored)
        entry.write_bytes(damage if isinstance(damage, bytes) else (damage if isinstance(damage, str) else json.dumps(damage)).encode())
        refetch = drive(c.jev, argv, record, respond=lambda i, r: json.dumps(ok_response(7)).encode())
        reuse = drive(c.jev, argv, record, respond=responses)
        case = f"cache corrupt={name}"
        if not (c.clean_exit("cache-clean-exit", case, refetch) and c.clean_exit("cache-clean-exit", case, reuse)):
            continue
        fresh = {"id": "c", **ok_response(7)}
        if (refetch.code, len(refetch.requests)) != (0, 1) or not same(json.loads(refetch.stdout or "null"), fresh):
            c.fail("cache-corrupt-refetch", case, f"exit {refetch.code}, {len(refetch.requests)} requests, stdout {refetch.stdout[:120]!r}")
        if len(reuse.requests) != 0 or not same(json.loads(reuse.stdout or "null"), fresh):
            c.fail("cache-corrupt-rewritten", case, f"third run sent {len(reuse.requests)}, stdout {reuse.stdout[:120]!r}")

    for n in range(1, depth + 1):
        for seq in itertools.product(LINE_KINDS, repeat=n):
            c.cases += 1
            cache = c.tmp / "cache-resume"
            shutil.rmtree(cache, ignore_errors=True)
            lines = [LINE_KINDS[kind][1](i) for i, kind in enumerate(seq)]
            stored = {}
            case = "cache resume " + ",".join(seq)
            for attempt in ("interrupted", "resumed"):
                sent = []

                def respond(index, request):
                    sent.append(index)
                    kind = pending[index] if attempt == "interrupted" else "ok"
                    responder = LINE_KINDS[kind][2]
                    return responder(index, request) if responder else json.dumps(ok_response(index)).encode()

                expected_out, pending = [], []
                for i, kind in enumerate(seq):
                    if LINE_KINDS[kind][0] == "local":
                        break
                    identity = request_identity("https://opencode.ai/zen/v1", "jev-1.13-free", {"q": NOUL}, lines[i])
                    if identity in stored:
                        expected_out.append({"id": json.loads(lines[i])["id"], **stored[identity]})
                        continue
                    pending.append(kind)
                    if attempt == "interrupted" and LINE_KINDS[kind][0] == "net":
                        break
                    stored[identity] = ok_response(len(pending) - 1)
                    expected_out.append({"id": json.loads(lines[i])["id"], **stored[identity]})
                run = drive(c.jev, ["batch", "--questions", questions, "--cache", str(cache)],
                            "\n".join(lines) + "\n", respond=respond)
                if not c.clean_exit("cache-clean-exit", case, run):
                    break
                got = [json.loads(x) for x in run.stdout.splitlines()]
                if len(run.requests) != len(pending):
                    c.fail("cache-resume-sends-only-missing", case, f"{attempt}: {len(run.requests)} requests, expected {len(pending)}")
                if not (len(got) == len(expected_out) and all(map(same, got, expected_out))):
                    c.fail("cache-resume-output", case, f"{attempt}: stdout {run.stdout[:200]!r}")

    for base, model, questions, raw in configs:
        c.cases += 1
        case = f"cache --dry-run base={base} model={model} questions={sorted(questions)} record={raw[:40]}"
        cache = c.tmp / "cache-dry"
        shutil.rmtree(cache, ignore_errors=True)
        argv = ["classify", "--questions", c.questions_file(questions), "--base-url", base, "--model", model]
        cold = drive(c.jev, [*argv, "--dry-run", "--cache", str(cache)], raw, respond=responses)
        created = cache.exists()
        drive(c.jev, [*argv, "--cache", str(cache)], raw, respond=responses)
        before = {p: p.read_bytes() for p in cache_entries(cache)}
        warm = drive(c.jev, [*argv, "--dry-run", "--cache", str(cache)], raw, respond=responses)
        plain = drive(c.jev, [*argv, "--dry-run"], raw, respond=responses)
        if not all(c.clean_exit("dry-run-clean-exit", case, r) for r in (cold, warm, plain)):
            continue
        after = {p: p.read_bytes() for p in cache_entries(cache)}
        if created or after != before or cold.requests or warm.requests \
                or (cold.code, cold.stdout) != (plain.code, plain.stdout) or (warm.code, warm.stdout) != (plain.code, plain.stdout):
            c.fail("dry-run-ignores-cache", case, f"cache created={created} changed={after != before}, "
                   f"stdout cold={cold.stdout[:60]!r} warm={warm.stdout[:60]!r} plain={plain.stdout[:60]!r}")


# P7. Eval: offline, exact report against an independent oracle, hard errors for ambiguous input.

EVAL_OPTIONS = ("a", "b")
EVAL_QUESTIONS = {"r": {"type": "choice", "criteria": {"a": "A", "b": "B"}}, "n": NOUL}
EVAL_IDS = ["x", 1]
EVAL_EXTRA_ID = "1"
ABSENT = object()
EVAL_CHOICES = ["a", "b", "c", ABSENT]
EVAL_CONFIDENCES = [ABSENT, 0.0999, 0.1, 0.9, 1.0, 1.5, True, "0.5"]
EVAL_EDGES = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


def eval_line(id, choice, confidence):
    answer = {"type": "choice"}
    if choice is not ABSENT:
        answer["choice"] = choice
    if confidence is not ABSENT:
        answer["confidence"] = confidence
    return json.dumps({"id": id, "model": "m", "answers": {"r": answer} if choice is not ABSENT else {}})


def eval_oracle(labels, predictions):
    """Plain-loop restatement of README's eval contract; predictions are (id, choice, confidence)."""
    by_id = {}
    for id, choice, confidence in predictions:
        by_id[(type(id), id)] = (choice, confidence)
    confusion = {t: {p: 0 for p in EVAL_OPTIONS} for t in EVAL_OPTIONS}
    missing = unknown = uncalibrated = 0
    bins = [[] for _ in EVAL_EDGES]
    for id, truth in labels:
        if (type(id), id) not in by_id:
            missing += 1
            continue
        choice, confidence = by_id[(type(id), id)]
        if choice not in EVAL_OPTIONS:
            unknown += 1
            continue
        confusion[truth][choice] += 1
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            uncalibrated += 1
            continue
        index = max(i for i, edge in enumerate(EVAL_EDGES) if confidence >= edge)
        bins[index].append((confidence, choice == truth))
    scored = sum(sum(row.values()) for row in confusion.values())
    correct = confusion["a"]["a"] + confusion["b"]["b"]
    labeled_keys = {(type(id), id) for id, _ in labels}
    highs = EVAL_EDGES[1:] + [1.0]
    return {
        "question": "r", "labels": list(EVAL_OPTIONS),
        "counts": {"labeled": len(labels), "scored": scored,
                   "unscored": {"missing_prediction": missing, "unknown_choice": unknown},
                   "extra_predictions": len(set(by_id) - labeled_keys),
                   "calibrated": scored - uncalibrated, "missing_confidence": uncalibrated},
        "correct": correct, "accuracy": correct / scored if scored else None,
        "confusion": confusion,
        "calibration": [{"low": lo, "high": hi, "count": len(rows),
                         "mean_confidence": sum(c for c, _ in rows) / len(rows) if rows else None,
                         "accuracy": sum(ok for _, ok in rows) / len(rows) if rows else None}
                        for lo, hi, rows in zip(EVAL_EDGES, highs, bins)],
    }


def drive_eval(c, labels_text, predictions_text, extra=("--question", "r"), questions=EVAL_QUESTIONS):
    path = c.tmp / "labels.jsonl"
    path.write_bytes(labels_text.encode("utf-8"))
    run = drive(c.jev, ["eval", "--questions", c.questions_file(questions), "--labels", str(path), *extra],
                predictions_text, EVAL_ENV, respond=lambda i, r: b'{"answers":{}}')
    return run, path.read_bytes() == labels_text.encode("utf-8")


def eval_common(c, prop_case, run, unchanged):
    if run.requests or run.key_reads:
        c.fail("eval-offline", prop_case, f"{len(run.requests)} requests, {run.key_reads} credential reads")
    if not unchanged:
        c.fail("eval-inputs-unchanged", prop_case, "labels file bytes changed")
    return c.clean_exit("eval-clean-exit", prop_case, run)


EVAL_ERRORS = [
    # (labels lines, prediction lines, extra argv, expected stderr prefix)
    ([], [], None, "jev: labels file has no records"),
    (['{"id":"x","label":"a"}', '{"id":"x","label":"b"}'], [], None, "jev: labels line 2: duplicate id"),
    (['{"id":1,"label":"a"}', '{"id":"x","label":"a"}', '{"id":1,"label":"a"}'], [], None,
     "jev: labels line 3: duplicate id"),
    (['{"id":"x","label":"c"}'], [], None, "jev: labels line 1: label must be one of the r options"),
    (['{"id":"x"}'], [], None, "jev: labels line 1: label must be one of the r options"),
    (['{"id":"x","label":["a"]}'], [], None, "jev: labels line 1: label must be one of the r options"),
    (['{"id":"x","label":"a"}', '{"label":"a"}'], [], None, "jev: labels line 2: expected an object with id"),
    (['["x","a"]'], [], None, "jev: labels line 1: expected an object with id"),
    (['{"id":"x","label":"a"}', ''], [], None, "jev: labels line 2: invalid JSON"),
    (['{"id":NaN,"label":"a"}'], [], None, "jev: labels line 1: invalid JSON"),
    (['{"id":"x","label":"a"}'], ['{"id":"x","answers":{}}', '{"id":"x","answers":{}}'], None,
     "jev: predictions line 2: duplicate id"),
    (['{"id":"x","label":"a"}'], ['{"id":"x","answers":[]}'], None, "jev: predictions line 1: expected answers object"),
    (['{"id":"x","label":"a"}'], ['{"id":"x"}'], None, "jev: predictions line 1: expected answers object"),
    (['{"id":"x","label":"a"}'], ['{"answers":{}}'], None, "jev: predictions line 1: expected an object with id"),
    (['{"id":"x","label":"a"}'], ['{"id":"y","answers":{}}', ''], None, "jev: predictions line 2: invalid JSON"),
    (['{"id":"x","label":"a"}'], ['{"id":"x","answers":{"r":{"confidence":1e999}}}'], None,
     "jev: predictions line 1: invalid JSON"),
    (['{"id":"x","label":"a"}'], [], (), "jev: eval needs --question naming a Choice question"),
    (['{"id":"x","label":"a"}'], [], ("--question", "n"), "jev: eval needs --question naming a Choice question"),
    (['{"id":"x","label":"a"}'], [], ("--question", "zz"), "jev: eval needs --question naming a Choice question"),
    (['{"id":"x","label":"a"}'], [], ("--question", "r", "--cache", "eval-cache-never-created"),
     "jev: eval sends no requests; --cache is for classify and batch"),
    (['{"id":"x","label":"a"}'], [], ("--question", "r", "--dry-run"),
     "jev: eval sends no requests; --dry-run is for classify and batch"),
]


def check_eval(c):
    label_states = [None, "a", "b"]
    prediction_states = [None] + list(itertools.product(EVAL_CHOICES, EVAL_CONFIDENCES))
    per_id = list(itertools.product(label_states, prediction_states))
    for combo in itertools.product(per_id, repeat=len(EVAL_IDS)):
        for with_extra in (False, True):
            c.cases += 1
            labels = [(id, lab) for id, (lab, _) in zip(EVAL_IDS, combo) if lab is not None]
            predictions = [(id, *pred) for id, (_, pred) in zip(EVAL_IDS, combo) if pred is not None]
            if with_extra:
                predictions.append((EVAL_EXTRA_ID, "a", 0.5))
            labels_text = "".join(json.dumps({"id": id, "label": lab}) + "\n" for id, lab in labels)
            predictions_text = "".join(eval_line(*p) + "\n" for p in predictions)
            case = f"eval labels={labels} predictions={[(i, '-' if ch is ABSENT else ch, '-' if cf is ABSENT else cf) for i, ch, cf in predictions]}"
            run, unchanged = drive_eval(c, labels_text, predictions_text)
            if not eval_common(c, case, run, unchanged):
                continue
            if not labels:
                if run.code != 1 or run.stdout or run.stderr != "jev: labels file has no records\n":
                    c.fail("eval-errors", case, f"exit {run.code} stdout {run.stdout[:80]!r} stderr {run.stderr!r}")
                continue
            if run.code != 0 or run.stderr:
                c.fail("eval-report", case, f"exit {run.code} stderr {run.stderr!r}")
                continue
            if not same(json.loads(run.stdout), eval_oracle(labels, predictions)):
                c.fail("eval-report", case, f"stdout {run.stdout[:300]!r}")
    for labels, predictions, extra, expected in EVAL_ERRORS:
        c.cases += 1
        case = f"eval-error labels={labels} predictions={predictions} argv={extra}"
        run, unchanged = drive_eval(c, "".join(x + "\n" for x in labels), "".join(x + "\n" for x in predictions),
                                    *(() if extra is None else (extra,)))
        if not eval_common(c, case, run, unchanged):
            continue
        if run.code != 1 or run.stdout or not run.stderr.startswith(expected) or run.stderr.count("\n") != 1:
            c.fail("eval-errors", case, f"exit {run.code} stdout {run.stdout[:80]!r} stderr {run.stderr!r}")


def check_question_file_dry_run(c, case, command, document, real):
    c.cases += 1
    case = case.replace("questions ", "questions --dry-run ", 1)
    run = drive(c.jev, [command, "--dry-run", "--questions", c.questions_file(document)], '{"id":"q","state":"x"}\n',
                respond=lambda i, r: json.dumps(ok_response(i)).encode())
    if not c.clean_exit("dry-run-clean-exit", case, run):
        return
    if run.requests or (run.code, run.stderr) != (real.code, real.stderr) or (run.code != 0 and run.stdout):
        c.fail("dry-run-questions-like-send", case, f"dry exit {run.code} {run.stderr!r}, send exit {real.code} {real.stderr!r}")
        return
    if run.code == 0:
        row, = json.loads(run.stdout)["records"]
        questions = json.loads(real.requests[0]["request"].data)["questions"]
        if row["request_bytes"] != len(real.requests[0]["request"].data) \
                or row["question_tokens_estimate"] != readme_estimate(questions):
            c.fail("dry-run-questions-like-send", case, f"row {row} vs sent body of {len(real.requests[0]['request'].data)} bytes")


# Broken variants of jev.py. Each must make the named property fail, or the check proves nothing.

MUTANTS = [
    ("batch keeps going after a failure", "batch-prefix",
     [("                    return 1\n                if args.dry_run:", "                    continue\n                if args.dry_run:")]),
    ("batch output not flushed per line", "batch-flush-before-next",
     [("allow_nan=False), flush=True)", "allow_nan=False))")]),
    ("TypeSafe origin matched by substring", "auth-only-to-typesafe",
     [('url.netloc.lower() == "api.typesafe.ai"', '"typesafe.ai" in url.netloc.lower()')]),
    ("whole record sent as state", "request-body",
     [('"state": record["state"], "questions"', '"state": record, "questions"')]),
    ("probabilities rounded on read", "numbers-passthrough-exact",
     [("    value = json.loads(text)\n", "    value = json.loads(text, parse_float=lambda s: round(float(s), 6))\n")]),
    ("nonfinite read check dropped", "batch-exit",
     [("    json.dumps(value, allow_nan=False)\n    return value", "    return value")]),
    ("nonfinite allowed everywhere", "numbers-nonfinite-rejected",
     [("    json.dumps(value, allow_nan=False)\n    return value", "    return value"),
      ("allow_nan=False", "allow_nan=True")]),
    ("images header only for assets", "images-header-iff-images",
     [('    if uses_images(payload):\n        headers', '    if "assets" in payload:\n        headers')]),
    ("free backend accepts Box", "box-rejected-locally-on-free",
     [('        return not (urlsplit(self.base_url).hostname == "opencode.ai"',
       '        return True or not (urlsplit(self.base_url).hostname == "opencode.ai"')]),
    ("wrapper never unwrapped", "questions-accept-iff-valid",
     [('        document = document["questions"]\n', "")]),
    ("schema_version leaked into the request", "questions-unwrap-exact",
     [('"questions": questions}\n    if "assets" in record:', '"questions": questions, "schema_version": 1}\n    if "assets" in record:')]),
    ("unknown schema_version accepted", "questions-accept-iff-valid",
     [("if type(version) is not int or version not in SCHEMA_VERSIONS:", "if type(version) is not int:")]),
    ("bool schema_version accepted", "questions-accept-iff-valid",
     [("if type(version) is not int or version not in SCHEMA_VERSIONS:",
       "if not isinstance(version, int) or version not in SCHEMA_VERSIONS:")]),
    ("extra wrapper keys allowed", "questions-accept-iff-valid",
     [('if set(document) != {"schema_version", "questions"}:', 'if "questions" not in document:')]),
    ("wrapper error echoes keys", "questions-reject-no-echo",
     [('raise JevError("versioned questions document must contain exactly schema_version and questions")',
       'raise JevError("versioned questions document has keys: " + ", ".join(document))')]),
    ("bad questions JSON gets the generic error", "questions-reject-message",
     [("    except (OSError, ValueError, RecursionError):\n        raise JevError(\"questions file",
       "    except OSError:\n        raise JevError(\"questions file")]),
    ("record id part of cache key", "cache-hit-iff-same-request",
     [("cache.path(backend.endpoint, body)", 'cache.path(backend.endpoint, body + repr(record["id"]).encode())')]),
    ("endpoint left out of cache key", "cache-hit-iff-same-request",
     [('f"jev-cache-v{CACHE_VERSION}\\n{endpoint}\\n"', 'f"jev-cache-v{CACHE_VERSION}\\n"')]),
    ("cached response returned with stored id", "cache-output-current-id",
     [('return {"id": record["id"], **cached}', 'return {**cached, "id": "cached"}')]),
    ("failure stored and trusted", "cache-never-stores-failure",
     [('            result = read_json(response.read().decode("utf-8"))\n',
       '            result = read_json(response.read().decode("utf-8"))\n            cache and cache.store(entry, result)\n'),
      ('return entry["response"] if valid_response(entry.get("response")) else None', 'return entry.get("response")')]),
    ("corrupt entry raises", "cache-corrupt-refetch",
     [("        except (OSError, ValueError, UnicodeError, RecursionError):\n            return None",
       "        except FileNotFoundError:\n            return None")]),
    ("entry fields not validated", "cache-corrupt-refetch",
     [('entry.get("version") != CACHE_VERSION or entry.get("key") != path.stem', "False")]),
    ("cache keyed per questions file", "cache-hit-across-question-file-forms",
     [("cache = Cache(args.cache) if args.cache else None", "cache = Cache(args.cache / args.questions.name) if args.cache else None")]),
    ("cache never read", "cache-resume-sends-only-missing",
     [("cached = cache.load(entry) if entry else None", "cached = None")]),
    ("eval reads credentials", "eval-offline",
     [('        if args.command == "eval":\n            if not args.labels:',
       '        if args.command == "eval":\n            api_key()\n            if not args.labels:')]),
    ("calibration bins high-inclusive", "eval-report",
     [("bins[bisect_right(CALIBRATION_EDGES, confidence)]", "bins[bisect_left(CALIBRATION_EDGES, confidence)]"),
      ("from bisect import bisect_right", "from bisect import bisect_left")]),
    ("out-of-range confidence calibrated", "eval-report",
     [("or not 0 <= confidence <= 1:", "or False:")]),
    ("confusion transposed", "eval-report",
     [("confusion[truth][choice] += 1", "confusion[choice][truth] += 1")]),
    ("duplicate label ids accepted", "eval-errors",
     [('        if key in labels:\n            raise JevError(f"labels line {number}: duplicate id")\n',
       '        if False:\n            raise JevError(f"labels line {number}: duplicate id")\n')]),
    ("duplicate prediction ids accepted", "eval-errors",
     [('        if id_key(record["id"]) in predictions:', '        if False:')]),
    ("eval rereads the raw questions file", "questions-eval-same-loader",
     [('        if args.command == "eval":\n            if not args.labels:',
       '        if args.command == "eval":\n            questions = read_json(args.questions.read_text(encoding="utf-8"))\n'
       '            if not args.labels:')]),
    ("eval accepts --cache and ignores it", "eval-errors",
     [('        if offline and args.cache:\n', '        if False:\n')]),
    ("eval writes the labels file", "eval-inputs-unchanged",
     [('            with args.labels.open(encoding="utf-8") as labels:',
       '            args.labels.write_text(args.labels.read_text(encoding="utf-8") + " ", encoding="utf-8")\n'
       '            with args.labels.open(encoding="utf-8") as labels:')]),
    ("dry run reads credentials", "dry-run-no-network-no-credentials",
     [("api_key() if backend.typesafe and not args.dry_run else None", "api_key() if backend.typesafe else None")]),
    ("dry run also sends", "dry-run-no-network-no-credentials",
     [("                return estimate(record, questions, backend)",
       "                return [classify(record, questions, key, backend, args.timeout), "
       "estimate(record, questions, backend)][1]")]),
    ("dry run skips record validation", "dry-run-validates-like-send",
     [("    payload = request_payload(record, questions, backend)\n    text = ",
       "    payload = dict(record, questions=questions)\n    text = ")]),
    ("dry run prices unknown backends at zero", "dry-run-cost",
     [("return PRICES.get((host, self.model))", 'return PRICES.get((host, self.model), ("0", None))')]),
    ("dry run counts image assets as text", "dry-run-tokens",
     [('payload.items() if key != "assets"}', "payload.items()}")]),
    ("dry run measures unescaped JSON", "dry-run-measures-sent-bytes",
     [('"request_bytes": len(request_body(payload))',
       '"request_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))')]),
    ("dry run measures the questions wrapper", "dry-run-questions-like-send",
     [('"question_tokens_estimate": estimate_tokens(questions)',
       '"question_tokens_estimate": estimate_tokens({"schema_version": 1, "questions": questions})')]),
    ("dry run writes the cache", "dry-run-ignores-cache",
     [("                return estimate(record, questions, backend)",
       "                cache and cache.store(cache.path(backend.endpoint, request_body(request_payload(record, questions, backend))), {\"answers\": {}})\n"
       "                return estimate(record, questions, backend)")]),
    ("eval accepts --dry-run and ignores it", "eval-errors",
     [('        if offline and args.dry_run:\n', '        if False:\n')]),
    ("dry run total drops a batch row", "dry-run-total-is-sum",
     [('total = sum(row["input_tokens_estimate"] for row in rows)',
       'total = sum(row["input_tokens_estimate"] for row in rows[1:]) or rows[0]["input_tokens_estimate"]')]),
    ("lower bin edge excluded", "summarize-bins",
     [("bins[bin_names[bisect_right(CALIBRATION_EDGES, value)]]", "bins[bin_names[bisect_left(CALIBRATION_EDGES, value)]]"),
      ("from bisect import bisect_right\n", "from bisect import bisect_left, bisect_right\n")]),
    ("boolean confidence binned as a number", "summarize-confidence-classes",
     [("if isinstance(value, bool) or not isinstance(value, (int, float)):", "if not isinstance(value, (int, float)):")]),
    ("example IDs unbounded", "summarize-examples", [("        if len(bucket) < 3:\n", "        if True:\n")]),
    ("labels seeded from the --question flag, not the selected question", "summarize-counts",
     [("def summarize(lines, question=None, questions=None):\n", "def summarize(lines, question=None, questions=None):\n    asked = question\n"),
      ("    if questions and question in questions:\n", "    if questions and asked in questions:\n")]),
    ("summarize parses --questions without load_questions", "summarize-counts",
     [("        questions = load_questions(args.questions)\n        if question is None",
       "        questions = read_json(args.questions.read_text(encoding=\"utf-8\"))\n        if question is None")]),
    ("summarize treated as a sending command", "summarize-flag-errors",
     [('offline = args.command in ("eval", "summarize")', 'offline = args.command == "eval"')]),
    ("summarize reads credentials", "summarize-offline",
     [("            return run_summarize(args)", "            api_key()\n            return run_summarize(args)")]),
]

# Broken variants of scripts/demo_sessions.py, checked the same way.
DEMO_MUTANTS = [
    ("demo send without the cache", "demo-send-caches",
     [(', "--cache", str(folder / "cache")]', "]")]),
    ("summarize pipe not pinned to UTF-8", "demo-send-clean-exit",
     [('capture_output=True, check=True,\n                                 encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})',
       'capture_output=True, check=True, text=True)')]),
    ("demo drops summarize example IDs", "demo-send-summarize",
     [('summary["example_ids_by_category"] = counts["examples"]', 'summary["example_ids_by_category"] = {}')]),
    ("send skips the second redaction pass", "demo-send-redacts-outgoing",
     [('outgoing, summary["redaction"] = redact_records(records)',
       'outgoing, summary["redaction"] = records, {"policy": REDACTION_POLICY, "counts": {}}')]),
    ("redaction after the 4000-character cap", "redact-before-cap",
     [("                        text = redact(text, Counter())\n", "                        pass\n"),
      ("                    text = text[:4000]\n", "                    text = redact(text[:4000], Counter())\n")]),
    ("counts include matches past the cap", "redact-counts-kept",
     [("text = redact(text, Counter())", "text = redact(text, early := Counter())"),
      ("hits = Counter(RULE_BY_LABEL[label] for label in PLACEHOLDER.findall(text)) if counts", "hits = early if counts")]),
    ("send pipe left to the locale encoding", "demo-send-utf8",
     [('encoding="utf-8",', "text=True,")]),
    ("jev.py stdio left to the locale encoding", "demo-send-utf8",
     [('env={**os.environ, "PYTHONIOENCODING": "utf-8"}', "env=os.environ")]),
    ("arrays not recursed", "redact-no-fixture-survives",
     [("        return [redact(item, counts) for item in value]", "        return value")]),
    ("record id redacted", "redact-id-kept",
     [('item if key == "id" else redact(item, counts)', 'redact(item, counts)')]),
    ("placeholder re-matched", "redact-idempotent",
     [(r"(?!\[REDACTED)", "")]),
    ("home path boundary dropped", "redact-benign-unchanged",
     [(r"(?<![\w.:/\\])(?:/Users", "(?:/Users")]),
    ("limit cap raised", "demo-send-gates",
     [("if not 1 <= args.limit <= 20:", "if not 1 <= args.limit <= 21:")]),
]

# Violations the current code produces. Each stays listed until the code is fixed; the check fails if one disappears.
KNOWN_FINDINGS = {
    "F1 control character in --base-url crashes with a traceback after reading credentials":
        lambda prop, case: prop in ("request-clean-exit", "credential-read-only-for-typesafe")
        and ("\\n" in case or "\\t" in case),
    "F2 batch blames the record for API response failures":
        lambda prop, case: prop == "batch-blames-right-party"
        and any(k in case for k in ("resp_not_json", "resp_not_utf8", "resp_infinity")),
    "F3 control character in --base-url gets a TypeSafe dry-run price for a non-TypeSafe urllib target":
        lambda prop, case: prop == "dry-run-cost" and ("\\n" in case or "\\t" in case),
}


def run_all(jev, demo, depth, only=None):
    with tempfile.TemporaryDirectory() as tmp:
        c = Checker(jev, tmp, demo)
        for name, fn in (("batch", lambda: check_batch(c, depth)), ("requests", lambda: check_requests(c)),
                         ("numbers", lambda: check_numbers(c)), ("questions", lambda: check_question_files(c)),
                         ("redaction", lambda: (check_redaction(c), check_redact_before_cap(c), check_demo_send(c))),
                         ("cache", lambda: check_cache(c, min(depth, 2))),
                         ("eval", lambda: check_eval(c)),
                         ("summarize", lambda: check_summarize(c, min(depth, 2)))):
            if only in (None, name):
                fn()
        return c


SECTIONS = {"batch": "batch", "numbers": "numbers", "questions": "questions", "cache": "cache", "eval": "eval",
            "summarize": "summarize", "redact": "redaction", "demo": "redaction"}


# Properties checked outside the section their prefix names; a mutant must run where its property is checked.
PROPERTY_SECTIONS = {"dry-run-questions-like-send": "questions", "dry-run-ignores-cache": "cache",
                     "dry-run-total-is-sum": "batch", "dry-run-batch-exit": "batch", "dry-run-batch-report": "batch"}


def section_of(prop):
    return PROPERTY_SECTIONS.get(prop) or SECTIONS.get(prop.split("-", 1)[0], "requests")


def triage(violations):
    known = {name: 0 for name in KNOWN_FINDINGS}
    new = []
    for prop, case, detail in violations:
        match = next((name for name, hit in KNOWN_FINDINGS.items() if hit(prop, case)), None)
        if match:
            known[match] += 1
        else:
            new.append((prop, case, detail))
    return known, new


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True).stdout.strip()


def stale():
    changed = 0
    for path in COVERED:
        synced, current = SYNC_BLOBS.get(path), git("hash-object", path)
        if synced == current:
            print(f"in sync  {path} {current[:10]}")
            continue
        changed += 1
        since = synced and git("log", "-1", "--format=%h", f"--find-object={synced}", "--all", "--", path)
        print(f"CHANGED  {path} {(synced or 'unrecorded')[:10]} -> {current[:10]}"
              + (f"; diff with: git diff {since} -- {path}" if since else "; synced blob not in history"))
    return 1 if changed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depth", type=int, default=3, help="max batch length to enumerate (default 3)")
    parser.add_argument("--only", choices=["batch", "requests", "numbers", "questions", "redaction", "cache", "eval", "summarize"])
    parser.add_argument("--mutants", action="store_true", help="also prove each property fails on a broken jev.py")
    parser.add_argument("--stale", action="store_true", help="only report covered files whose content differs from SYNC_BLOBS")
    args = parser.parse_args()
    if args.stale:
        return stale()

    source = TARGET.read_text(encoding="utf-8")
    demo_source = DEMO.read_text(encoding="utf-8")
    started = time.monotonic()
    c = run_all(load(source), load(demo_source, DEMO), args.depth, args.only)
    known, new = triage(c.violations)
    print(f"{c.cases} cases, depth {args.depth}, {time.monotonic() - started:.1f}s")
    code = 0
    for name, hits in known.items():
        if hits:
            print(f"KNOWN  {name} ({hits} cases)")
        elif args.only is None:
            print(f"FIXED? {name} no longer reproduces; remove it from KNOWN_FINDINGS")
            code = 1
    seen = set()
    for prop, case, detail in new:
        if prop not in seen:
            seen.add(prop)
            print(f"FAIL   {prop}: {case}\n       {detail}")
    if new:
        print(f"{len(new)} new violations across {len(seen)} properties")
        code = 1

    if args.mutants:
        suites = [(TARGET, source, MUTANTS, lambda m: (load(m), load(demo_source, DEMO))),
                  (DEMO, demo_source, DEMO_MUTANTS, lambda m: (load(source), load(m, DEMO)))]
        for path, original, mutants, modules in suites:
            for label, prop, edits in mutants:
                mutated = original
                for old, new_text in edits:
                    if old not in mutated:
                        break
                    mutated = mutated.replace(old, new_text)
                else:
                    jev, demo = modules(mutated)
                    caught = any(p == prop for p, _, _ in run_all(jev, demo, min(args.depth, 2), section_of(prop)).violations)
                    print(f"MUTANT {'caught' if caught else 'MISSED'}  {label} -> {prop}")
                    code = code if caught else 1
                    continue
                print(f"MUTANT STALE  {label}: anchor {old!r} not in {path.name}; update the mutant list")
                code = 1
    print("PASS" if code == 0 else "CHECK FAILED")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
