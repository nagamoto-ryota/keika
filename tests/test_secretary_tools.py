"""AI相談の作る道具とメールの下書き。作るのは頼まれた時だけ、送る経路は無い。左に実物で開く。"""
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.claude_checks import CheckRun, save_run
from keika.db import connect
from keika.findings import Evidence, Finding
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record
from keika.secretary import TOOLS, WireMail, answer_question, get_mail, made_list, mark_mail_ok, revise_mail, start_question, thread_messages
from keika.settings import Settings
from keika.web.app import create_app
from test_reading import FakeClient, answer

USAGE = NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 5))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-01", kind_read="電話", body="能登さんに週2回の家事の内容を聞く。"))
    add_record(c, RecordIn(user_id="U01", event_date="2026-11-01", kind_read="電話", body="杉森さんのひみつ。"))
    yield s, c
    c.close()


def said(text):
    return NS(content=[NS(type="text", text=text)], stop_reason="end_turn", usage=USAGE)


def tool(name, **kw):
    return NS(content=[NS(type="tool_use", id="t1", name=name, input=kw)], stop_reason="tool_use", usage=USAGE)


def ask(s, c, thread, text, *results):
    client = FakeClient(*results)
    answer_question(c, client, s, thread, start_question(c, thread, text))
    return client, thread_messages(c, thread)[-1]


def result(client):
    for m in client.calls[-1]["messages"]:
        if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result":
            return m["content"][0]
    raise AssertionError


MAIL = dict(to="能登さん", subject="家事の内容の確認", body="能登さん、週2回の家事の内容を教えてください。")


def finding():
    return Finding(rule_id="R07", user_id="U04", kind="finding", period="2026-10", source="claude", message="交付の記録が見当たりません。", evidence=[Evidence(file="x.csv", quote="計画")], todo="主治医への交付を確かめる")


# --- 道具の形 ---


def test_tools_still_take_no_user_id():
    for t in TOOLS:
        assert "user_id" not in t["input_schema"]["properties"]


def test_making_tools_say_only_when_asked():
    for t in TOOLS:
        if t["name"] in ("make_next_steps", "make_care_plan", "write_mail"):
            assert "頼まれた時だけ" in t["description"]


def test_no_way_to_send_exists():
    source = (Path(__file__).parent.parent / "src" / "keika" / "secretary.py").read_text(encoding="utf-8")
    for word in ("smtplib", "send_message", "sendmail", "gmail"):
        assert word not in source
    assert not [t for t in TOOLS if "send" in t["name"]]


# --- 点検と手順の案 ---


