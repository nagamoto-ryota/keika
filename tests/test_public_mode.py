"""審査用の公開版。合言葉を知っている人だけが入れ、合言葉が無い人には AI を呼ばない。AI の利用額は1日の上限で止める。"""
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.ledger import seed_from_json
from keika.public import LIMIT_MESSAGE, DailyLimit, fresh_db
from keika.settings import Settings
from keika.web.app import create_app

CODE = "test-code-123"


@pytest.fixture
def env(tmp_path):
    s = Settings(drive_root=SAMPLE / "drive", db_path=tmp_path / "keika.db", as_of=date(2026, 11, 13))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


class Counting:
    def __init__(self):
        self.made = 0

    def __call__(self):
        self.made += 1
        raise RuntimeError("呼ばれた")


def web(s, factory=None):
    factory = factory or Counting()
    return TestClient(create_app(s, client_factory=factory, access_code=CODE), base_url="https://testserver", follow_redirects=False), factory  # 公開版は https だけ


def test_without_the_code_only_the_notice_is_shown_and_no_ai_is_called(env):
    s, _ = env
    http, factory = web(s)
    for path in ("/", "/users/U04", "/desk?t=U04", "/board", "/drafts/1"):
        r = http.get(path)
        assert r.status_code == 401 and "審査用です" in r.text and "合言葉" in r.text, path
        assert "柚木" not in r.text
    assert http.post("/records/new", data={"text": "話した"}).status_code == 403
    assert factory.made == 0


def test_styles_open_without_the_code(env):
    s, _ = env
    http, _ = web(s)
    assert http.get("/static/style.css").status_code == 200


def test_wrong_code_does_not_let_you_in(env):
    s, _ = env
    http, _ = web(s)
    r = http.post("/enter", data={"code": "wrong"})
    assert "合言葉が違います" in r.text and "keika_pass" not in r.headers.get("set-cookie", "")
    assert http.get("/").status_code == 401


def test_right_code_lets_you_in(env):
    s, _ = env
    http, _ = web(s)
    r = http.post("/enter", data={"code": CODE})
    assert r.status_code == 303 and "keika_pass" in r.headers["set-cookie"]
    page = http.get("/")
    assert page.status_code == 200 and "あなたの番" in page.text and "架空データ" in page.text


def test_the_cookie_does_not_contain_the_code(env):
    s, _ = env
    http, _ = web(s)
    cookie = http.post("/enter", data={"code": CODE}).headers["set-cookie"]
    assert CODE not in cookie


def test_daily_limit_stops_new_ai_calls(env):
    spent = [0.0]
    limit = DailyLimit(lambda: "client", limit_usd=5.0, spent=lambda: spent[0])
    assert limit() == "client"
    spent[0] = 5.01
    with pytest.raises(RuntimeError, match=LIMIT_MESSAGE):
        limit()


def test_daily_limit_starts_again_the_next_day():
    spent, day = [7.0], ["2026-11-07"]
    limit = DailyLimit(lambda: "client", limit_usd=5.0, spent=lambda: spent[0], day=lambda: day[0])
    spent[0] = 12.5
    with pytest.raises(RuntimeError):
        limit()
    day[0] = "2026-11-08"
    assert limit() == "client"  # その日の始まりの額から数え直す
    spent[0] = 17.6
    with pytest.raises(RuntimeError):
        limit()


def test_limit_message_reaches_the_screen(env):
    s, _ = env
    limit = DailyLimit(lambda: "client", limit_usd=0.0, spent=lambda: 1.0)
    http, _ = web(s, limit)
    http.post("/enter", data={"code": CODE})
    r = http.post("/records/new", data={"text": "柚木さんに電話した。デイは週2回のまま。"}, follow_redirects=True)
    assert r.status_code == 200 and LIMIT_MESSAGE in r.text


def test_every_start_begins_from_the_base_db(tmp_path):
    base, target = tmp_path / "base.db", tmp_path / "run" / "keika.db"
    sqlite3.connect(base).execute("CREATE TABLE t (x)").connection.commit()
    target.parent.mkdir()
    sqlite3.connect(target).execute("CREATE TABLE dirty (x)").connection.commit()
    fresh_db(base, target)
    names = {r[0] for r in sqlite3.connect(target).execute("SELECT name FROM sqlite_master")}
    assert names == {"t"}


