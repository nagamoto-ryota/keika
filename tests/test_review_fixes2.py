"""訪問前の1枚まわりの独立レビューで見つかった不具合の再発防止。"""
import io
from datetime import date

import docx
import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.claude_checks import crosses_line
from keika.db import connect
from keika.drafts import (
    WireDraft, WireItem, WireSection, finish_homework, get_draft, make_draft, mark_checked, open_homework, sync_review, to_docx, update_draft,
)
from keika.files import scan
from keika.ledger import User, get_user, seed_from_json, upsert_user
from keika.reading import DocFacts, TextFact, _save
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.visit_questions import WireArea, WireBasic, WireCheck, WireGap, WireQuestions, get_questions, make_questions
from keika.visit_sheet import build_sheet
from keika.web.app import create_app
from test_reading import FakeClient, answer

FOLDER = "利用者/U09_予防花子"
ASSESS = f"{FOLDER}/アセスメント.txt"
SCAN = f"{FOLDER}/スキャン.pdf"
BODY = "主訴: 買い物に行きたい。趣味は編み物です。階段を手すりや壁をつたわらずに昇っていません。身長150cm 体重40kg。"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / FOLDER).mkdir(parents=True)
    (root / ASSESS).write_text(BODY, encoding="utf-8")
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 13))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    upsert_user(c, User(id="U09", name="予防 花子", cm="CM01", data={"type": "介護予防支援", "care_level": "要支援1"}))
    _save(c, next(r for r in scan(s) if r.rel_path == ASSESS), DocFacts(doc_type="アセスメント", summary="a", doc_date="2026-04-22", user_name="予防 花子"))
    yield s, c
    c.close()


def wire(**kw):
    data = dict(assessment=[], checklist_past=[], basic_info=[], intentions=[])
    return WireQuestions(**(data | kw))


def make(s, c, w, user="U09"):
    return make_questions(c, FakeClient(answer(w)), s, user)


def basic(past, file=ASSESS, quote="趣味は編み物です", question="今も続けているか聞く", field="趣味"):
    return WireBasic(field=field, question=question, past=past, file=file, quote=quote)


# --- 「前回」の裏付け ---


def _scan(s, c):
    (s.drive_root / SCAN).write_bytes(b"%PDF-1.4 x")
    _save(c, next(r for r in scan(s) if r.rel_path == SCAN), DocFacts(doc_type="アセスメント", summary="s", doc_date="2026-05-01", user_name="予防 花子"))


def test_scan_past_is_marked_to_check_against_paper(env, monkeypatch):
    s, c = env
    _scan(s, c)
    monkeypatch.setattr("keika.files._check_pdf", lambda raw: None)
    q = make(s, c, wire(basic_info=[basic("昨年3回転倒した", file=SCAN, quote="作り話の引用です")]))
    assert "現物" in q["basic_info"][0]["past"]


def test_past_from_document_not_sent_to_claude_is_dropped(env):
    s, c = env
    other = s.drive_root / FOLDER / "契約書.pdf"
    other.write_bytes(b"%PDF-1.4 x")
    q = make(s, c, wire(basic_info=[basic("昨年3回転倒した", file=f"{FOLDER}/契約書.pdf", quote="作り話の引用です")]))
    assert q["basic_info"][0]["past"] == ""


def test_past_unrelated_to_its_quote_is_dropped(env):
    s, c = env
    assert make(s, c, wire(basic_info=[basic("杖で屋外を歩いている", quote="主訴")]))["basic_info"][0]["past"] == ""
    assert make(s, c, wire(basic_info=[basic("杖で屋外を歩いている", quote="買い物に行きたい")]))["basic_info"][0]["past"] == ""


def test_past_with_number_not_in_quote_is_dropped(env):
    s, c = env
    assert make(s, c, wire(basic_info=[basic("編み物を週3回", quote="趣味は編み物です")]))["basic_info"][0]["past"] == ""


def test_checklist_answer_needs_quote_about_that_question(env):
    s, c = env
    q = make(s, c, wire(checklist_past=[WireCheck(no=9, answer="はい", value="", file=ASSESS, quote="買い物に行きたい")]))
    assert q["checklist"][8]["past"] == ""
    q = make(s, c, wire(checklist_past=[WireCheck(no=6, answer="いいえ", value="", file=ASSESS, quote="階段を手すりや壁をつたわらずに昇っていません")]))
    assert q["checklist"][5]["past"] == "いいえ"


