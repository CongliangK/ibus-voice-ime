#!/usr/bin/env python3
"""Deterministic validation gates for ibus-voice-ime."""

from __future__ import annotations

import ast
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable

EXCLUDED_PARTS = {
    ".git",
    ".claude",
    ".codegraph",
    ".gitnexus",
    ".pi",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    ".venv-qwen-asr",
    ".venv-mimo-asr",
    ".mamba-python312",
    ".micromamba-root",
    "vendor",
    "graphify-out",
}
REQUIRED_FILES = (
    "mise.toml",
    "docs/validation.md",
    ".github/workflows/agentic-validation.yml",
)


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, check=False)


def git_lines(args: list[str]) -> list[str]:
    result = run(["git", *args])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return [line for line in result.stdout.splitlines() if line]


def project_path(path: str) -> bool:
    parts = Path(path).parts
    return not any(part in EXCLUDED_PARTS for part in parts)


def repo_files() -> list[str]:
    paths = git_lines(["ls-files", "--cached", "--others", "--exclude-standard"])
    return sorted(path for path in paths if project_path(path))


def changed_files() -> list[str]:
    names = set(git_lines(["diff", "--name-only"]))
    names.update(git_lines(["diff", "--cached", "--name-only"]))
    names.update(git_lines(["ls-files", "--others", "--exclude-standard"]))
    return sorted(path for path in names if project_path(path))


def python_files(paths: Iterable[str]) -> list[str]:
    return sorted(path for path in paths if path.endswith(".py"))


def shell_files(paths: Iterable[str]) -> list[str]:
    return sorted(path for path in paths if path.endswith(".sh"))


def check_python(path: str) -> str:
    source = Path(path).read_text(encoding="utf-8")
    ast.parse(source, filename=path)
    return f"python syntax ok: {path}"


def check_shell(path: str) -> str:
    result = run(["bash", "-n", path])
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"shell syntax failed: {path}\n{message}")
    return f"shell syntax ok: {path}"


def check_many(label: str, paths: list[str], checker) -> None:
    print(f"{label}: {len(paths)} file(s)")
    with ThreadPoolExecutor() as pool:
        for message in pool.map(checker, paths):
            print(message)


def check_required_files() -> None:
    missing = [path for path in REQUIRED_FILES if not Path(path).is_file()]
    if missing:
        raise RuntimeError("missing required automation: " + ", ".join(missing))
    print("automation files ok")


def run_unit_tests() -> None:
    if not Path("tests").is_dir():
        print("unit tests: no tests directory")
        return
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    result = subprocess.run(command, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError("unit tests failed")


def validate(paths: list[str]) -> None:
    check_required_files()
    check_many("python syntax", python_files(paths), check_python)
    check_many("shell syntax", shell_files(paths), check_shell)
    run_unit_tests()


def selected_paths(mode: str) -> list[str]:
    if mode == "affected":
        paths = changed_files()
        print("affected mode uses changed files plus unit-test fallback")
        return paths
    if mode in {"dev", "release"}:
        print(f"{mode} mode uses full repository validation")
        return repo_files()
    raise RuntimeError(f"unknown validation mode: {mode}")


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "dev"
    try:
        validate(selected_paths(mode))
    except Exception as exc:  # noqa: BLE001 - gate must print concise failures.
        print(f"validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"validation passed: {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
