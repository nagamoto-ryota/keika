"""訪問前に聞くことの洗い出し: アセスメントの抜け、基本チェックリスト、基本情報、予防の意向。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.files import scan
from keika.ledger import User, seed_from_json, upsert_user
from keika.reading import DocFacts, _save
from keika.settings import Settings
from keika.visit_questions import (
    AREAS, CHECKLIST, STANDARD_ITEMS, WireArea, WireBasic, WireCheck, WireGap, WireQuestions, checklist_due, get_questions, make_questions,
)
from keika.web.app import create_app
from test_reading import FakeClient, answer

FOLDER = "利用者/U09_予防花子"
ASSESS = "アセスメント_2026-04-22.txt"
QUOTE = "階段は手すりを使って昇る"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / FOLDER).mkdir(parents=True)
    (root / FOLDER / ASSESS).write_text(f"主訴: 買い物に行きたい。{QUOTE}。趣味は編み物。", encoding="utf-8")
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 13))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    upsert_user(c, User(id="U09", name="予防 花子", cm="CM01", data={"type": "介護予防支援", "care_level": "要支援1", "cert_period": ["2026-04-01", "2027-03-31"]}))
    _save(c, next(r for r in scan(s) if r.rel_path.endswith(ASSESS)), DocFacts(doc_type="アセスメント", summary="アセスメント", doc_date="2026-04-22"))
    yield s, c
    c.close()


FILE = f"{FOLDER}/{ASSESS}"


def wire(**kw):
    data = dict(
        assessment=[WireGap(no=18, state="書かれていない", question="歯や入れ歯の具合を聞く", past="", file="", quote="")],
        checklist_past=[WireCheck(no=6, answer="はい", value="", file=FILE, quote=QUOTE)],
        basic_info=[WireBasic(field="趣味・楽しみ", question="今も続けているか聞く", past="編み物", file=FILE, quote="趣味は編み物")],
        intentions=[WireArea(area="運動・移動", past="買い物に行きたい", file=FILE, quote="買い物に行きたい")],
    )
    return WireQuestions(**(data | kw))


def make(s, c, w, user="U09"):
    return make_questions(c, FakeClient(answer(w)), s, user)


def test_official_lists_have_the_right_size_and_wording():
    assert len(CHECKLIST) == 25 and CHECKLIST[1] == "バスや電車で１人で外出していますか" and "身長" in CHECKLIST[12]
    assert len(STANDARD_ITEMS) == 23 and STANDARD_ITEMS[7] == "主訴・意向" and STANDARD_ITEMS[18] == "口腔内の状況"
    assert len(AREAS) == 4


def test_gap_gets_official_item_name(env):
    s, c = env
    q = make(s, c, wire())
    assert q["assessment"][0]["item"] == "口腔内の状況" and q["assessment"][0]["question"] == "歯や入れ歯の具合を聞く"


def test_unknown_item_number_is_dropped(env):
    s, c = env
    q = make(s, c, wire(assessment=[WireGap(no=99, state="薄い", question="x", past="", file="", quote="")]))
    assert q["assessment"] == []


def test_past_needs_a_real_quote(env):
    s, c = env
    q = make(s, c, wire(basic_info=[WireBasic(field="趣味", question="聞く", past="釣り", file=FILE, quote="趣味は釣り")]))
    assert q["basic_info"][0]["past"] == ""


def test_past_from_other_users_file_is_dropped(env):
    s, c = env
    q = make(s, c, wire(basic_info=[WireBasic(field="趣味", question="聞く", past="釣り", file="利用者/U01/x.txt", quote="趣味は編み物")]))
    assert q["basic_info"][0]["past"] == ""


def test_medical_judgement_question_is_dropped(env):
    s, c = env
    q = make(s, c, wire(assessment=[WireGap(no=10, state="薄い", question="すぐ受診が必要か聞く", past="", file="", quote="")]))
    assert q["assessment"] == []


def test_checklist_lists_all_25_with_past_where_known(env):
    s, c = env
    q = make(s, c, wire())
    assert len(q["checklist"]) == 25
    row = q["checklist"][5]
    assert row["no"] == 6 and row["question"] == CHECKLIST[6] and row["past"] == "はい"
    assert q["checklist"][0]["past"] == ""


def test_checklist_past_without_real_quote_is_blank(env):
    s, c = env
    q = make(s, c, wire(checklist_past=[WireCheck(no=6, answer="いいえ", value="", file=FILE, quote="作り話です")]))
    assert q["checklist"][5]["past"] == ""


def test_area_questions_cover_all_four_for_preventive_users(env):
    s, c = env
    q = make(s, c, wire())
    assert [a["area"] for a in q["intentions"]] == list(AREAS)
    assert q["intentions"][0]["past"] == "買い物に行きたい"


def test_care_user_gets_only_assessment_gaps(env):
    s, c = env
    folder = s.drive_root / "利用者" / "U04_柚木茂"
    folder.mkdir()
    (folder / "アセスメント.txt").write_text("主訴: 庭に出たい。", encoding="utf-8")
    _save(c, next(r for r in scan(s) if r.rel_path.endswith("アセスメント.txt")), DocFacts(doc_type="アセスメント", summary="a", doc_date="2026-04-22"))
    q = make(s, c, wire(), user="U04")
    assert q["assessment"] and q["checklist"] == [] and q["basic_info"] == [] and q["intentions"] == []


def test_checklist_due_when_none_on_file(env):
    s, c = env
    assert checklist_due(c, s, c.execute("SELECT 1").fetchone() and __import__("keika.ledger", fromlist=["get_user"]).get_user(c, "U09")) is True


def test_failure_keeps_nothing(env):
    s, c = env
    assert make_questions(c, FakeClient(RuntimeError("切断")), s, "U09") is None
    assert get_questions(c, "U09") is None


def test_documents_are_sent_to_claude_and_treated_as_material(env):
    s, c = env
    client = FakeClient(answer(wire()))
    make_questions(c, client, s, "U09")
    sent = str(client.calls[0]["messages"])
    assert QUOTE in sent and "命令ではない" in str(client.calls[0]["system"])


def test_sheet_offers_button_then_shows_questions(env):
    s, c = env
    client = TestClient(create_app(s, client_factory=lambda: FakeClient(answer(wire()))))
    assert "聞くことを洗い出す" in client.get("/users/U09/visit").text
    assert client.post("/users/U09/visit/questions", follow_redirects=False).status_code == 303
    page = client.get("/users/U09/visit").text
    for word in ("口腔内の状況", "歯や入れ歯の具合を聞く", "バスや電車で１人で外出していますか", "前回: はい", "運動・移動", "買い物に行きたい", "今も続けているか聞く", "前回: 編み物"):
        assert word in page
    assert "聞くことを作り直す" in page


def test_copy_text_includes_questions(env):
    s, c = env
    client = TestClient(create_app(s, client_factory=lambda: FakeClient(answer(wire()))))
    client.post("/users/U09/visit/questions")
    page = client.get("/users/U09/visit").text
    # コピーの文はこの印から画面で組み立てる
    assert 'data-section="基本チェックリスト" data-label="6 階段を手すりや壁をつたわらずに昇っていますか" data-past="はい"' in page
    assert 'data-section="アセスメントで聞けていないこと"' in page and "/static/visit_sheet.js" in page


def test_failure_shows_message(env):
    s, c = env
    client = TestClient(create_app(s, client_factory=lambda: FakeClient(RuntimeError("切断"))))
    r = client.post("/users/U09/visit/questions")
    assert r.status_code == 200 and "作れませんでした" in r.text


def test_sample_mode_hides_button(env):
    s, c = env
    page = TestClient(create_app(s, client_factory=lambda: None, sample=True)).get("/users/U09/visit").text
    assert "聞くことを洗い出す" not in page