def test_height_weight_needs_the_numbers_in_quote(env):
    s, c = env
    q = make(s, c, wire(checklist_past=[WireCheck(no=12, answer="身長150 体重40", value="", file=ASSESS, quote="身長150cm 体重40kg")]))
    assert q["checklist"][11]["past"] == "身長150 体重40"
    q = make(s, c, wire(checklist_past=[WireCheck(no=12, answer="身長160 体重50", value="", file=ASSESS, quote="身長150cm 体重40kg")]))
    assert q["checklist"][11]["past"] == ""


# --- 聞き方の検査 ---


@pytest.mark.parametrize("text", ["病院に行くべきか聞く", "認知症かどうか聞く", "主治医に診てもらう必要があるか聞く", "服薬を中止すべきか"])
def test_medical_questions_cross_the_line(text):
    assert crosses_line(text)


def test_question_with_fact_not_in_documents_is_dropped(env):
    s, c = env
    q = make(s, c, wire(assessment=[WireGap(no=10, state="薄い", question="先月転んで入院した後の様子を聞く", past="", file="", quote="")]))
    assert q["assessment"] == []


def test_field_with_medical_judgement_is_dropped(env):
    s, c = env
    assert make(s, c, wire(basic_info=[basic("", field="服薬を中止すべきか")]))["basic_info"] == []


# --- 他の人の書類 ---


def _foreign_plan(s, c):
    path = s.drive_root / FOLDER / "計画書.txt"
    path.write_text("畑仕事をしたい。", encoding="utf-8")
    _save(c, next(r for r in scan(s) if r.rel_path.endswith("計画書.txt")),
          DocFacts(doc_type="介護予防サービス計画書", summary="p", user_name="柚木 茂", period_start="2026-01-01", period_end="2026-12-31", goals=[TextFact(text="畑に出る")]))


def test_foreign_plan_does_not_enter_the_sheet_and_is_warned(env):
    s, c = env
    _foreign_plan(s, c)
    sheet = build_sheet(c, s, "U09")
    assert sheet.goals == [] and sheet.plan_end is None and any("計画書.txt" in f for f in sheet.foreign)
    assert "別の人の名前" in TestClient(create_app(s)).get("/users/U09/visit").text


def test_foreign_document_is_not_read_or_quoted(env):
    s, c = env
    _foreign_plan(s, c)
    client = FakeClient(answer(wire(basic_info=[basic("畑仕事", file=f"{FOLDER}/計画書.txt", quote="畑仕事をしたい")])))
    q = make_questions(c, client, s, "U09")
    assert q["basic_info"][0]["past"] == "" and "畑仕事をしたい" not in str(client.calls[0]["messages"])


# --- 壊れた書類、古い結果 ---


def test_broken_document_is_skipped(env):
    s, c = env
    path = s.drive_root / FOLDER / "壊れた.docx"
    path.write_bytes(b"not a docx")
    _save(c, next(r for r in scan(s) if r.rel_path.endswith("壊れた.docx")), DocFacts(doc_type="アセスメント", summary="x", doc_date="2026-06-01", user_name="予防 花子"))
    assert make(s, c, wire(assessment=[WireGap(no=18, state="薄い", question="歯の具合を聞く", past="", file="", quote="")]))["assessment"]


def test_sheet_says_results_are_old_after_new_record(env):
    s, c = env
    make(s, c, wire(assessment=[WireGap(no=18, state="薄い", question="歯の具合を聞く", past="", file="", quote="")]))
    client = TestClient(create_app(s))
    assert "作り直してください" not in client.get("/users/U09/visit").text
    add_record(c, RecordIn(user_id="U09", event_date="2026-11-10", kind_read="電話", body="電話した。"))
    assert "作り直してください" in client.get("/users/U09/visit").text


def test_prompt_has_no_leftover_instruction_for_other_tool(env):
    s, c = env
    client = FakeClient(answer(wire()))
    make_questions(c, client, s, "U09")
    assert "report_finding" not in str(client.calls[0]["messages"])


def test_height_weight_row_has_its_own_answer_space(env):
    s, c = env
    make(s, c, wire())
    assert "身長　　cm　体重　　kg" in TestClient(create_app(s)).get("/users/U09/visit").text


# --- 訪問後の記録の並び ---


def test_later_call_on_same_day_as_visit_is_listed(env):
    s, c = env
    add_record(c, RecordIn(user_id="U09", event_date="2026-11-10", event_time="16:00", kind_read="電話", body="夕方の電話。"))
    add_record(c, RecordIn(user_id="U09", event_date="2026-11-10", event_time="10:00", kind_read="居宅訪問", body="朝の訪問。"))
    assert [r.body for r in build_sheet(c, s, "U09").since_visit] == ["夕方の電話。"]


