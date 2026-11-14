#!/usr/bin/env python3
"""Small stdlib client for Jev System One."""

import argparse
from bisect import bisect_right
import contextlib
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "jev-1.13-free"
SCHEMA_VERSIONS = (1,)
CALIBRATION_EDGES = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)


@dataclass(frozen=True)
class Backend:
    base_url: str
    model: str

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise JevError("base URL must be an HTTP(S) URL without credentials, query, or fragment")
        if not self.model.strip():
            raise JevError("model must not be empty")

    @property
    def endpoint(self):
        return self.base_url.rstrip("/") + "/systemone"

    @property
    def typesafe(self):
        url = urlsplit(self.base_url)
        return url.scheme == "https" and url.netloc.lower() == "api.typesafe.ai"

    @property
    def supports_box(self):
        return not (urlsplit(self.base_url).hostname == "opencode.ai" or "1.13" in self.model or self.model.endswith("-free"))


class JevError(Exception):
    pass


CACHE_VERSION = 1


@dataclass(frozen=True)
class Cache:
    """Validated System One responses keyed by endpoint and exact request body."""

    root: Path

    def path(self, endpoint, body):
        digest = hashlib.sha256(f"jev-cache-v{CACHE_VERSION}\n{endpoint}\n".encode("utf-8") + body).hexdigest()
        return self.root / f"v{CACHE_VERSION}" / digest[:2] / (digest + ".json")

    def load(self, path):
        try:
            entry = read_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError, RecursionError):
            return None
        if not isinstance(entry, dict) or entry.get("version") != CACHE_VERSION or entry.get("key") != path.stem:
            return None
        return entry["response"] if valid_response(entry.get("response")) else None

    def store(self, path, response):
        entry = json.dumps({"version": CACHE_VERSION, "key": path.stem, "response": response},
                           ensure_ascii=False, allow_nan=False)
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(entry)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp)
                raise
        except OSError:
            # The request already succeeded and may be billed; keep its result.
            print("jev: warning: could not write cache entry", file=sys.stderr)


def api_key():
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key and sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "typesafe", "-w"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            key = result.stdout.strip()
    if not key:
        raise JevError("missing TYPESAFE_API_KEY or macOS Keychain service typesafe")
    return key


def read_json(text):
    value = json.loads(text)
    # json.loads accepts nonfinite numbers, including overflowing exponents.
    json.dumps(value, allow_nan=False)
    return value


def load_questions(path):
    try:
        document = read_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise JevError("questions file must be readable UTF-8 JSON with finite numbers") from None
    if isinstance(document, dict) and "schema_version" in document:
        version = document["schema_version"]
        if type(version) is not int or version not in SCHEMA_VERSIONS:
            supported = ", ".join(map(str, SCHEMA_VERSIONS))
            raise JevError(f"unsupported questions schema_version; supported: {supported}, or a raw question mapping without a wrapper")
        if set(document) != {"schema_version", "questions"}:
            raise JevError("versioned questions document must contain exactly schema_version and questions")
        document = document["questions"]
    if not isinstance(document, dict) or not document:
        raise JevError("questions must be a nonempty API question mapping")
    for question in document.values():
        if not isinstance(question, dict) or question.get("type") not in ("choice", "score", "noul", "box"):
            raise JevError("supported question types: choice, score, noul, box")
    return document


def valid_response(result):
    return isinstance(result, dict) and isinstance(result.get("answers"), dict) and "id" not in result


