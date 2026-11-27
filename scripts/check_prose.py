#!/usr/bin/env python3
"""Check a PR title and body for the writing problems reviewers keep correcting.

python3 scripts/check_prose.py BODY.md   # a draft; a first line "# TITLE <title>" is checked as the title
PR_TITLE=... PR_BODY=... python3 scripts/check_prose.py --pr-env
"""
import argparse
import os
from pathlib import Path
import re

WORDS = ("additionally", "crucial", "delve", "enhance", "enhances", "enhanced", "foster", "landscape", "pivotal",
         "showcase", "testament", "underscore", "underscores", "vibrant", "utilize", "utilizes", "leverage",
         "leverages", "seamless", "seamlessly", "simply")
RULES = (
    ("dash", re.compile(r"[—–]|\s--?\s"), "a dash joins two thoughts; use a period or a comma"),
    ("smart-quote", re.compile(r"[‘’“”]"), "use straight quotes"),
    ("arrow", re.compile(r"->|=>|<-|→|←"), "spell the arrow out as a verb"),
    ("slash", re.compile(r"\w ?/ ?\w"), 'write "and", "or", or "N of M"; put a path in backticks'),
    ("ai-word", re.compile(r"\b(?:%s)\b" % "|".join(WORDS), re.I), "use the plain word, or cut it"),
    ("filler", re.compile(r"\b(?:in order to|it is important to note|it's worth noting|etc\.)", re.I),
     'cut the filler: "in order to" is "to"; list the items instead of "etc."'),
    ("chatbot", re.compile(r"\b(?:hope this helps|let me know if|happy to help)\b", re.I), "cut the chat phrase"),
)
HEADINGS = ("Why", "What changed", "Scope", "Tradeoffs", "Blast Radius", "Verification")
MAX_BODY_LINES = 40
CODE = re.compile(r"`[^`\n]*`|https?://\S+|\]\([^)]*\)|<[^>\s]+>")
MARKER = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")


def unfenced(text):
    fenced = False
    for number, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            yield number, line


def prose_problems(text, source):
    found = []
    for number, line in unfenced(text):
        line = CODE.sub(" ", MARKER.sub("", line))
        for rule, pattern, fix in RULES:
            match = pattern.search(line)
            if match:
                excerpt = line[max(0, match.start() - 20):match.end() + 20].strip()
                found.append(f"{source}:{number}: {rule}: {excerpt!r}: {fix}")
    return found


def shape_problems(body):
    found = []
    headings = [(n, line[3:].strip()) for n, line in unfenced(body) if line.startswith("## ")]
    for number, heading in headings:
        if heading not in HEADINGS:
            found.append(f"body:{number}: heading: '## {heading}' is not one of: {', '.join(HEADINGS)}")
    if not headings or headings[0][1] != "Why":
        found.append("body:1: heading: the first heading must be '## Why'")
    known = [HEADINGS.index(h) for _, h in headings if h in HEADINGS]
    if known != sorted(set(known)):
        found.append(f"body:1: heading: use each heading once, in this order: {', '.join(HEADINGS)}")
    lines = len(body.strip("\n").split("\n"))
    if lines > MAX_BODY_LINES:
        found.append(f"body:1: length: {lines} lines; keep the body under {MAX_BODY_LINES} lines and link long output")
    return found


def check(title, body):
    return (prose_problems(title, "title") if title is not None else []) + prose_problems(body, "body") \
        + shape_problems(body)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("body", nargs="?", type=Path, help="a PR body draft")
    source.add_argument("--pr-env", action="store_true", help="read the PR_TITLE and PR_BODY environment variables")
    args = parser.parse_args(argv)
    if args.pr_env:
        title, body = os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", "")
    else:
        first, _, rest = args.body.read_text(encoding="utf-8").partition("\n")
        title, body = (first[len("# TITLE "):], rest) if first.startswith("# TITLE ") else (None, first + "\n" + rest)
    found = check(title, body)
    for problem in found:
        print(problem)
    print(f"prose: {len(found)} problems")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
