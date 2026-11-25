#!/usr/bin/env python3
"""Install jev-batch into a fresh venv and check the installed command. Needs network for setuptools."""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("jev.py", "pyproject.toml", "README.md", "LICENSE")


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8")


def main():
    version = re.search(r'^__version__ = "([^"]+)"$', (ROOT / "jev.py").read_text(encoding="utf-8"), re.M)[1]
    print(f"python {sys.version.split()[0]}; expecting jev-batch {version}")
    with tempfile.TemporaryDirectory() as tmp:
        # Install from a copy so build/ and *.egg-info stay out of the repo.
        source = Path(tmp) / "src"
        source.mkdir()
        for name in SOURCES:
            shutil.copy2(ROOT / name, source)
        env = Path(tmp) / "venv"
        # Symlink like `python -m venv`; a copied interpreter binary aborts on some standalone builds.
        venv.create(env, with_pip=True, symlinks=sys.platform != "win32")
        bin_dir = str(env / ("Scripts" if sys.platform == "win32" else "bin"))
        python = shutil.which("python", path=bin_dir)
        install = run(python, "-m", "pip", "install", "--disable-pip-version-check", "-q", str(source))
        if install.returncode:
            print(install.stdout + install.stderr)
            print("FAIL  pip install")
            return 1
        command = shutil.which("jev-batch", path=bin_dir)
        version_out = run(command, "--version") if command else None
        help_out = run(command, "classify", "--help") if command else None
        show = run(python, "-m", "pip", "show", "--disable-pip-version-check", "jev-batch")
        requires = re.search(r"^Requires:(.*)$", show.stdout, re.M)
        checks = {
            f"jev-batch --version prints jev-batch {version}":
                command and (version_out.returncode, version_out.stdout) == (0, f"jev-batch {version}\n"),
            "jev-batch classify --help exits 0": command and help_out.returncode == 0 and "--questions" in help_out.stdout,
            "no jev command installed": shutil.which("jev", path=bin_dir) is None,
            "pip show jev-batch lists no requirements": show.returncode == 0 and requires and not requires[1].strip(),
        }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
