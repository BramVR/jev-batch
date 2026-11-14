#!/usr/bin/env python3
"""Prepare recent session excerpts and run the Jev batch CLI. Private outputs only."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

REDACTION_POLICY = "jev-demo-redact-1"
# Applied in order to every string value; keys, IDs, and structure stay unchanged.
REDACTION_RULES = [
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED:private_key]"),
    ("bearer", re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED:bearer]"),
    ("token", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                         r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}"
                         r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"), "[REDACTED:token]"),
    ("assignment", re.compile(r"(?i)\b((?:api[_-]?key|secret|token|password|passwd|access[_-]?key)[\"']?\s*[:=]\s*[\"']?)"
                              r"(?!\[REDACTED)[^\s\"',;]{8,}"), r"\1[REDACTED:secret]"),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"), "[REDACTED:email]"),
    ("home_path", re.compile(r"(?<![\w.:/\\])(?:/Users|/home|\b[A-Za-z]:[\\/]+Users)[\\/]+[^\\/\s\"'`]+"), "[REDACTED:home]"),
]

RULE_BY_LABEL = {re.search(r"\[REDACTED:(\w+)\]", placeholder).group(1): name for name, _, placeholder in REDACTION_RULES}
PLACEHOLDER = re.compile(r"\[REDACTED:(%s)\]" % "|".join(RULE_BY_LABEL))


def redact(value, counts):
    """Return a copy of nested JSON with sensitive strings replaced; add per-rule hits to counts."""
    if isinstance(value, str):
        for name, pattern, placeholder in REDACTION_RULES:
            value, hits = pattern.subn(placeholder, value)
            if hits:
                counts[name] += hits
        return value
    if isinstance(value, list):
        return [redact(item, counts) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, counts) for key, item in value.items()}
    return value


def redact_records(records):
    counts = Counter()
    redacted = [{key: item if key == "id" else redact(item, counts) for key, item in record.items()}
                if isinstance(record, dict) else record for record in records]
    return redacted, {"policy": REDACTION_POLICY, "counts": dict(sorted(counts.items()))}


def prepare_sample(limit, counts=None):
    """Return up to limit records; with counts, redact each message and tally kept redactions there."""
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
                    if counts is not None:
                        # Redact the whole message before capping so a cut cannot leave half a secret.
                        text = redact(text, Counter())
                    text = text[:4000]
                    # Count only placeholders that survive the cap, so counts describe the saved excerpt.
                    hits = Counter(RULE_BY_LABEL[label] for label in PLACEHOLDER.findall(text)) if counts is not None else Counter()
                    if text:
                        messages.append(({"role": payload["role"], "text": text}, hits))
        if meta.get("cwd") == str(ROOT) or not messages or not meta.get("id") or meta["id"] in seen:
            continue
        excerpt = []
        size = 0
        kept = Counter()
        for message, hits in reversed(messages):
            if size + len(message["text"]) > 24000:
                break
            excerpt.append(message)
            kept += hits
            size += len(message["text"])
        if not excerpt:
            continue
        seen.add(meta["id"])
        if counts is not None:
            counts += kept
        records.append({"id": meta["id"], "state": {
            "sampling": "Trailing user and assistant messages only; each message capped at 4000 characters; total at most 24000. Tools, reasoning, and earlier context omitted. Not necessarily a complete session.",
            "messages": list(reversed(excerpt)),
        }})
        if len(records) == limit:
            break
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--send", action="store_true", help="send the existing reviewed sample to the selected backend; requires authorization")
    parser.add_argument("--no-redact", action="store_true", help="skip redaction when preparing and sending; review the sample by hand")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 20:
        parser.error("--limit must be between 1 and 20")
    folder = ROOT / ".private" / "demo"
    folder.mkdir(parents=True, exist_ok=True)
    inputs = folder / "sample.jsonl"
    if args.send:
        try:
            records = [json.loads(line) for line in inputs.read_text(encoding="utf-8").splitlines()]
        except (OSError, ValueError):
            parser.error("--send requires an existing valid .private/demo/sample.jsonl; prepare and review it first")
        if not 1 <= len(records) <= args.limit:
            parser.error("existing sample count must be between 1 and --limit; sample was not changed")
    else:
        prepared = None if args.no_redact else Counter()
        records = prepare_sample(args.limit, prepared)
        inputs.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    summary = {"sample_count": len(records), "session_ids": [r["id"] for r in records],
               "classification_counts": {}, "example_ids_by_category": {}, "unclassified": len(records)}
    if args.no_redact:
        outgoing, summary["redaction"] = records, {"policy": "disabled", "counts": {}}
    elif args.send:
        # Redact the outgoing copy again: older or hand-edited samples may predate redaction.
        outgoing, summary["redaction"] = redact_records(records)
    else:
        outgoing, summary["redaction"] = records, {"policy": REDACTION_POLICY, "counts": dict(sorted(prepared.items()))}
    if args.send and records:
        with (folder / "results.jsonl").open("w", encoding="utf-8") as output:
            run = subprocess.run([sys.executable, str(ROOT / "jev.py"), "batch", "--questions",
                                  str(ROOT / "examples/failure-patterns.json"), "--cache", str(folder / "cache")],
                                 input="".join(json.dumps(r, ensure_ascii=False) + "\n" for r in outgoing),
                                 stdout=output, stderr=subprocess.PIPE, encoding="utf-8",
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        results = [json.loads(line) for line in (folder / "results.jsonl").read_text(encoding="utf-8").splitlines()]
        if [r["id"] for r in results] != summary["session_ids"][:len(results)]:
            raise ValueError("result ids differ from input order")
        summary["classification_counts"] = dict(Counter(r["answers"]["failure_pattern"]["choice"] for r in results))
        categories = json.loads((ROOT / "examples/failure-patterns.json").read_text())["failure_pattern"]["criteria"]
        summary["classification_counts"] = {category: summary["classification_counts"].get(category, 0) for category in categories}
        summary["example_ids_by_category"] = {
            category: [r["id"] for r in results if r["answers"]["failure_pattern"]["choice"] == category][:3]
            for category in categories
        }
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
