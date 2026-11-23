#!/usr/bin/env python3
"""Small stdlib client for Jev System One."""

import argparse
from bisect import bisect_right
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from fractions import Fraction
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "jev-1.13-free"
SCHEMA_VERSIONS = (1,)
CALIBRATION_EDGES = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
PRICING_DATE = "2026-11-14"
ZEN_PRICES = "https://opencode.ai/docs/zen"
TYPESAFE_PRICES = "https://docs.typesafe.ai/models"
MAX_CONCURRENCY = 32
QUESTION_TOKEN_LIMIT = 32000
REQUEST_OVERHEAD_TOKENS = 512
MAX_RETRIES = 10
DEFAULT_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 30.0
ERROR_BODY_LIMIT = 4096
ENDPOINT_UNAVAILABLE = b"Endpoint is unavailable"
NEVER_RETRIED = (401, 403, 422)
RETRY_RULE = (f"HTTP 429 or 5xx, a connection error or timeout, or an '{ENDPOINT_UNAVAILABLE.decode()}' error body "
              f"on a status other than {'/'.join(map(str, NEVER_RETRIED))}")
PRICES = {
    ("opencode.ai", "jev-1.13-free"): ("0", ZEN_PRICES),
    ("opencode.ai", "jev-1.13"): ("0.042", ZEN_PRICES),
    **{("api.typesafe.ai", model): ("0.042", TYPESAFE_PRICES)
       for model in ("jev-1.14.0", "jev-1.13.0", "jev-latest", "jev-preview")},
}
TOKEN_METHOD = ("heuristic: ceil(bytes / 4) over the exact UTF-8 request body classify sends "
                "(ASCII-escaped JSON), without assets; not a tokenizer count")


@dataclass(frozen=True)
class Backend:
    base_url: str
    model: str

    def __post_init__(self):
        # urlsplit silently drops tabs and newlines that urllib keeps; only printable ASCII parses alike in both.
        if re.search(r"[^\x21-\x7e]", self.base_url):
            raise JevError("base URL must be printable ASCII without spaces or control characters")
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

    @property
    def price(self):
        host = urlsplit(self.base_url).hostname
        if host == "api.typesafe.ai" and not self.typesafe:
            return None
        return PRICES.get((host, self.model))


class JevError(Exception):
    pass


class Transient(Exception):
    def __init__(self, reason, retry_after=None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


CACHE_VERSION = 1


@dataclass(frozen=True)
class Cache:
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
            print("jev: warning: could not write cache entry", file=sys.stderr)


def api_key():
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key and sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "typesafe", "-w"],
            capture_output=True, encoding="utf-8", timeout=15,
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


def uses_images(payload):
    return "assets" in payload or any(q["type"] == "box" for q in payload["questions"].values())


def request_payload(record, questions, backend):
    if not isinstance(record, dict) or "id" not in record or "state" not in record:
        raise JevError("record must be an object with id and state")
    if not isinstance(record["state"], (str, dict, list)):
        raise JevError("state must be a string, object, or array")
    payload = {"model": backend.model, "state": record["state"], "questions": questions}
    if "assets" in record:
        if not isinstance(record["assets"], dict):
            raise JevError("assets must be an object")
        payload["assets"] = record["assets"]
    if uses_images(payload) and not backend.supports_box:
        raise JevError("Box and assets require a backend and model with image support; free Jev 1.13 is unsupported")
    return payload


def request_body(value):
    return json.dumps(value, allow_nan=False).encode("utf-8")


def estimate_tokens(value):
    return (len(request_body(value)) + 3) // 4


def estimate(record, questions, backend):
    payload = request_payload(record, questions, backend)
    text = {key: value for key, value in payload.items() if key != "assets"}
    row = {"id": record["id"], "request_bytes": len(request_body(payload)),
           "input_tokens_estimate": estimate_tokens(text),
           "question_tokens_estimate": estimate_tokens(questions),
           "state_tokens_estimate": estimate_tokens(record["state"])}
    if "assets" in payload:
        row["assets_not_estimated"] = True
    return row


