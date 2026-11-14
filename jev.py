#!/usr/bin/env python3
"""Small stdlib client for Jev System One."""

import argparse
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["classify", "batch"])
    parser.add_argument("--questions", required=True, type=Path, help=f"JSON questions file: raw API mapping or {{schema_version: {'|'.join(map(str, SCHEMA_VERSIONS))}, questions}}")
    parser.add_argument("--base-url", default=os.environ.get("JEV_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("JEV_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout", type=float, default=60, help="per-request seconds (default 60)")
    parser.add_argument("--input", type=Path, help="one {id,state} JSON record; default stdin")
    parser.add_argument("--cache", type=Path, metavar="DIR", help="reuse and store successful responses in DIR; off by default")
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise JevError("timeout must be positive and finite")
        if args.command == "batch" and args.input:
            raise JevError("batch reads JSONL from stdin; --input is for classify")
        questions = load_questions(args.questions)
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