# --- 宿題 ---

TALK = "柚木様のお宅で担当者会議です。長男が来月までに施設の見学に行くそうです。特になしです。"


def draft_of(s, c, items, user="U04"):
    r = add_record(c, RecordIn(user_id=user, event_date="2026-11-05", kind_read="会議", monitoring=False, body="会議。"))
    w = WireDraft(place="", start_time="", end_time="", attendees=[], sections=[WireSection(key="remaining", items=items)])
    return r, make_draft(c, FakeClient(answer(w)), s, r, TALK, "会議.txt", review_id=77)


def test_unverified_homework_says_to_check(env):
    s, c = env
    draft_of(s, c, [WireItem(text="長男が来月までに施設の見学に行く。", quote="")])
    assert open_homework(c, "U04")[0].flag
    page = TestClient(create_app(s)).get("/users/U04/visit").text
    assert "【確かめる】長男が来月までに施設の見学に行く。" in page


def test_nothing_special_is_not_homework(env):
    s, c = env
    draft_of(s, c, [WireItem(text="特になし。", quote="特になしです")])
    assert open_homework(c, "U04") == []


def test_rewritten_finished_homework_comes_back(env):
    s, c = env
    _, i = draft_of(s, c, [WireItem(text="長男が来月までに施設の見学に行く。", quote="長男が来月までに施設の見学に行くそうです")])
    h = open_homework(c, "U04")[0]
    finish_homework(c, i, h.item_id)
    update_draft(c, i, place="", start_time="", end_time="", attendees=[], sections={"remaining": [{"id": h.item_id, "text": "全く別の宿題。"}]})
    assert [x.text for x in open_homework(c, "U04")] == ["全く別の宿題。"]


def test_review_moves_checked_draft_too(env):
    s, c = env
    _, i = draft_of(s, c, [WireItem(text="長男が来月までに施設の見学に行く。", quote="長男が来月までに施設の見学に行くそうです")])
    mark_checked(c, i)
    sync_review(c, 77, "U05", None)
    assert open_homework(c, "U04") == [] and len(open_homework(c, "U05")) == 1
    sync_review(c, 77, None, None)
    assert open_homework(c, "U05") == []


def test_moving_record_to_another_user_moves_its_draft(env):
    s, c = env
    r, i = draft_of(s, c, [WireItem(text="長男が来月までに施設の見学に行く。", quote="長男が来月までに施設の見学に行くそうです")])
    data = dict(user_id="U05", event_date=r.event_date, event_time="", kind_written="", partner="", body=r.body, kind_read="会議", author="")
    TestClient(create_app(s)).post(f"/records/{r.id}/edit", data=data, follow_redirects=False)
    assert get_draft(c, i).user_id == "U05"


# --- Word ---


@pytest.mark.parametrize("bad", ["\x0b", "\x0c", "\x01", "\x1b", "￾"])
def test_word_export_survives_control_characters(env, bad):
    s, c = env
    _, i = draft_of(s, c, [WireItem(text="長男が来月までに施設の見学に行く。", quote="")])
    d = get_draft(c, i)
    d.place = f"自宅{bad}"
    d.sections["remaining"][0]["text"] = f"宿題{bad}です"
    out = docx.Document(io.BytesIO(to_docx(d, get_user(c, "U04").name, "")))
    assert any("宿題" in p.text for p in out.paragraphs)


def test_word_has_meeting_count_field_to_fill(env):
    s, c = env
    _, i = draft_of(s, c, [WireItem(text="長男が来月までに施設の見学に行く。", quote="")])
    d = docx.Document(io.BytesIO(to_docx(get_draft(c, i), "柚木 茂", "")))
    assert "開催回数" in " ".join(cell.text for t in d.tables for row in t.rows for cell in row.cells)


# --- 下書きの数字 ---


def test_number_changed_from_the_quote_is_flagged_even_if_said_nearby(env):
    s, c = env
    r = add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="会議", monitoring=False, body="会議。"))
    text = "デイは週2回のまま。訪問介護は週3回。"
    w = WireDraft(place="", start_time="", end_time="", attendees=[], sections=[WireSection(key="conclusion", items=[WireItem(text="デイは週3回に増やす。", quote="デイは週2回のまま")])])
    i = make_draft(c, FakeClient(answer(w)), s, r, text, "会議.txt")
    assert get_draft(c, i).sections["conclusion"][0]["flag"]


def test_questions_without_saved_result_are_none(env):
    _, c = env
    assert get_questions(c, "U09") is None
