#!/usr/bin/env python3
"""Prepare recent session excerpts and run the Jev batch CLI. Private outputs only."""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--send", action="store_true", help="send excerpts to TypeSafe; requires authorization")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    folder = ROOT / ".private" / "demo"
    folder.mkdir(parents=True, exist_ok=True)
    records = []
    seen = set()
    paths = sorted((Path.home() / ".codex/sessions").rglob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        messages = []
        meta = {}
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                payload = item.get("payload", {})
                if item.get("type") == "session_meta":
                    meta = payload
                if item.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") in {"user", "assistant"}:
                    text = "\n".join(c.get("text", "") for c in payload.get("content", []) if c.get("type") in {"input_text", "output_text"})
                    if text:
                        messages.append({"role": payload["role"], "text": text[:4000]})
        if meta.get("cwd") == str(ROOT) or not messages or not meta.get("id") or meta["id"] in seen:
            continue
        excerpt = []
        size = 0
        for message in reversed(messages):
            if size + len(message["text"]) > 24000:
                break
            excerpt.append(message)
            size += len(message["text"])
        if not excerpt:
            continue
        seen.add(meta["id"])
        records.append({"id": meta["id"], "state": {
            "sampling": "Trailing user and assistant messages only; each message capped at 4000 characters; total at most 24000. Tools, reasoning, and earlier context omitted. Not necessarily a complete session.",
            "messages": list(reversed(excerpt)),
        }})
        if len(records) == args.limit:
            break
    inputs = folder / "sample.jsonl"
    inputs.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    summary = {"sample_count": len(records), "session_ids": [r["id"] for r in records],
               "classification_counts": {}, "unclassified": len(records)}
    if args.send and records:
        with inputs.open(encoding="utf-8") as source, (folder / "results.jsonl").open("w", encoding="utf-8") as output:
            run = subprocess.run([sys.executable, str(ROOT / "jev.py"), "batch", "--questions",
                                  str(ROOT / "examples/failure-patterns.json")], stdin=source, stdout=output, capture_output=False, stderr=subprocess.PIPE, text=True)
        results = [json.loads(line) for line in (folder / "results.jsonl").read_text().splitlines()]
        if [r["id"] for r in results] != summary["session_ids"][:len(results)]:
            raise ValueError("result ids differ from input order")
        summary["classification_counts"] = dict(Counter(r["answers"]["failure_pattern"]["choice"] for r in results))
        summary["unclassified"] -= len(results)
        summary["api_exit_code"] = run.returncode
        summary["diagnostic"] = run.stderr.strip()
    else:
        summary["diagnostic"] = "Prepared only; --send required for API call"
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return summary.get("api_exit_code", 0)


if __name__ == "__main__":
    raise SystemExit(main())