def dry_run_report(rows, backend):
    price, source = backend.price or (None, None)
    total = sum(row["input_tokens_estimate"] for row in rows)
    cost = None
    if price is not None and not any(row.get("assets_not_estimated") for row in rows):
        cost = format((Decimal(price) * total / 1000000).normalize(), "f")
    if price is None:
        status, basis = "unknown", "No listed price for this backend host and model; cost is unknown, not zero."
    else:
        status = "free" if Decimal(price) == 0 else "priced"
        basis = f"Input price read from source on {PRICING_DATE}; the page shows \"$\" without a currency code, so USD is assumed."
    return {
        "dry_run": True,
        "estimate": "Estimated input tokens and cost; nothing was sent and no credentials were read.",
        "backend": {"endpoint": backend.endpoint, "model": backend.model},
        "token_method": TOKEN_METHOD,
        "pricing": {
            "date": PRICING_DATE, "currency": "USD", "status": status,
            "input_usd_per_million_tokens": price,
            "output_usd_per_million_tokens": None if price is None else "0",
            "source": source,
            "assumptions": [basis, "Image assets are not estimated; any record with assets makes the cost unknown."],
        },
        "output_tokens": "not estimated; listed pricing bills output tokens at 0; unknown backends may bill them",
        "records": rows,
        "total_input_tokens_estimate": total,
        "estimated_cost_usd": cost,
    }


def prepare(record, questions, key, backend):
    payload = request_payload(record, questions, backend)
    headers = {"Content-Type": "application/json", "User-Agent": "jev-cli/0.1"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if uses_images(payload):
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    return urllib.request.Request(backend.endpoint, data=request_body(payload), headers=headers, method="POST")


def retry_after_seconds(value):
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,9}", value):
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def send(request, timeout):
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            status = exc.code
            retry_after = retry_after_seconds((exc.headers or {}).get("Retry-After"))
            unavailable = status not in NEVER_RETRIED and ENDPOINT_UNAVAILABLE in (exc.read(ERROR_BODY_LIMIT) or b"")
        except (OSError, ValueError):
            unavailable = False
        finally:
            exc.close()
        if status == 429 or 500 <= status <= 599 or unavailable:
            raise Transient(f"System One HTTP {status}", retry_after) from None
        raise JevError(f"System One HTTP {status}; not retryable") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Transient("System One connection failed or timed out") from None
    try:
        return read_json(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise JevError("System One returned invalid JSON") from None


def backoff_seconds(attempt, retry_after):
    if retry_after is not None:
        return min(BACKOFF_CAP_SECONDS, retry_after)
    return random.uniform(0, min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)))


def post(request, timeout, retries, stop):
    attempt = 1
    while True:
        try:
            return send(request, timeout)
        except Transient as exc:
            if attempt > retries:
                plural = "s" if attempt > 1 else ""
                raise JevError(f"{exc.reason}; gave up after {attempt} attempt{plural}") from None
            if stop.wait(backoff_seconds(attempt, exc.retry_after)):
                raise JevError(f"{exc.reason}; retry cancelled") from None
            attempt += 1


def respond(record_id, request, timeout, retries, stop, cache=None, check=None):
    entry = cache.path(request.full_url, request.data) if cache else None
    result = cache.load(entry) if entry else None
    if result is not None:
        if check:
            check(result)
        return {"id": record_id, **result}
    result = post(request, timeout, retries, stop)
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise JevError("System One returned an invalid response object")
    if "id" in result:
        raise JevError("System One response id collides with record id")
    if check:
        check(result)
    if cache:
        cache.store(entry, result)
    return {"id": record_id, **result}


def classify(record, questions, key, backend, timeout, retries=0, stop=None, cache=None):
    request = prepare(record, questions, key, backend)
    return respond(record["id"], request, timeout, retries, stop or threading.Event(), cache)


def utf8_tokens(text):
    # Upper bound for byte-level BPE tokenizers: every token covers at least one UTF-8 byte.
    return len(text.encode("utf-8"))


def chunk_capacity(questions, budget):
    capacity = budget - REQUEST_OVERHEAD_TOKENS - utf8_tokens(json.dumps(questions, ensure_ascii=False))
    if capacity < 4:
        raise JevError("--chunk-tokens leaves no room for state after question overhead")
    return capacity


def split_text(text, capacity):
    spans, start, size = [], 0, 0
    for end, char in enumerate(text):
        width = len(char.encode("utf-8"))
        if size + width > capacity:
            spans.append((start, end))
            start, size = end, 0
        size += width
    spans.append((start, len(text)))
    return spans


def check_chunk_questions(questions, budget):
    if not 0 < budget <= QUESTION_TOKEN_LIMIT:
        raise JevError(f"--chunk-tokens must be between 1 and {QUESTION_TOKEN_LIMIT}")
    if any(q["type"] != "choice" or not isinstance(q.get("criteria"), dict) or not q["criteria"]
           for q in questions.values()):
        raise JevError("--chunk-tokens supports only choice questions with criteria objects")
    return chunk_capacity(questions, budget)


