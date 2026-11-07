"""上のメニューの今いる画面、画面の1回の間だけのファイル一覧の使い回し。"""
import re
import threading
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika import files
from keika.db import connect
from keika.files import one_view, scan
from keika.ledger import seed_from_json
from keika.settings import Settings
from keika.tasks import build_board
from keika.web.app import create_app


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者" / "U04_柚木茂").mkdir(parents=True)
    (root / "利用者" / "U04_柚木茂" / "メモ.txt").write_text("メモ", encoding="utf-8")
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 5))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def counting(monkeypatch):
    calls = []
    real = files._walk
    monkeypatch.setattr(files, "_walk", lambda s: (calls.append(1), real(s))[1])
    return calls


# --- 使い回し ---


def test_off_by_default_every_scan_walks(env, monkeypatch):
    s, _ = env
    calls = counting(monkeypatch)
    scan(s)
    scan(s)
    assert len(calls) == 2


def test_one_view_walks_once(env, monkeypatch):
    s, _ = env
    calls = counting(monkeypatch)
    with one_view():
        assert scan(s) == scan(s)
    assert len(calls) == 1


def test_nothing_carries_over_to_the_next_view(env):
    s, _ = env
    with one_view():
        before = len(scan(s))
    (s.drive_root / "利用者" / "U04_柚木茂" / "新しい.txt").write_text("x", encoding="utf-8")
    with one_view():
        assert len(scan(s)) == before + 1
    assert len(scan(s)) == before + 1


def test_list_changes_by_caller_do_not_reach_the_next_caller(env):
    s, _ = env
    with one_view():
        first = scan(s)
        first.clear()
        assert scan(s) != []


def test_other_threads_do_not_share(env, monkeypatch):
    s, _ = env
    calls = counting(monkeypatch)
    with one_view():
        scan(s)
        t = threading.Thread(target=lambda: scan(s))
        t.start()
        t.join()
    assert len(calls) == 2


def test_other_drive_root_is_not_mixed(env, tmp_path):
    s, _ = env
    other = tmp_path / "other"
    (other / "利用者" / "U09_他").mkdir(parents=True)
    (other / "利用者" / "U09_他" / "a.txt").write_text("a", encoding="utf-8")
    s2 = Settings(drive_root=other, db_path=s.db_path, as_of=s.as_of)
    with one_view():
        assert {r.rel_path for r in scan(s)} != {r.rel_path for r in scan(s2)}


def test_same_board_with_and_without(env):
    s, c = env
    def shape(board):
        return [(p.user.id, [(t.title, t.deadline) for t in p.tasks]) for p in board.people], board.total, [u.id for u in board.visit_waiting]
    off = shape(build_board(c, s))
    with one_view():
        on = shape(build_board(c, s))
    assert on == off


@pytest.mark.parametrize("path", ["/", "/todo", "/board"])
def test_screens_walk_a_few_times_only(env, monkeypatch, path):
    s, _ = env
    http = TestClient(create_app(s))
    calls = counting(monkeypatch)
    assert http.get(path).status_code == 200
    assert len(calls) == 1


def test_measurement_paths_never_turn_it_on():
    from pathlib import Path
    root = Path(__file__).parent.parent
    for name in list((root / "eval").glob("*.py")) + [root / "src" / "keika" / "cli.py", root / "src" / "keika" / "reading.py", root / "src" / "keika" / "watcher.py", root / "src" / "keika" / "claude_checks.py", root / "src" / "keika" / "intake.py"]:
        assert "one_view" not in name.read_text(encoding="utf-8"), name


def test_a_new_file_shows_on_the_next_view(env):
    s, _ = env
    http = TestClient(create_app(s))
    assert "新しい書類.txt" not in http.get("/desk?t=U04").text
    (s.drive_root / "利用者" / "U04_柚木茂" / "新しい書類.txt").write_text("x", encoding="utf-8")
    assert "新しい書類.txt" in http.get("/desk?t=U04").text


# --- 今いる画面 ---


def current(html):
    return re.findall(r'<a href="([^"]+)"[^>]*aria-current="page"', html.split("</nav>")[0])


@pytest.mark.parametrize("path,on", [
    ("/", "/"), ("/todo", "/"), ("/review", "/"), ("/office", "/"), ("/silenced", "/"),
    ("/desk", "/desk"), ("/desk?t=U04", "/desk"),
    ("/board", "/board"), ("/users/U04", "/board"), ("/users/U04/visit", "/board"), ("/ledger", "/board"),
    ("/records/new", "/records/new"),
    ("/guide", None),
])
def test_menu_marks_exactly_the_current_screen(env, path, on):
    s, _ = env
    r = TestClient(create_app(s)).get(path)
    assert r.status_code == 200
    assert current(r.text) == ([on] if on else [])


def test_record_button_is_not_filled_unless_current(env):
    s, _ = env
    http = TestClient(create_app(s))
    assert 'class="primary"' not in http.get("/").text.split("</nav>")[0]
    assert 'aria-current="page"' in http.get("/records/new").text.split("</nav>")[0]


def test_a_press_shows_a_reaction_at_once(env):
    s, _ = env
    assert "going" in TestClient(create_app(s)).get("/").text