def test_list_findings_shows_this_users_only(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    client, _ = ask(s, c, "U04", "何が残ってる？", tool("list_findings"), said("交付です。"))
    assert "交付の記録が見当たりません" in str(result(client)["content"])


def test_make_next_steps_opens_on_the_left(env, monkeypatch):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    monkeypatch.setattr("keika.secretary.make_plan", lambda conn, client, s, f: {"steps": [{"text": "主治医に連絡する", "who": "ケアマネ"}], "docs": [{"kind": "主治医への連絡文", "title": "連絡", "body": "〇〇", "flag": ""}], "removed": 0})
    client, _ = ask(s, c, "U04", "手順を作って", tool("make_next_steps", number="N1"), said("作りました。"))
    assert "主治医に連絡する" in str(result(client)["content"])
    made = made_list(c, "U04")
    assert made[0]["kind"] == "plan" and made[0]["url"].startswith("/users/U04/plan/")


def test_make_next_steps_unknown_number_is_refused(env, monkeypatch):
    s, c = env
    client, _ = ask(s, c, "U04", "手順を作って", tool("make_next_steps", number="N9"), said("ありません。"))
    assert result(client)["is_error"] and made_list(c, "U04") == []


def test_make_care_plan_opens_on_the_left(env, monkeypatch):
    s, c = env
    monkeypatch.setattr("keika.secretary.make_care_plan", lambda conn, client, s, user_id: 7)
    ask(s, c, "U04", "プランを作って", tool("make_care_plan"), said("作りました。"))
    assert made_list(c, "U04")[0]["url"] == "/careplans/7"


def test_make_care_plan_failure_says_so(env, monkeypatch):
    s, c = env
    monkeypatch.setattr("keika.secretary.make_care_plan", lambda conn, client, s, user_id: None)
    client, _ = ask(s, c, "U04", "プランを作って", tool("make_care_plan"), said("作れませんでした。"))
    assert result(client)["is_error"] and made_list(c, "U04") == []


def test_show_draft_only_opens_this_users(env):
    s, c = env
    client, _ = ask(s, c, "U04", "下書き見せて", tool("show_draft", draft_id=99), said("ありません。"))
    assert result(client)["is_error"]


def test_all_thread_cannot_make_anything(env):
    s, c = env
    client, _ = ask(s, c, "all", "作って", tool("write_mail", **MAIL), said("できません。"))
    assert result(client)["is_error"] and made_list(c, "all") == []


def test_made_is_kept_by_person(env, monkeypatch):
    s, c = env
    monkeypatch.setattr("keika.secretary.make_care_plan", lambda conn, client, s, user_id: 7)
    ask(s, c, "U04", "プランを作って", tool("make_care_plan"), said("作りました。"))
    assert made_list(c, "U02") == []


# --- メール ---


def test_mail_draft_is_stored_and_opens(env):
    s, c = env
    ask(s, c, "U04", "能登さんへのメールを作って", tool("write_mail", **MAIL), said("作りました。"))
    made = made_list(c, "U04")[0]
    assert made["kind"] == "mail"
    mail = get_mail(c, "U04", made["item_id"])
    assert mail["to_name"] == "能登さん" and mail["status"] == "draft"
    html = TestClient(create_app(s)).get(made["url"]).text
    assert "能登さん、週2回の家事の内容を教えてください" in html and "送信はしません" in html and ">いいよ</button>" in html and "<summary>違う、こうして</summary>" in html


def test_mail_with_a_word_not_in_the_record_is_refused(env):
    s, c = env
    client, _ = ask(s, c, "U04", "メールを作って", tool("write_mail", to="能登さん", subject="確認", body="99歩歩いたと聞きました。"), said("だめでした。"))
    assert result(client)["is_error"] and "99" in str(result(client)["content"]) and made_list(c, "U04") == []


def test_mail_with_a_medical_judgment_is_refused(env):
    s, c = env
    client, _ = ask(s, c, "U04", "メールを作って", tool("write_mail", to="能登さん", subject="確認", body="その薬は中止すべきです。"), said("だめでした。"))
    assert result(client)["is_error"] and made_list(c, "U04") == []


def mail_id(s, c):
    ask(s, c, "U04", "能登さんへのメールを作って", tool("write_mail", **MAIL), said("作りました。"))
    return made_list(c, "U04")[0]["item_id"]


def test_mail_ok_marks_it_without_sending(env):
    s, c = env
    i = mail_id(s, c)
    r = TestClient(create_app(s)).post(f"/desk/U04/mail/{i}/ok", follow_redirects=True)
    assert r.status_code == 200 and "確かめました" in r.text and "送るのはご自身で" in r.text
    assert get_mail(c, "U04", i)["status"] == "ok"


def test_mail_revise_applies_the_instruction(env):
    s, c = env
    i = mail_id(s, c)
    notice = revise_mail(c, FakeClient(answer(WireMail(to="能登さん", subject="家事の内容の確認", body="能登さん、週2回の家事の内容を金曜までに教えてください。"))), s, "U04", i, "金曜までにと足して")
    mail = get_mail(c, "U04", i)
    assert notice == "" and "金曜まで" in mail["body"] and "金曜" in mail["changed"] and mail["status"] == "draft"


def test_mail_revise_refuses_words_from_nowhere(env):
    s, c = env
    i = mail_id(s, c)
    notice = revise_mail(c, FakeClient(answer(WireMail(to="能登さん", subject="件名", body="99回の話です。"))), s, "U04", i, "直して")
    assert "直しませんでした" in notice and get_mail(c, "U04", i)["body"] == MAIL["body"]


def test_mail_revise_route_shows_the_result(env):
    s, c = env
    i = mail_id(s, c)
    http = TestClient(create_app(s, client_factory=lambda: FakeClient(answer(WireMail(to="能登さん", subject="件名", body="99回の話です。")))))
    assert "直しませんでした" in http.post(f"/desk/U04/mail/{i}/revise", data={"instruction": "直して"}, follow_redirects=True).text


def test_other_thread_cannot_open_the_mail(env):
    s, c = env
    i = mail_id(s, c)
    assert TestClient(create_app(s)).get(f"/desk/U02/mail/{i}").status_code == 404


def test_sample_mode_cannot_change_mail(env):
    s, c = env
    i = mail_id(s, c)
    assert TestClient(create_app(s, sample=True)).post(f"/desk/U04/mail/{i}/ok").status_code == 403


# --- 画面 ---


def test_messages_carry_what_was_made(env):
    s, c = env
    i = mail_id(s, c)
    data = TestClient(create_app(s)).get("/desk/U04/messages").json()
    assert data["made"][0]["url"] == f"/desk/U04/mail/{i}"


def test_page_has_a_tab_for_what_was_made(env):
    s, c = env
    assert 'data-tab="made"' in TestClient(create_app(s)).get("/desk?t=U04").text


def test_pages_hide_the_header_inside_the_left_pane(env):
    s, c = env
    html = TestClient(create_app(s)).get("/board").text
    assert "window.self !== window.top" in html


def test_answer_can_be_sent_to_the_record_box_only_by_a_button(env):
    s, c = env
    before = c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    ask(s, c, "U04", "質問", said("答えです。"))
    assert c.execute("SELECT COUNT(*) FROM records").fetchone()[0] == before
    assert "keika_prefill" in TestClient(create_app(s)).get("/records/new").text