def chunk_plan(record, questions, budget):
    if not isinstance(record, dict) or "id" not in record or "state" not in record:
        raise JevError("record must be an object with id and state")
    if not isinstance(record["state"], str) or "assets" in record:
        raise JevError("--chunk-tokens supports only string state without assets")
    return split_text(record["state"], chunk_capacity(questions, budget))


def choice_probabilities(question, answer):
    probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
    if not isinstance(probabilities, dict) or set(probabilities) != set(question["criteria"]) or not all(
            isinstance(p, (int, float)) and not isinstance(p, bool) for p in probabilities.values()):
        raise JevError("chunk answer probabilities must cover exactly the criteria labels")
    return probabilities


def mean_choice(question, chunk_probabilities):
    labels = list(question["criteria"])
    exact = {label: sum(map(Fraction, (p[label] for p in chunk_probabilities))) / len(chunk_probabilities)
             for label in labels}
    choice = max(labels, key=exact.__getitem__)
    total = {label: float(mean) for label, mean in exact.items()}
    return {"type": "choice", "choice": choice, "confidence": total[choice], "probabilities": total}


def run_chunks(record, spans, questions, key, backend, timeout, budget, retries, stop, cache=None):
    if len(spans) == 1:
        return respond(record["id"], prepare(record, questions, key, backend), timeout, retries, stop, cache)
    chunks, probabilities = [], []
    for index, (start, end) in enumerate(spans):
        text = record["state"][start:end]
        try:
            request = prepare({"id": None, "state": text}, questions, key, backend)
            result = respond(None, request, timeout, retries, stop, cache,
                             lambda r: probabilities.append({name: choice_probabilities(q, r["answers"].get(name))
                                                             for name, q in questions.items()}))
        except JevError as exc:
            raise JevError(f"chunk {index + 1}/{len(spans)}: {exc}") from None
        del result["id"]
        chunks.append({"index": index, "start": start, "end": end,
                       "estimated_tokens": utf8_tokens(text), "response": result})
    answers = {name: mean_choice(question, [p[name] for p in probabilities]) for name, question in questions.items()}
    return {
        "id": record["id"],
        "answers": answers,
        "aggregation": {"method": "mean", "weights": "equal", "tie_break": "criteria order",
                        "confidence": "mean probability of chosen label"},
        "chunking": {"token_budget": budget, "state_capacity": chunk_capacity(questions, budget),
                     "estimator": "utf8-bytes", "overlap": 0, "count": len(chunks)},
        "chunks": chunks,
    }


def estimate_chunked(record, questions, backend, budget):
    spans = chunk_plan(record, questions, budget)
    if len(spans) == 1:
        return estimate(record, questions, backend)
    rows = [estimate({"id": None, "state": record["state"][start:end]}, questions, backend) for start, end in spans]
    return {"id": record["id"], "chunks": len(rows),
            **{field: sum(row[field] for row in rows)
               for field in ("request_bytes", "input_tokens_estimate", "question_tokens_estimate", "state_tokens_estimate")}}


def emit(result):
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)


def batch(questions, key, backend, timeout, retries, concurrency, cache=None, chunk_tokens=None):
    stop = threading.Event()
    window = deque()
    lines = iter(sys.stdin)
    emitted = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        try:
            while True:
                while lines is not None and len(window) < concurrency:
                    try:
                        line = next(lines, None)
                        if line is None:
                            lines = None
                            break
                        record = read_json(line)
                        if chunk_tokens is None:
                            request = prepare(record, questions, key, backend)
                        else:
                            spans = chunk_plan(record, questions, chunk_tokens)
                    except (JevError, ValueError, UnicodeError, RecursionError) as exc:
                        window.append(exc)
                        lines = None
                        break
                    if chunk_tokens is None:
                        window.append(pool.submit(respond, record["id"], request, timeout, retries, stop, cache))
                    else:
                        window.append(pool.submit(run_chunks, record, spans, questions, key, backend, timeout,
                                                  chunk_tokens, retries, stop, cache))
                if not window:
                    return 0
                pending = window.popleft()
                try:
                    if not isinstance(pending, Future):
                        raise pending
                    result = pending.result()
                except (JevError, ValueError, UnicodeError, RecursionError) as exc:
                    detail = str(exc) if isinstance(exc, JevError) else "invalid JSON record"
                    print(f"jev: batch stopped at line {emitted + 1}; {detail}", file=sys.stderr)
                    return 1
                emit(result)
                emitted += 1
        except KeyboardInterrupt:
            print(f"jev: batch stopped at line {emitted + 1}; interrupted", file=sys.stderr)
            return 130
        finally:
            stop.set()
            for pending in window:
                if isinstance(pending, Future):
                    pending.cancel()


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


