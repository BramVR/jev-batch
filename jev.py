#!/usr/bin/env python3
"""Small stdlib client for TypeSafe System One."""

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class JevError(Exception):
    pass


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


def classify(record, questions, key, model, timeout):
    if not isinstance(record, dict) or "id" not in record or "state" not in record:
        raise JevError("record must be an object with id and state")
    if not isinstance(record["state"], (str, dict, list)):
        raise JevError("state must be a string, object, or array")
    payload = {"model": model, "state": record["state"], "questions": questions}
    if "assets" in record:
        if not isinstance(record["assets"], dict):
            raise JevError("assets must be an object")
        payload["assets"] = record["assets"]
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if "assets" in record or any(q["type"] == "box" for q in questions.values()):
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    body = json.dumps(
        payload,
        allow_nan=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT, data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = read_json(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        exc.close()
        raise JevError(f"TypeSafe HTTP {exc.code}; no retry performed") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise JevError("TypeSafe connection failed or timed out; no retry performed") from None
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise JevError("TypeSafe returned an invalid response object")
    if "id" in result:
        raise JevError("TypeSafe response id collides with record id")
    return {"id": record["id"], **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["classify", "batch"])
    parser.add_argument("--questions", required=True, type=Path, help="raw API questions mapping JSON file")
    parser.add_argument("--model", default="jev-1.14.0")
    parser.add_argument("--timeout", type=float, default=60, help="per-request seconds (default 60)")
    parser.add_argument("--input", type=Path, help="one {id,state} JSON record; default stdin")
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise JevError("timeout must be positive and finite")
        if args.command == "batch" and args.input:
            raise JevError("batch reads JSONL from stdin; --input is for classify")
        questions = read_json(args.questions.read_text(encoding="utf-8"))
        if not isinstance(questions, dict) or not questions:
            raise JevError("questions must be a nonempty API question mapping")
        for question in questions.values():
            if not isinstance(question, dict) or question.get("type") not in ("choice", "score", "noul", "box"):
                raise JevError("supported question types: choice, score, noul, box")
        key = api_key()
        if args.command == "classify":
            raw = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
            result = classify(read_json(raw), questions, key, args.model, args.timeout)
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        else:
            for number, line in enumerate(sys.stdin, 1):
                try:
                    result = classify(read_json(line), questions, key, args.model, args.timeout)
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
