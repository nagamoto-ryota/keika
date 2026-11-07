"""コミットの門番（Claude Code の PreToolUse hook）。

止める物: API キー、会話ログや .env や data/ のファイル、000- 以外の電話番号（実データの疑い）、試験の失敗。
作る過程で、試験が落ちたままコミットしたことが3回あったので、試験も見る。文書だけのコミットでは試験を回さない。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

COMMIT = re.compile(r"\bgit\s+(?:-\S+\s+)*commit\b")
ADD = re.compile(r"\bgit\s+(?:-\S+\s+)*add\b")
FORCED_ADD = re.compile(r"\bgit\s+(?:-\S+\s+)*add\s+([^;&|]*)")
NEEDS_TESTS = re.compile(r"\.py$|\.html$|\.yaml$|^src/|^sample_data/|^demo/")


def block(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def changed_files(root: Path, including_unstaged: bool) -> list[str]:
    def lines(*args: str) -> list[str]:
        out = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=root, capture_output=True, text=True, encoding="utf-8").stdout
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    files = lines("diff", "--cached", "--name-only")
    if including_unstaged:
        files += lines("diff", "--name-only") + lines("ls-files", "--others", "--exclude-standard")
    return list(dict.fromkeys(files))


def run_tests(root: Path) -> None:
    venv = root / ".venv" / "Scripts" / "python.exe"
    python = str(venv) if venv.exists() else sys.executable
    done = subprocess.run([python, "-m", "pytest", "tests", "-q", "-x"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        block("試験が通っていないのでコミットを止めました。\n" + "\n".join(done.stdout.splitlines()[-15:]))


def main() -> None:
    data = json.load(sys.stdin)
    command = (data.get("tool_input") or {}).get("command") or ""
    root = Path(data.get("cwd") or os.getcwd())
    sys.path.insert(0, str(root / "tools"))
    if shutil.which("git") is None:
        return
    from check_public import LOG_NAMES, problems

    for m in FORCED_ADD.finditer(command):
        words = m[1].split()
        if {"-f", "--force"} & set(words) and any(LOG_NAMES.search(w) for w in words):
            block(f"無視の設定を越えて入れようとしています: {m[0].strip()}")
    if not COMMIT.search(command):
        return
    files = changed_files(root, bool(ADD.search(command)))
    found = problems(files, [], root)
    if found:
        block("公開リポに入れてはいけない物が見つかったのでコミットを止めました。\n" + "\n".join(found))
    if os.environ.get("KEIKA_GUARD_TESTS", "1") != "0" and (root / "tests").is_dir() and any(NEEDS_TESTS.search(f) for f in files):
        run_tests(root)


if __name__ == "__main__":
    main()