def classify(record, questions, key, backend, timeout, cache=None):
    if not isinstance(record, dict) or "id" not in record or "state" not in record:
        raise JevError("record must be an object with id and state")
    if not isinstance(record["state"], (str, dict, list)):
        raise JevError("state must be a string, object, or array")
    payload = {"model": backend.model, "state": record["state"], "questions": questions}
    if "assets" in record:
        if not isinstance(record["assets"], dict):
            raise JevError("assets must be an object")
        payload["assets"] = record["assets"]
    headers = {"Content-Type": "application/json", "User-Agent": "jev-cli/0.1"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if "assets" in record or any(q["type"] == "box" for q in questions.values()):
        if not backend.supports_box:
            raise JevError("Box and assets require a backend and model with image support; free Jev 1.13 is unsupported")
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    body = json.dumps(
        payload,
        allow_nan=False,
    ).encode("utf-8")
    entry = cache.path(backend.endpoint, body) if cache else None
    cached = cache.load(entry) if entry else None
    if cached is not None:
        return {"id": record["id"], **cached}
    request = urllib.request.Request(
        backend.endpoint, data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = read_json(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        exc.close()
        raise JevError(f"System One HTTP {exc.code}; no retry performed") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise JevError("System One connection failed or timed out; no retry performed") from None
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise JevError("System One returned an invalid response object")
    if "id" in result:
        raise JevError("System One response id collides with record id")
    if cache:
        cache.store(entry, result)
    return {"id": record["id"], **result}


def id_key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def jsonl_records(lines, source):
    for number, line in enumerate(lines, 1):
        try:
            record = read_json(line)
        except (ValueError, RecursionError):
            raise JevError(f"{source} line {number}: invalid JSON") from None
        if not isinstance(record, dict) or "id" not in record:
            raise JevError(f"{source} line {number}: expected an object with id")
        yield number, record


def evaluate(name, options, label_lines, prediction_lines):
    labels = {}
    for number, record in jsonl_records(label_lines, "labels"):
        if record.get("label") not in options:
            raise JevError(f"labels line {number}: label must be one of the {name} options")
        key = id_key(record["id"])
        if key in labels:
            raise JevError(f"labels line {number}: duplicate id")
        labels[key] = record["label"]
    if not labels:
        raise JevError("labels file has no records")

    predictions = {}
    for number, record in jsonl_records(prediction_lines, "predictions"):
        answers = record.get("answers")
        if not isinstance(answers, dict):
            raise JevError(f"predictions line {number}: expected answers object")
        if id_key(record["id"]) in predictions:
            raise JevError(f"predictions line {number}: duplicate id")
        predictions[id_key(record["id"])] = answers.get(name)

    confusion = {truth: dict.fromkeys(options, 0) for truth in options}
    unscored = {"missing_prediction": 0, "unknown_choice": 0}
    bins = [[] for _ in range(len(CALIBRATION_EDGES) + 1)]
    missing_confidence = 0
    for key, truth in labels.items():
        if key not in predictions:
            unscored["missing_prediction"] += 1
            continue
        answer = predictions[key]
        choice = answer.get("choice") if isinstance(answer, dict) else None
        if not isinstance(choice, str) or choice not in options:
            unscored["unknown_choice"] += 1
            continue
        confusion[truth][choice] += 1
        confidence = answer.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            missing_confidence += 1
            continue
        bins[bisect_right(CALIBRATION_EDGES, confidence)].append((confidence, choice == truth))

    scored = sum(sum(row.values()) for row in confusion.values())
    correct = sum(confusion[label][label] for label in options)
    edges = (0.0, *CALIBRATION_EDGES, 1.0)
    calibration = [{
        "low": edges[i], "high": edges[i + 1], "count": len(rows),
        "mean_confidence": math.fsum(c for c, _ in rows) / len(rows) if rows else None,
        "accuracy": sum(ok for _, ok in rows) / len(rows) if rows else None,
    } for i, rows in enumerate(bins)]
    return {
        "question": name,
        "labels": list(options),
        "counts": {
            "labeled": len(labels),
            "scored": scored,
            "unscored": unscored,
            "extra_predictions": len(predictions.keys() - labels.keys()),
            "calibrated": scored - missing_confidence,
            "missing_confidence": missing_confidence,
        },
        "correct": correct,
        "accuracy": correct / scored if scored else None,
        "confusion": confusion,
        "calibration": calibration,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["classify", "batch", "eval"])
    parser.add_argument("--questions", required=True, type=Path, help=f"JSON questions file: raw API mapping or {{schema_version: {'|'.join(map(str, SCHEMA_VERSIONS))}, questions}}")
    parser.add_argument("--base-url", default=os.environ.get("JEV_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("JEV_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout", type=float, default=60, help="per-request seconds (default 60)")
    parser.add_argument("--input", type=Path, help="one {id,state} JSON record; default stdin")
    parser.add_argument("--cache", type=Path, metavar="DIR", help="reuse and store successful responses in DIR; off by default")
    parser.add_argument("--labels", type=Path, help="eval: hand-labeled {id,label} JSONL file")
    parser.add_argument("--question", help="eval: Choice question name; default the only question")
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise JevError("timeout must be positive and finite")
        if args.command != "classify" and args.input:
            raise JevError(f"{args.command} reads JSONL from stdin; --input is for classify")
        if args.command != "eval" and (args.labels or args.question is not None):
            raise JevError("--labels and --question are for eval")
        if args.command == "eval" and args.cache:
            raise JevError("eval sends no requests; --cache is for classify and batch")
        questions = load_questions(args.questions)
        if args.command == "eval":
            if not args.labels:
                raise JevError("eval requires --labels")
            name = args.question if args.question is not None else next(iter(questions)) if len(questions) == 1 else None
            question = questions.get(name)
            if not question or question["type"] != "choice" or not isinstance(question.get("criteria"), dict) or not question["criteria"]:
                raise JevError("eval needs --question naming a Choice question with criteria options")
            with args.labels.open(encoding="utf-8") as labels:
                report = evaluate(name, tuple(question["criteria"]), labels, sys.stdin)
            print(json.dumps(report, ensure_ascii=False, allow_nan=False))
            return 0
        backend = Backend(args.base_url, args.model)
        if any(q["type"] == "box" for q in questions.values()) and not backend.supports_box:
            raise JevError("Box and assets require a backend and model with image support; free Jev 1.13 is unsupported")
        key = api_key() if backend.typesafe else None
        cache = Cache(args.cache) if args.cache else None
        if args.command == "classify":
            raw = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
            result = classify(read_json(raw), questions, key, backend, args.timeout, cache)
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        else:
            for number, line in enumerate(sys.stdin, 1):
                try:
                    result = classify(read_json(line), questions, key, backend, args.timeout, cache)
                except (JevError, ValueError, UnicodeError, RecursionError) as exc:
                    detail = str(exc) if isinstance(exc, JevError) else "invalid JSON record"
                    print(f"jev: batch stopped at line {number}; {detail}", file=sys.stderr)
                    return 1
                print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    except JevError as exc:
        print("jev: " + str(exc), file=sys.stderr)
        return 1
    except (ValueError, OSError, UnicodeError, subprocess.SubprocessError, RecursionError):
        print("jev: could not read input, credentials, or API JSON", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
