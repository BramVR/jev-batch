#!/usr/bin/env python3
import ast
from datetime import date
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOW = re.compile(r'#\s*lint-allow:\s*(?P<rule>[\w-]+)\s+reason="(?P<reason>[^"]+)"\s+'
                   r'expires=(?P<expires>\d{4}-\d{2}-\d{2})\s+approved-by=(?P<who>[\w-]+)')
SUBPROCESS_TEXT = {"run", "Popen", "check_output", "check_call", "call"}
UTF8 = 'pass encoding="utf-8"'
SPLITLINES = ("str.splitlines() also splits U+2028/U+2029 inside JSON strings; iterate the file, stream, "
              "or io.StringIO(text) (splits on \\n only), or use text.split(\"\\n\")")


def keyword(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


def text_mode(call, position):
    mode = keyword(call, "mode") or (call.args[position] if len(call.args) > position else None)
    if mode is None:
        return True
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return "b" not in mode.value
    return None


def is_true(node):
    return isinstance(node, ast.Constant) and node.value is True


def encoding_problem(call):
    if keyword(call, "encoding") is not None:
        return None
    func = call.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    owner = func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else None
    if name == "open" and owner != "os" and text_mode(call, 1 if owner is None and isinstance(func, ast.Name) else 0):
        return f"open() in text mode without encoding= uses the locale; {UTF8}"
    if name == "fdopen" and text_mode(call, 1):
        return f"os.fdopen() in text mode without encoding= uses the locale; {UTF8}"
    if name in ("read_text", "write_text"):
        return f"Path.{name}() without encoding= uses the locale; {UTF8}"
    if name == "TextIOWrapper":
        return f"TextIOWrapper() without encoding= uses the locale; {UTF8}"
    if owner == "subprocess" and name in SUBPROCESS_TEXT and (is_true(keyword(call, "text"))
                                                             or is_true(keyword(call, "universal_newlines"))):
        return f"subprocess.{name}(text=True) without encoding= uses the locale; {UTF8} instead of text=True"
    return None


def allowed(line, rule, path, number, errors):
    match = ALLOW.search(line)
    if not match or match["rule"] != rule:
        return False
    if date.fromisoformat(match["expires"]) < date.today():
        errors.append(f"{path}:{number}: {rule}: lint-allow expired {match['expires']}; fix the line or renew with approval")
    return True


def lint(path, text):
    errors = []
    lines = text.split("\n")
    for node in ast.walk(ast.parse(text, str(path))):
        if not isinstance(node, ast.Call):
            continue
        line = lines[node.lineno - 1]
        problem = encoding_problem(node)
        if problem and not allowed(line, "implicit-encoding", path, node.lineno, errors):
            errors.append(f"{path}:{node.lineno}: implicit-encoding: {problem}")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "splitlines" \
                and not allowed(line, "jsonl-splitlines", path, node.lineno, errors):
            errors.append(f"{path}:{node.lineno}: jsonl-splitlines: {SPLITLINES}")
    return errors


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    paths = [Path(a) for a in args] or [Path(p) for p in subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "*.py"], capture_output=True, encoding="utf-8", check=True).stdout.split()]
    errors = []
    for path in paths:
        errors += lint(path, (ROOT / path).read_text(encoding="utf-8"))
    errors.sort(key=lambda e: (e.split(":")[0], int(e.split(":")[1]) if e.split(":")[1].isdigit() else 0))
    for error in errors:
        print(error)
    print(f"lint: {len(paths)} files, {len(errors)} problems")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
