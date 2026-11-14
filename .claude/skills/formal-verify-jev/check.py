#!/usr/bin/env python3
"""Bounded exhaustive contract checks for jev.py and demo redaction. Stdlib only, no network."""

import argparse
from collections import Counter
import contextlib
from dataclasses import dataclass, field
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
    "jev.py": "654da228ef38acbc73073c997554abd46d4a3066",
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
    for (label, document), command in itertools.product(question_documents(), ("classify", "batch")):
        c.cases += 1
        case = f"questions {command} {label}"
        run = drive(c.jev, [command, "--questions", c.questions_file(document)], '{"id":"q","state":"x"}\n',
                    respond=lambda i, r: json.dumps(ok_response(i)).encode())
        if not c.clean_exit("questions-clean-exit", case, run):
            continue
        expected = None if isinstance(document, bytes) else expected_questions(document)
        if (run.code == 0) != (expected is not None):
            c.fail("questions-accept-iff-valid", case, f"exit {run.code} stderr {run.stderr[:120]!r}")
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
    (root / "examples/failure-patterns.json").write_text(json.dumps({"failure_pattern": {"criteria": {"fine": "F"}}}))
    sample = folder / "sample.jsonl"
    if sample_lines is None:
        sample.unlink(missing_ok=True)
    else:
        sample.write_text("".join(json.dumps(r) + "\n" for r in sample_lines))
    before = sample.read_bytes() if sample.exists() else None
    sent, commands = [], []

    def send(command, input=None, stdout=None, encoding=None, env=None, **kwargs):
        commands.append(command)
        # Model a hostile locale: anything not pinned to UTF-8 on either side of the pipe is ASCII.
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
    return code, sent, before == after, out.getvalue() + err.getvalue() + summary, commands


def check_demo_send(c):
    raw = [{"id": f"user{i}@example.test", "state": {"messages": [{"role": "user", "text": "café ✓ " + t}]}}
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


# Broken variants of jev.py. Each must make the named property fail, or the check proves nothing.

MUTANTS = [
    ("batch keeps going after a failure", "batch-prefix",
     [("                    return 1\n                print(", "                    continue\n                print(")]),
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
     [('if "assets" in record or any(q["type"] == "box" for q in questions.values()):', 'if "assets" in record:')]),
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
]

# Broken variants of scripts/demo_sessions.py, checked the same way.
DEMO_MUTANTS = [
    ("demo send without the cache", "demo-send-caches",
     [(', "--cache", str(folder / "cache")]', "]")]),
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
}


def run_all(jev, demo, depth, only=None):
    with tempfile.TemporaryDirectory() as tmp:
        c = Checker(jev, tmp, demo)
        for name, fn in (("batch", lambda: check_batch(c, depth)), ("requests", lambda: check_requests(c)),
                         ("numbers", lambda: check_numbers(c)), ("questions", lambda: check_question_files(c)),
                         ("redaction", lambda: (check_redaction(c), check_redact_before_cap(c), check_demo_send(c))),
                         ("cache", lambda: check_cache(c, min(depth, 2)))):
            if only in (None, name):
                fn()
        return c


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
    parser.add_argument("--only", choices=["batch", "requests", "numbers", "questions", "redaction", "cache"])
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
        suites = [(TARGET, source, MUTANTS, "batch requests numbers questions cache", lambda m: (load(m), load(demo_source, DEMO))),
                  (DEMO, demo_source, DEMO_MUTANTS, "redaction", lambda m: (load(source), load(m, DEMO)))]
        for path, original, mutants, scope, modules in suites:
            for label, prop, edits in mutants:
                mutated = original
                for old, new_text in edits:
                    if old not in mutated:
                        break
                    mutated = mutated.replace(old, new_text)
                else:
                    jev, demo = modules(mutated)
                    caught = any(any(p == prop for p, _, _ in run_all(jev, demo, min(args.depth, 2), only).violations)
                                 for only in scope.split())
                    print(f"MUTANT {'caught' if caught else 'MISSED'}  {label} -> {prop}")
                    code = code if caught else 1
                    continue
                print(f"MUTANT STALE  {label}: anchor {old!r} not in {path.name}; update the mutant list")
                code = 1
    print("PASS" if code == 0 else "CHECK FAILED")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
