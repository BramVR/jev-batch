#!/usr/bin/env python3
"""Drive a fresh copy of jev.py as a user would and keep the evidence. Stdlib only."""

import argparse
from dataclasses import dataclass, field
import datetime
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
STUB_RESPONSE = ('{"model":"stub-model","answers":{"topic":{"type":"choice","choice":"bug",'
                 '"confidence":0.30000000000000004,"probabilities":{"billing":0.1,"bug":0.30000000000000004,'
                 '"other":0.6}}},"usage":{"input_tokens":12,"output_tokens":3},"future_field":[1e-7,-0.0]}')


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


# Stub System One: a real HTTP server on 127.0.0.1 reached through the user-facing --base-url.

class Stub:
    def __init__(self):
        self.queue, self.seen = [], []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                stub.seen.append({"method": "POST", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                  "body": json.loads(raw)})
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
    return [json.loads(x) for x in out.splitlines()]


def paths(reqs):
    return [r["path"] for r in reqs]


def journeys(stub_url):
    base = stub_url + "/zen/v1/"
    stub_args = ["--base-url", base]
    response = json.loads(STUB_RESPONSE)
    return {
        "cli-surface": [
            Step("help", ["--help"], stdout=lambda o: "classify" in o and "--questions" in o),
            Step("missing questions is a usage error", ["classify"], code=2, stderr="--questions"),
            Step("nonfinite timeout rejected", ["classify", "--questions", "noul.json", "--timeout", "inf"],
                 code=1, stderr="jev: timeout must be positive and finite"),
            Step("batch refuses --input", ["batch", "--questions", "noul.json", "--input", "record.json"],
                 code=1, stderr="jev: batch reads JSONL from stdin"),
            Step("empty question mapping rejected", ["classify", "--questions", "empty.json"],
                 '{"id":1,"state":"x"}', code=1, stderr="jev: questions must be a nonempty"),
            Step("unknown question type rejected", ["classify", "--questions", "weird.json"],
                 '{"id":1,"state":"x"}', code=1, stderr="jev: supported question types"),
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
                 '{"id":"a","state":"one"}\n{"id":"a","state":"two"}\n{"id":3,"state":["three"]}\n',
                 stub=[ok(), ok(), ok()],
                 stdout=lambda o: [x["id"] for x in lines_json(o)] == ["a", "a", 3]
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
    }


def probability_map_ok(answer, labels):
    probs = answer.get("probabilities", {})
    values = list(probs.values())
    return (set(probs) == set(labels) and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in values)
            and abs(sum(values) - 1) < 0.01 and answer.get("choice") in labels)


LIVE = [
    Step("live classify noul", ["classify", "--questions", "noul.v1.json"],
         '{"id":"verify-live-1","state":"I was charged twice for my subscription this month."}',
         stdout=lambda o: lines_json(o)[0]["id"] == "verify-live-1"
         and 0 <= lines_json(o)[0]["answers"]["billing"]["noul"] <= 1, timeout=90),
    Step("live batch choice", ["batch", "--questions", "choice.json"],
         '{"id":"verify-live-2","state":"The app crashes when I press save."}\n'
         '{"id":"verify-live-3","state":"Please refund the duplicate charge on my card."}\n',
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
    subprocess.run([sys.executable, "-m", "compileall", "-q", "jev.py"], cwd=target, check=True)
    return target


def write_fixtures(work):
    work.mkdir(parents=True, exist_ok=True)
    for name, value in {"noul.json": NOUL, "choice.json": CHOICE, "box.json": BOX, "empty.json": {},
                        "weird.json": {"q": {"type": "essay"}},
                        "noul.v1.json": {"schema_version": 1, "questions": NOUL},
                        "v2.json": {"schema_version": 2, "questions": NOUL},
                        "extra.v1.json": {"schema_version": 1, "questions": NOUL, "note": SECRET_BODY},
                        "empty.v1.json": {"schema_version": 1, "questions": {}},
                        "broken.json": '{"billing": {"type": "noul"',
                        "record.json": {"id": {"ticket": 7}, "state": {"subject": "App crashes on save"}}}.items():
        (work / name).write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def execute(step, build_dir, work, stub):
    stub.queue[:] = list(step.stub)
    stub.seen.clear()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(work), "LANG": "C.UTF-8", **step.env}
    # Fixture files live in `work`; the CLI runs from there as `python3 <build>/jev.py ...`.
    cmd = [sys.executable, str(build_dir / "jev.py"), *step.argv]
    started = time.monotonic()
    proc = subprocess.run(cmd, input=step.stdin, capture_output=True, text=True, cwd=work, env=env,
                          timeout=step.timeout)
    elapsed = time.monotonic() - started
    time.sleep(0.05)
    seen = list(stub.seen)
    problems = []
    if proc.returncode != step.code:
        problems.append(f"exit {proc.returncode}, expected {step.code}")
    if step.stderr and step.stderr not in proc.stderr:
        problems.append(f"stderr lacks {step.stderr!r}")
    if step.code == 0 and proc.stderr:
        problems.append("stderr not empty on success")
    if "Traceback" in proc.stderr or SECRET_BODY in proc.stderr + proc.stdout or SENTINEL_KEY in proc.stdout:
        problems.append("traceback, private body, or key leaked to output")
    for label, check, value in (("stdout", step.stdout, proc.stdout), ("requests", step.requests, seen)):
        try:
            if check and not check(value):
                problems.append(f"{label} check failed")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            problems.append(f"{label} check raised {type(exc).__name__}: {exc}")
    shown = " ".join(["python3", "jev.py", *step.argv])
    return {"step": step.name, "command": shown, "stdin": step.stdin, "env": sorted(step.env),
            "exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "seconds": round(elapsed, 2),
            "stub_requests": [{k: r[k] for k in ("path", "body")} | {"headers": {
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
    run.add_argument("journeys", nargs="*", help="cli-surface classify batch backend-select live-opencode")
    run.add_argument("--live", action="store_true", help="add live-opencode: 3 tiny synthetic requests to OpenCode")
    run.add_argument("--ref", default="worktree", help="worktree (default) or a git ref such as HEAD")
    run.add_argument("--keep-build", action="store_true")
    sub.add_parser("doctor", help="read-only readiness check")
    sub.add_parser("cleanup", help="remove build/work scratch, keep evidence")
    args = parser.parse_args()
    return {"run": cmd_run, "doctor": cmd_doctor, "cleanup": cmd_cleanup}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
