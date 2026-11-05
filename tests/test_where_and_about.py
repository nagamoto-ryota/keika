"""項目の文に根拠を添える。秘書へ項目ごと移れる。項目の数は変えない。"""
from datetime import date
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.claude_checks import APP_RECORDS, CheckRun, save_run
from keika.db import connect
from keika.findings import Evidence, Finding
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record
from keika.secretary import answer_question, start_question, thread_messages
from keika.settings import Settings
from keika.tasks import Task, build_board, finding_where
from keika.web.app import create_app
from test_reading import FakeClient

USAGE = NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)
DOC = "利用者/U04_柚木茂/居宅サービス計画書_2026-08-06.txt"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者" / "U04_柚木茂").mkdir(parents=True)
    (root / DOC).write_text("交付: 本人、ほたるい。主治医は空欄。", encoding="utf-8")
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 5))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def finding(**kw):
    data = dict(rule_id="R07", user_id="U04", kind="finding", period="2026-10", source="claude", message="交付の記録が見当たりません。", evidence=[Evidence(file=DOC, quote="交付: 本人、ほたるい。主治医は空欄。")], todo="事業所ごとと主治医への交付を確かめる")
    return Finding(**(data | kw))


def test_where_names_the_document_and_quote():
    assert finding_where(finding()) == "根拠: 居宅サービス計画書 8/6「交付: 本人、ほたるい。主治医は空欄。」"


def test_where_names_the_record_with_its_date():
    f = finding(evidence=[Evidence(file="利用者/U04/支援経過_2026-08-01_2026-11-03.csv", quote="各事業所に計画書を交付した。", record_date="2026-08-07"), Evidence(file=DOC, quote="交付")])
    assert finding_where(f) == "根拠: 支援経過 8/7「各事業所に計画書を交付した。」 ほか1件"


def test_month_only_name_reads_as_a_month():
    f = finding(evidence=[Evidence(file="利用者/U04/利用票_2026-09_変更.pdf", quote="署名")])
    assert finding_where(f) == "根拠: 利用票 9月 変更「署名」"


def test_where_for_app_records():
    f = finding(evidence=[Evidence(file=APP_RECORDS, quote="有効期間が11月30日まで", record_date="2026-11-02")])
    assert finding_where(f).startswith("根拠: 支援経過 11/2")


def test_long_quote_is_cut():
    f = finding(evidence=[Evidence(file=DOC, quote="あ" * 60)])
    assert "…" in finding_where(f) and len(finding_where(f)) < 70


def test_no_evidence_says_what_was_searched():
    f = finding(evidence=[], searched_scope="台帳の認定有効期間")
    assert finding_where(f) == "根拠の書類は見当たりません（探した範囲: 台帳の認定有効期間）"


def test_nothing_known_says_so():
    assert "特定できていません" in finding_where(finding(evidence=[], searched_scope=None))
    assert "特定できていません" in Task("進捗だけ").where


def test_homework_needs_no_extra_line():
    assert Task("宿題", homework=[object()]).where == ""


def test_todo_and_user_pages_show_it_and_counts_do_not_change(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    http = TestClient(create_app(s))
    before = build_board(c, s).total
    for path in ("/todo", "/users/U04"):
        assert "根拠: 居宅サービス計画書 8/6" in http.get(path).text
    assert build_board(c, s).total == before


def test_about_prefills_the_question(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    http = TestClient(create_app(s))
    page = http.get("/todo").text
    link = page[page.index("/desk?t=U04&about="):].split(chr(34))[0]
    html = http.get(link).text
    assert "「事業所ごとと主治医への交付を確かめる」はどれのことですか" in html
    assert c.execute("SELECT COUNT(*) FROM desk_messages").fetchone()[0] == 0  # 送るのは人


def test_about_of_another_person_is_ignored(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    http = TestClient(create_app(s))
    page = http.get("/todo").text
    fp = page[page.index("about=") + 6:].split(chr(34))[0]
    assert "はどれのことですか" not in http.get(f"/desk?t=U02&about={fp}").text


def test_about_draft_of_another_person_is_ignored(env):
    s, c = env
    assert "下書きの" not in TestClient(create_app(s)).get("/desk?t=U02&about=d99").text


def tool(name, **kw):
    return NS(content=[NS(type="tool_use", id="t1", name=name, input=kw)], stop_reason="tool_use", usage=USAGE)


def said(text):
    return NS(content=[NS(type="text", text=text)], stop_reason="end_turn", usage=USAGE)


def test_open_document_opens_only_this_users(env):
    s, c = env
    answer_question(c, FakeClient(tool("open_document", file=DOC), said("計画書です。")), s, "U04", start_question(c, "U04", "交付ってどれのこと？"))
    assert thread_messages(c, "U04")[-1]["cites"][0]["file"] == DOC
    answer_question(c, FakeClient(tool("open_document", file=DOC), said("読めません。")), s, "U02", start_question(c, "U02", "どれ？"))
    assert thread_messages(c, "U02")[-1]["cites"] == []


def test_list_findings_gives_the_evidence(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    client = FakeClient(tool("list_findings"), said("これです。"))
    answer_question(c, client, s, "U04", start_question(c, "U04", "交付ってどれ？"))
    assert DOC in str(client.calls[-1]["messages"])


def test_prompt_asks_to_open_the_document(env):
    s, c = env
    client = FakeClient(said("はい。"))
    answer_question(c, client, s, "U04", start_question(c, "U04", "こんにちは"))
    assert "交付ってどれのこと" in client.calls[0]["system"][0]["text"]


def test_no_key_does_not_stop_any_screen(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    http = TestClient(create_app(s))
    for path in ("/", "/todo", "/board", "/desk", "/desk?t=U04", "/users/U04", "/records/new"):
        assert http.get(path).status_code == 200, path
    assert http.post("/desk/U04/ask", data={"question": "夜は？"}).json()["status"] == "working"
    last = http.get("/desk/U04/messages").json()["messages"][-1]
    assert last["status"] == "failed" and "もう一度" in last["text"]
