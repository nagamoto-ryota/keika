"""指摘カードを「次の一手の提案」にする。支援経過の文を先回りで作らず、メールで済む指摘だけ〔作って〕〔今はいい〕を出す。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.claude_checks import CheckRun, save_run
from keika.db import connect
from keika.findings import Evidence, Finding
from keika.ledger import seed_from_json
from keika.outbox import WireProposal, recipients
from keika.secretary import WireMail
from keika.settings import Settings
from keika.web.app import create_app
from test_reading import FakeClient, answer


@pytest.fixture
def env(tmp_path):
    s = Settings(drive_root=SAMPLE / "drive", db_path=tmp_path / "keika.db", as_of=date(2026, 11, 4))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def finding(rule_id="R01", **kw):
    data = dict(rule_id=rule_id, user_id="U04", kind="finding", period="2026-10", source="claude", message="担当者会議の記録が見当たりません。確認してください。",
                evidence=[Evidence(file="利用者/U04_柚木茂/利用票.pdf", page=1, quote="月・水・木")], suggestion="〇〇月〇〇日に〇〇と会議を行った。")
    return Finding(**(data | kw))


def card(html):
    return html.split('id="todo"')[1] if 'id="todo"' in html else html


def page(s, **kw):
    return TestClient(create_app(s, **kw)).get("/users/U04").text


def test_the_draft_record_sentence_is_not_shown(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    html = page(s)
    assert "記録に書き足す文の案" not in html and "〇〇と会議を行った" not in html


def test_meeting_finding_offers_to_make_mails(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    html = page(s)
    assert "会議の開催を調整しませんか。するなら、担当の事業所と主治医に、意見照会と日程調整のメールを作ります。" in html
    assert ">作って</button>" in html and ">今はいい</button>" in html


def test_finding_a_mail_cannot_solve_has_no_offer_but_can_be_closed(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding("R16", message="身体的拘束の記録を確認してください。")]))
    html = page(s)
    assert ">作って</button>" not in html and "確認した・問題なし" in html


def test_older_buttons_move_into_others(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    html = page(s)
    others = html.split("<summary>その他</summary>")[1].split("</details>")[0]
    assert "次の手順と下書きを作る" in others


def test_make_creates_mails_only_for_this_persons_providers_and_doctor(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    fp = page(s).split('name="fp" value="')[1].split('"')[0]
    client = FakeClient(answer(WireMail(to="", subject="柚木 茂さんの担当者会議の件", body="〇〇様\n柚木 茂さんの担当者会議の日程を相談させてください。")))
    r = TestClient(create_app(s, client_factory=lambda: client)).post("/users/U04/offer", data={"fp": fp}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/desk/U04/mail/")
    mine = {p["id"]: p["kind"] for p in recipients(c, s, "U04")}
    made = c.execute("SELECT recipient_id FROM mail_out WHERE user_id = 'U04' AND origin = 'finding'").fetchall()
    ids = [m["recipient_id"] for m in made]
    assert ids and all(i in mine for i in ids)
    assert {mine[i] for i in ids} == {"provider", "medical"}
    assert len(client.calls) == len(ids)


def test_not_now_hides_the_offer(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    fp = page(s).split('name="fp" value="')[1].split('"')[0]
    TestClient(create_app(s)).post("/users/U04/offer/skip", data={"fp": fp})
    html = page(s)
    assert ">作って</button>" not in html and "担当者会議の記録が見当たりません" in html


def test_the_other_mails_of_the_same_offer_are_one_click_away(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding()]))
    fp = page(s).split('name="fp" value="')[1].split('"')[0]
    client = FakeClient(answer(WireMail(to="", subject="柚木 茂さんの担当者会議の件", body="〇〇様\n柚木 茂さんの担当者会議の日程を相談させてください。")))
    web = TestClient(create_app(s, client_factory=lambda: client))
    first = web.post("/users/U04/offer", data={"fp": fp}, follow_redirects=False).headers["location"]
    ids = [r["mail_id"] for r in c.execute("SELECT mail_id FROM mail_out WHERE origin = 'finding' ORDER BY mail_id")]
    html = web.get(first).text
    assert "同じ用件の案" in html and all(f'href="/desk/U04/mail/{i}"' in html for i in ids if f"/mail/{i}" != first[-len(f"/mail/{i}"):])