def summarize(lines, question=None, questions=None):
    records = []
    for number, line in enumerate(lines, 1):
        try:
            record = read_json(line)
        except (ValueError, RecursionError):
            raise JevError(f"summarize stopped at line {number}; invalid JSON record") from None
        if not isinstance(record, dict) or "id" not in record or not isinstance(record.get("answers"), dict):
            raise JevError(f"summarize stopped at line {number}; record must be an object with id and answers")
        records.append(record)
    if question is None:
        names = list(dict.fromkeys(name for record in records for name in record["answers"]))
        if len(names) != 1:
            raise JevError("select one question with --question; input has " + (", ".join(names) or "no answers"))
        question = names[0]
    labels = ()
    if questions and question in questions:
        spec = questions[question]
        if not isinstance(spec, dict) or spec.get("type") != "choice" or not isinstance(spec.get("criteria"), dict):
            raise JevError(f"summarize supports Choice questions only; {question!r} is not one")
        labels = list(spec["criteria"])
    counts = dict.fromkeys(labels, 0)
    examples = {label: [] for label in labels}
    edges = (0.0, *CALIBRATION_EDGES, 1.0)
    bins = {f"{low:.1f}-{high:.1f}": 0 for low, high in zip(edges, edges[1:])}
    bin_names = list(bins)
    confidence = {"bins": bins, "missing": 0, "malformed": 0, "out_of_range": 0}
    diagnostics = []
    for number, record in enumerate(records, 1):
        answer = record["answers"].get(question)
        if not isinstance(answer, dict) or answer.get("type", "choice") != "choice" or not isinstance(answer.get("choice"), str):
            raise JevError(f"summarize stopped at line {number}; {question!r} is not a Choice answer")
        label = answer["choice"]
        counts[label] = counts.get(label, 0) + 1
        bucket = examples.setdefault(label, [])
        if len(bucket) < 3:
            bucket.append(record["id"])
        if "confidence" not in answer:
            confidence["missing"] += 1
            continue
        value = answer["confidence"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            confidence["malformed"] += 1
            diagnostics.append(f"line {number}: confidence {json.dumps(value, ensure_ascii=False)} is not a number")
        elif not 0 <= value <= 1:
            confidence["out_of_range"] += 1
            diagnostics.append(f"line {number}: confidence {json.dumps(value)} is outside [0, 1]")
        else:
            bins[bin_names[bisect_right(CALIBRATION_EDGES, value)]] += 1
    return {"question": question, "records": len(records), "counts": counts, "examples": examples,
            "confidence": confidence, "diagnostics": diagnostics}


def build_parser():
    questions_help = (f"JSON questions file: raw API mapping or "
                      f"{{schema_version: {'|'.join(map(str, SCHEMA_VERSIONS))}, questions}}")
    sending = argparse.ArgumentParser(add_help=False)
    sending.add_argument("--questions", type=Path, required=True, help=questions_help)
    sending.add_argument("--base-url", default=os.environ.get("JEV_BASE_URL", DEFAULT_BASE_URL))
    sending.add_argument("--model", default=os.environ.get("JEV_MODEL", DEFAULT_MODEL))
    sending.add_argument("--timeout", type=float, default=60, help="per-request seconds (default 60)")
    sending.add_argument("--cache", type=Path, metavar="DIR", help="reuse and store successful responses in DIR; off by default")
    sending.add_argument("--dry-run", action="store_true",
                         help="validate, then print estimated input tokens and cost as one JSON object; "
                              "sends nothing and reads no credentials")
    sending.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                         help=f"retries per request after {RETRY_RULE} (default {DEFAULT_RETRIES}, max {MAX_RETRIES})")
    sending.add_argument("--chunk-tokens", type=int, metavar="N",
                         help=f"split string state so each request stays within N estimated tokens "
                              f"(max {QUESTION_TOKEN_LIMIT}); choice questions only")

    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, metavar="{classify,batch,eval,summarize}")
    classify_ = commands.add_parser("classify", allow_abbrev=False, parents=[sending], help="classify one {id,state} record")
    classify_.add_argument("--input", type=Path, help="one {id,state} JSON record; default stdin")
    classify_.set_defaults(run=run_send)
    batch_ = commands.add_parser("batch", allow_abbrev=False, parents=[sending], help="classify {id,state} JSONL from stdin, in order")
    batch_.add_argument("--concurrency", type=int, default=1, help=f"requests in flight (default 1, max {MAX_CONCURRENCY})")
    batch_.set_defaults(run=run_send)
    eval_ = commands.add_parser("eval", allow_abbrev=False, help="score saved batch results on stdin against hand labels; offline")
    eval_.add_argument("--questions", type=Path, required=True, help=questions_help)
    eval_.add_argument("--labels", type=Path, required=True, help="hand-labeled {id,label} JSONL file")
    eval_.add_argument("--question", help="Choice question name; default the only question")
    eval_.set_defaults(run=run_eval)
    summarize_ = commands.add_parser("summarize", allow_abbrev=False, help="count Choice labels in saved results; offline")
    summarize_.add_argument("--questions", type=Path, help=questions_help + "; used only for label order")
    summarize_.add_argument("--input", type=Path, help="results JSONL; default stdin")
    summarize_.add_argument("--question", help="Choice question name; default the only question")
    summarize_.set_defaults(run=run_summarize)
    return parser


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except JevError as exc:
        print("jev: " + str(exc), file=sys.stderr)
        return 1
    except (ValueError, OSError, UnicodeError, subprocess.SubprocessError, RecursionError):
        print("jev: could not read input or credentials", file=sys.stderr)
        return 1


