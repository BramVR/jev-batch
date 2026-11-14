#!/usr/bin/env python3
"""Bounded exhaustive contract checks for jev.py. Stdlib only, no network."""

import argparse
import contextlib
from dataclasses import dataclass, field
import http.client
import io
import itertools
import json
import os
from pathlib import Path
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
COVERED = ["jev.py"]
SYNC_COMMIT = "f5e59fc"
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


def load(source):
    module = types.ModuleType("jev")
    module.__file__ = str(TARGET)
    exec(compile(source, str(TARGET), "exec"), module.__dict__)
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
    def __init__(self, jev, tmp):
        self.jev = jev
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


def run_all(jev, depth, only=None):
    with tempfile.TemporaryDirectory() as tmp:
        c = Checker(jev, tmp)
        for name, fn in (("batch", lambda: check_batch(c, depth)), ("requests", lambda: check_requests(c)),
                         ("numbers", lambda: check_numbers(c)), ("questions", lambda: check_question_files(c))):
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


def stale():
    changed = subprocess.run(["git", "-C", str(ROOT), "diff", "--stat", SYNC_COMMIT, "--", *COVERED],
                             capture_output=True, text=True, check=True).stdout.strip()
    print(f"sync commit {SYNC_COMMIT}; covered files: {', '.join(COVERED)}")
    print(changed or "covered files unchanged since sync commit")
    return 1 if changed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depth", type=int, default=3, help="max batch length to enumerate (default 3)")
    parser.add_argument("--only", choices=["batch", "requests", "numbers", "questions"])
    parser.add_argument("--mutants", action="store_true", help="also prove each property fails on a broken jev.py")
    parser.add_argument("--stale", action="store_true", help="only report covered-file changes since SYNC_COMMIT")
    args = parser.parse_args()
    if args.stale:
        return stale()

    source = TARGET.read_text(encoding="utf-8")
    started = time.monotonic()
    c = run_all(load(source), args.depth, args.only)
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
        for label, prop, edits in MUTANTS:
            mutated = source
            for old, new_text in edits:
                if old not in mutated:
                    break
                mutated = mutated.replace(old, new_text)
            else:
                mc = run_all(load(mutated), min(args.depth, 2))
                caught = any(p == prop for p, _, _ in mc.violations)
                print(f"MUTANT {'caught' if caught else 'MISSED'}  {label} -> {prop}")
                code = code if caught else 1
                continue
            print(f"MUTANT STALE  {label}: anchor {old!r} not in jev.py; update MUTANTS")
            code = 1
    print("PASS" if code == 0 else "CHECK FAILED")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
