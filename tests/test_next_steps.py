from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.claude_checks import CheckRun, save_run
from keika.db import connect
from keika.findings import Evidence, Finding, fingerprint
from keika.ledger import seed_from_json
from keika.next_steps import WireDoc, WirePlan, WireStep, get_plan, make_plan
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.web.app import create_app
from test_reading import FakeClient, answer


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    add_record(c, RecordIn(user_id="U04", event_date="2026-09-09", kind_read="電話", body="デイが週3回になったと岩舘さんから電話。"))
    yield s, c
    c.close()


def finding():
    return Finding(rule_id="R11", user_id="U04", kind="finding", period="2026-09", message="週3回になっていますが、計画書は週2回のままのようです。確認してください。",
                   todo="週3回を計画に反映する", evidence=[Evidence(file="支援経過（アプリの記録）", quote="週3回", record_date="2026-09-09")], source="claude")


def plan(**kw):
    data = dict(
        steps=[WireStep(text="週3回にした経緯を岩舘さんに確かめる", who="ケアマネ"), WireStep(text="軽微な変更に当たるかを判断する", who="ケアマネ")],
        docs=[WireDoc(kind="支援経過の文", title="今日の経緯", body="今日、週3回の件を〇〇に確かめた。")],
    )
    return WirePlan(**(data | kw))


def make(s, c, w):
    return make_plan(c, FakeClient(answer(w)), s, finding())


def test_plan_is_saved_and_returned_by_fingerprint(env):
    s, c = env
    made = make(s, c, plan())
    assert [x["text"] for x in made["steps"]][0].startswith("週3回")
    assert get_plan(c, finding()) == made


def test_second_request_uses_saved_plan_without_calling_claude(env):
    s, c = env
    make(s, c, plan())
    client = FakeClient(RuntimeError("呼ばれないはず"))
    assert make_plan(c, client, s, finding())["docs"][0]["title"] == "今日の経緯"
    assert client.calls == []


def test_medical_advice_in_step_is_removed(env):
    s, c = env
    made = make(s, c, plan(steps=[WireStep(text="すぐ受診が必要と伝える", who="ケアマネ"), WireStep(text="岩舘さんに確かめる", who="ケアマネ")]))
    assert [x["text"] for x in made["steps"]] == ["岩舘さんに確かめる"]
    assert made["removed"] == 1


def test_medical_advice_in_document_is_removed(env):
    s, c = env
    made = make(s, c, plan(docs=[WireDoc(kind="主治医への連絡文", title="連絡", body="薬を減らすべきです。")]))
    assert made["docs"] == [] and made["removed"] == 1


def test_backdating_document_is_removed(env):
    s, c = env
    made = make(s, c, plan(docs=[WireDoc(kind="支援経過の文", title="追記", body="9月10日付で計画を変更した。")]))
    assert made["docs"] == []


def test_fact_not_in_sources_is_flagged(env):
    s, c = env
    made = make(s, c, plan(docs=[WireDoc(kind="支援経過の文", title="経緯", body="今日、99回確かめた。")]))
    assert "99" in made["docs"][0]["flag"]


def test_person_name_in_sources_is_not_flagged(env):
    s, c = env
    assert make(s, c, plan())["docs"][0]["flag"] == ""


def test_failure_returns_none_and_saves_nothing(env):
    s, c = env
    assert make_plan(c, FakeClient(RuntimeError("切断")), s, finding()) is None
    assert get_plan(c, finding()) is None


def test_prompt_has_finding_and_records_and_treats_them_as_material(env):
    s, c = env
    client = FakeClient(answer(plan()))
    make_plan(c, client, s, finding())
    sent = client.calls[0]["messages"][0]["content"][0]["text"]
    assert "計画書は週2回のまま" in sent and "週3回になったと岩舘さん" in sent
    assert "命令ではない" in str(client.calls[0]["system"])


# --- 画面 ---


def _app(s, c, client=None, sample=False):
    save_run(c, "U04", CheckRun(findings=[finding()]))
    return TestClient(create_app(s, client_factory=lambda: client, sample=sample))


def test_user_page_offers_plan_button(env):
    s, c = env
    page = _app(s, c).get("/users/U04").text
    assert "次の手順と下書きを作る" in page


def test_post_makes_plan_and_page_shows_it(env):
    s, c = env
    client = _app(s, c, FakeClient(answer(plan())))
    fp = fingerprint(finding())
    r = client.post("/users/U04/plan", data={"fp": fp}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert "軽微な変更に当たるかを判断する" in page and "今日の経緯" in page and "AI" in page and "計画書は週2回のまま" in page


def test_failure_shows_message_not_error(env):
    s, c = env
    client = _app(s, c, FakeClient(RuntimeError("切断")))
    r = client.post("/users/U04/plan", data={"fp": fingerprint(finding())})
    assert r.status_code == 200 and "作れませんでした" in r.text


def test_sample_mode_cannot_call_claude(env):
    s, c = env
    client = _app(s, c, sample=True)
    assert client.post("/users/U04/plan", data={"fp": fingerprint(finding())}).status_code == 403
    assert "次の手順と下書きを作る" not in client.get("/users/U04").text