def run_send(args):
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise JevError("timeout must be positive and finite")
    if not 0 <= args.retries <= MAX_RETRIES:
        raise JevError(f"retries must be between 0 and {MAX_RETRIES}")
    if args.command == "batch" and not 1 <= args.concurrency <= MAX_CONCURRENCY:
        raise JevError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
    questions = load_questions(args.questions)
    if args.chunk_tokens is not None:
        check_chunk_questions(questions, args.chunk_tokens)
    backend = Backend(args.base_url, args.model)
    if any(q["type"] == "box" for q in questions.values()) and not backend.supports_box:
        raise JevError("Box and assets require a backend and model with image support; free Jev 1.13 is unsupported")
    key = api_key() if backend.typesafe and not args.dry_run else None
    cache = Cache(args.cache) if args.cache else None

    if args.dry_run:
        def estimator(record):
            if args.chunk_tokens is None:
                return estimate(record, questions, backend)
            return estimate_chunked(record, questions, backend, args.chunk_tokens)
        if args.command == "classify":
            raw = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
            emit(dry_run_report([estimator(read_json(raw))], backend))
            return 0
        rows = []
        for number, line in enumerate(sys.stdin, 1):
            try:
                rows.append({"line": number, **estimator(read_json(line))})
            except (JevError, ValueError, UnicodeError, RecursionError) as exc:
                detail = str(exc) if isinstance(exc, JevError) else "invalid JSON record"
                print(f"jev: batch stopped at line {number}; {detail}", file=sys.stderr)
                return 1
        emit(dry_run_report(rows, backend))
        return 0
    if args.command == "batch":
        return batch(questions, key, backend, args.timeout, args.retries, args.concurrency, cache, args.chunk_tokens)
    raw = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
    record = read_json(raw)
    if args.chunk_tokens is None:
        emit(classify(record, questions, key, backend, args.timeout, args.retries, cache=cache))
    else:
        spans = chunk_plan(record, questions, args.chunk_tokens)
        emit(run_chunks(record, spans, questions, key, backend, args.timeout, args.chunk_tokens,
                        args.retries, threading.Event(), cache))
    return 0


def run_eval(args):
    questions = load_questions(args.questions)
    name = args.question if args.question is not None else next(iter(questions)) if len(questions) == 1 else None
    question = questions.get(name)
    if not question or question["type"] != "choice" or not isinstance(question.get("criteria"), dict) or not question["criteria"]:
        raise JevError("eval needs --question naming a Choice question with criteria options")
    with args.labels.open(encoding="utf-8") as labels:
        report = evaluate(name, tuple(question["criteria"]), labels, sys.stdin)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0


def run_summarize(args):
    question, questions = args.question, None
    if args.questions:
        questions = load_questions(args.questions)
        if question is None and len(questions) == 1:
            question = next(iter(questions))
    if args.input:
        with args.input.open(encoding="utf-8") as source:
            summary = summarize(source, question, questions)
    else:
        summary = summarize(sys.stdin, question, questions)
    for diagnostic in summary["diagnostics"]:
        print("jev: " + diagnostic, file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
