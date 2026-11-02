"""コミットの門番: キー、実データ、ログ、試験の失敗があれば止める。hook と同じ形（標準入力の JSON）で呼ぶ。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).parent.parent / ".claude" / "hooks" / "commit_guard.py"
FAKE_KEY = "sk-" + "ant-" + "a" * 24


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "x@example.invalid")
    git(tmp_path, "config", "user.name", "x")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "check_public.py").write_bytes((GUARD.parent.parent.parent / "tools" / "check_public.py").read_bytes())
    (tmp_path / "ok.md").write_text("問題なし", encoding="utf-8")
    return tmp_path


def run(repo, command, env_tests="0"):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)})
    return subprocess.run([sys.executable, str(GUARD)], input=payload, text=True, encoding="utf-8", capture_output=True,
                          cwd=repo, env={**__import__("os").environ, "KEIKA_GUARD_TESTS": env_tests, "PYTHONIOENCODING": "utf-8"})


def test_clean_commit_passes(repo):
    git(repo, "add", "ok.md")
    assert run(repo, 'git commit -m "x"').returncode == 0


def test_other_commands_are_ignored(repo):
    (repo / "bad.md").write_text(FAKE_KEY, encoding="utf-8")
    assert run(repo, "ls -la").returncode == 0


def test_key_in_staged_file_is_blocked(repo):
    (repo / "bad.md").write_text(FAKE_KEY, encoding="utf-8")
    git(repo, "add", "bad.md")
    r = run(repo, 'git commit -m "x"')
    assert r.returncode == 2 and "API キー" in r.stderr


def test_key_added_in_the_same_command_is_blocked(repo):
    (repo / "bad.md").write_text(FAKE_KEY, encoding="utf-8")
    assert run(repo, 'git add -A; git commit -m "x"').returncode == 2


def test_real_looking_phone_number_is_blocked(repo):
    (repo / "bad.csv").write_text("連絡先 0857-22-1234", encoding="utf-8")
    git(repo, "add", "bad.csv")
    assert run(repo, 'git commit -m "x"').returncode == 2


def test_fake_phone_number_passes(repo):
    (repo / "ok.csv").write_text("連絡先 000-0000-2109", encoding="utf-8")
    git(repo, "add", "ok.csv")
    assert run(repo, 'git commit -m "x"').returncode == 0


@pytest.mark.parametrize("command", ["git add -f .env", "git add --force data/keika.db", 'git add -f x.jsonl; git commit -m "x"'])
def test_forced_add_of_secret_files_is_blocked(repo, command):
    assert run(repo, command).returncode == 2


def test_failing_tests_block_a_commit_with_python(repo):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
    git(repo, "add", "tests/test_x.py")
    r = run(repo, 'git commit -m "x"', env_tests="1")
    assert r.returncode == 2 and "試験" in r.stderr


def test_passing_tests_allow_the_commit(repo):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    git(repo, "add", "tests/test_x.py")
    assert run(repo, 'git commit -m "x"', env_tests="1").returncode == 0


def test_docs_only_commit_skips_tests(repo):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
    git(repo, "add", "ok.md")
    assert run(repo, 'git commit -m "x"', env_tests="1").returncode == 0