def test_limit_message_reaches_the_ai_consultation(env):
    s, c = env
    limit = DailyLimit(lambda: "client", limit_usd=0.0, spent=lambda: 1.0)
    http, _ = web(s, limit)
    http.post("/enter", data={"code": CODE})
    http.post("/desk/U04/ask", data={"question": "次の訪問で何を確かめる？", "stay": "1"})
    row = c.execute("SELECT text, status FROM desk_messages WHERE role = 'assistant' ORDER BY id DESC").fetchone()
    assert row["text"] == LIMIT_MESSAGE and row["status"] == "failed"


def test_public_start_refuses_without_a_code(tmp_path, monkeypatch):
    from keika.public import app_from_env
    monkeypatch.delenv("KEIKA_ACCESS_CODE", raising=False)
    with pytest.raises(SystemExit):
        app_from_env()


def test_public_start_resets_the_db_and_gates(env, tmp_path, monkeypatch):
    from keika.public import app_from_env
    s, c = env
    c.execute("INSERT INTO desk_messages (thread, role, text, status, created_at) VALUES ('U04', 'user', '汚れ', 'done', '2026-11-07')")
    c.commit()
    base = tmp_path / "base.db"
    sqlite3.connect(s.db_path).backup(sqlite3.connect(base))
    c.execute("INSERT INTO desk_messages (thread, role, text, status, created_at) VALUES ('U04', 'user', 'もっと汚れ', 'done', '2026-11-07')")
    c.commit()
    run_db = tmp_path / "run" / "keika.db"
    for k, v in {"KEIKA_ACCESS_CODE": CODE, "KEIKA_BASE_DB": str(base), "KEIKA_DB_PATH": str(run_db), "KEIKA_DRIVE_ROOT": str(SAMPLE / "drive"), "KEIKA_AS_OF": "2026-11-13"}.items():
        monkeypatch.setenv(k, v)
    http = TestClient(app_from_env(), base_url="https://testserver", follow_redirects=False)
    assert http.get("/").status_code == 401
    texts = [r[0] for r in sqlite3.connect(run_db).execute("SELECT text FROM desk_messages")]
    assert texts == ["汚れ"]


def test_ai_consultation_spending_counts_toward_the_limit(env):
    """AI相談の額も、1日の上限の数に入る（11/7 最終レビュー 🔴）。"""
    from types import SimpleNamespace as NS
    from keika.claude import METER
    from keika.secretary import _loop

    usage = NS(input_tokens=2_000_000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    message = NS(usage=usage, content=[NS(type="text", text="答え")], stop_reason="end_turn")

    class Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self): return message

    client = NS(beta=NS(messages=NS(stream=lambda **kw: Stream())))
    before = METER.cost_usd
    s, c = env
    _loop(client, c, s, "U04", [], [{"role": "user", "content": "x"}], [], "claude-sonnet-5-5", "")
    assert METER.cost_usd - before > 1.0


def test_the_limit_is_checked_on_every_call_not_only_when_the_client_is_made():
    spent = [0.0]
    calls = []
    inner = type("C", (), {})()
    from types import SimpleNamespace as NS
    inner.beta = NS(messages=NS(stream=lambda **kw: calls.append(kw) or "stream"))
    limit = DailyLimit(lambda: inner, limit_usd=5.0, spent=lambda: spent[0])
    client = limit()
    assert client.beta.messages.stream(model="m") == "stream"
    spent[0] = 6.0
    with pytest.raises(RuntimeError, match=LIMIT_MESSAGE):
        client.beta.messages.stream(model="m")
    assert len(calls) == 1


def test_one_person_guessing_does_not_lock_out_the_judges(env):
    s, _ = env
    http, _ = web(s)
    for _ in range(40):
        http.post("/enter", data={"code": "nope"}, headers={"x-forwarded-for": "203.0.113.9"})
    r = http.post("/enter", data={"code": CODE}, headers={"x-forwarded-for": "198.51.100.7"})
    assert r.status_code == 303


def test_pass_cookie_is_secure_on_the_public_version(env):
    s, _ = env
    http, _ = web(s)
    cookie = http.post("/enter", data={"code": CODE}).headers["set-cookie"]
    assert "secure" in cookie.lower()


def test_strange_cookie_does_not_break_the_gate(env):
    s, _ = env
    http, _ = web(s)
    assert http.get("/", headers={"cookie": "keika_pass=é".encode("utf-8")}).status_code == 401
