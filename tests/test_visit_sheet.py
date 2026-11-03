"""Word の書き出し、会議の宿題、訪問前の1枚。"""
import io
from datetime import date

import docx
import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.claude_checks import CheckRun, save_run
from keika.db import connect
from keika.drafts import (
    WireAttendee, WireDraft, WireItem, WireSection, finish_homework, get_draft, make_draft, mark_checked, open_homework, to_docx, update_draft,
)
from keika.files import scan
from keika.findings import Evidence, Finding
from keika.ledger import seed_from_json
from keika.reading import DocFacts, ServiceFact, TextFact, _save
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.tasks import build_board
from keika.visit_sheet import build_sheet
from keika.web.app import create_app
from test_reading import FakeClient, answer

TALK = "柚木様のお宅で14時から担当者会議を始めます。能登です。デイは週2回のまま続ける、これで決まりでいいですね。ショートステイは必要になったら再相談します。"
KEPT = "デイは週2回のまま続ける"
LATER = "ショートステイは必要になったら再相談します"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者" / "U04_柚木茂").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 13))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def meeting(s, c, flagged=False):
    r = add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="会議", monitoring=False, body="会議を開いた。"))
    w = WireDraft(
        place="柚木様のお宅", start_time="14:00", end_time="",
        attendees=[WireAttendee(org="", role="", name="能登", quote="能登です")],
        sections=[
            WireSection(key="conclusion", items=[WireItem(text="デイは週2回のまま続ける。", quote="" if flagged else KEPT)]),
            WireSection(key="remaining", items=[WireItem(text="ショートステイは必要になったら再相談する。", quote=LATER)]),
        ],
    )
    return make_draft(c, FakeClient(answer(w)), s, r, TALK, "会議.txt")


def doc_text(data: bytes) -> str:
    d = docx.Document(io.BytesIO(data))
    cells = [cell.text for t in d.tables for row in t.rows for cell in row.cells]
    return "\n".join([p.text for p in d.paragraphs] + cells)


# --- Word ---


def test_docx_has_standard_fields_and_text(env):
    s, c = env
    d = get_draft(c, meeting(s, c))
    text = doc_text(to_docx(d, "柚木 茂", "井波 拓真"))
    for word in ("サービス担当者会議の要点", "柚木 茂", "井波 拓真", "2026-11-05", "柚木様のお宅", "能登", "結論", "デイは週2回のまま続ける。", "残された課題"):
        assert word in text


def test_docx_marks_sentences_still_to_check(env):
    s, c = env
    d = get_draft(c, meeting(s, c, flagged=True))
    assert "【確かめる】デイは週2回のまま続ける。" in doc_text(to_docx(d, "柚木 茂", ""))


def test_download_returns_word_file_with_date_in_name(env):
    s, c = env
    i = meeting(s, c)
    r = TestClient(create_app(s)).get(f"/drafts/{i}/docx")
    assert r.status_code == 200 and "wordprocessingml" in r.headers["content-type"]
    assert "2026-11-05" in r.headers["content-disposition"]
    assert "デイは週2回" in doc_text(r.content)


def test_draft_page_explains_where_to_put_the_file(env):
    s, c = env
    page = TestClient(create_app(s)).get(f"/drafts/{meeting(s, c)}").text
    assert f"/drafts/{meeting(s, c) - 1}/docx" in page and "利用者のフォルダ" in page


def test_saved_word_file_can_be_read_back_as_text(env, tmp_path):
    s, c = env
    d = get_draft(c, meeting(s, c))
    path = s.drive_root / "利用者" / "U04_柚木茂" / "要点.docx"
    path.write_bytes(to_docx(d, "柚木 茂", ""))
    from keika.files import to_payload
    ref = next(r for r in scan(s) if r.rel_path.endswith("要点.docx"))
    assert "デイは週2回のまま続ける。" in to_payload(s, ref).text


# --- 宿題 ---


def test_remaining_items_become_homework(env):
    s, c = env
    i = meeting(s, c)
    hw = open_homework(c, "U04")
    assert [h.text for h in hw] == ["ショートステイは必要になったら再相談する。"]
    assert hw[0].draft_id == i and hw[0].event_date == "2026-11-05"


def test_blank_section_is_not_homework(env):
    s, c = env
    r = add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="会議", monitoring=False, body="会議。"))
    make_draft(c, FakeClient(answer(WireDraft(place="", start_time="", end_time="", attendees=[], sections=[]))), s, r, TALK, "会議.txt")
    assert open_homework(c, "U04") == []


def test_checked_draft_keeps_its_homework(env):
    s, c = env
    mark_checked(c, meeting(s, c))
    assert len(open_homework(c, "U04")) == 1


def test_finished_homework_leaves_and_survives_edit(env):
    s, c = env
    i = meeting(s, c)
    h = open_homework(c, "U04")[0]
    finish_homework(c, i, h.item_id)
    assert open_homework(c, "U04") == []
    item = get_draft(c, i).sections["remaining"][0]
    update_draft(c, i, place="", start_time="", end_time="", attendees=[], sections={"remaining": [{"id": item["id"], "text": "直した課題。"}]})
    assert open_homework(c, "U04") == []


def test_edited_text_shows_in_homework(env):
    s, c = env
    i = meeting(s, c)
    item = get_draft(c, i).sections["remaining"][0]
    update_draft(c, i, place="", start_time="", end_time="", attendees=[], sections={"remaining": [{"id": item["id"], "text": "直した課題。"}]})
    assert open_homework(c, "U04")[0].text == "直した課題。"


def test_unknown_item_is_refused(env):
    s, c = env
    with pytest.raises(ValueError):
        finish_homework(c, meeting(s, c), "i999")


def test_homework_is_on_the_todo_board(env):
    s, c = env
    meeting(s, c)
    person = next(p for p in build_board(c, s).people if p.user.id == "U04")
    row = next(t for t in person.tasks if t.homework)
    assert row.title == "宿題・持ち越し 1件" and "ショートステイ" in row.homework[0].text


def test_todo_page_has_done_button_and_source_link(env):
    s, c = env
    i = meeting(s, c)
    page = TestClient(create_app(s)).get("/").text
    assert "ショートステイ" in page and f"/drafts/{i}" in page and "済んだ" in page


def test_done_button_removes_it_from_todo(env):
    s, c = env
    i = meeting(s, c)
    h = open_homework(c, "U04")[0]
    client = TestClient(create_app(s))
    assert client.post(f"/drafts/{i}/items/{h.item_id}/done", follow_redirects=False).status_code == 303
    assert "ショートステイ" not in client.get("/").text.split("書類の下書きの確認")[0]
    assert open_homework(c, "U04") == []


def test_sample_mode_has_no_done_button(env):
    s, c = env
    meeting(s, c)
    page = TestClient(create_app(s, client_factory=lambda: None, sample=True)).get("/").text
    assert "ショートステイ" in page and "済んだ" not in page


# --- 訪問前の1枚 ---


def plan_facts(s, c):
    path = s.drive_root / "利用者" / "U04_柚木茂" / "計画書.txt"
    path.write_text("計画書", encoding="utf-8")
    ref = next(r for r in scan(s) if r.rel_path.endswith("計画書.txt"))
    _save(c, ref, DocFacts(doc_type="居宅サービス計画書", summary="計画書", period_start="2025-12-01", period_end="2026-11-30",
                           goals=[TextFact(text="自分で歩いて庭に出る")], services=[ServiceFact(name="デイサービスこなら坂", per_week=2)]))


def finding(**kw):
    data = dict(rule_id="R11", user_id="U04", kind="finding", period="2026-09", message="週3回のようです。確認してください。", todo="週3回を計画に反映する",
                evidence=[Evidence(file="支援経過（アプリの記録）", quote="週3回", record_date="2026-09-09")], source="claude")
    return Finding(**(data | kw))


def test_sheet_has_last_visit_homework_checks_goals_and_deadlines(env):
    s, c = env
    plan_facts(s, c)
    add_record(c, RecordIn(user_id="U04", event_date="2026-09-01", kind_read="居宅訪問", body="古い訪問。"))
    add_record(c, RecordIn(user_id="U04", event_date="2026-10-20", kind_read="居宅訪問", body="10月の訪問の様子。"))
    add_record(c, RecordIn(user_id="U04", event_date="2026-10-25", kind_read="電話", body="電話の話。"))
    meeting(s, c)
    save_run(c, "U04", CheckRun(findings=[finding()]))
    sheet = build_sheet(c, s, "U04")
    assert sheet.last_visit.event_date == "2026-10-20"
    assert [r.event_date for r in sheet.since_visit] == ["2026-10-25", "2026-11-05"]
    assert [h.text for h in sheet.homework] == ["ショートステイは必要になったら再相談する。"]
    assert "週3回を計画に反映する" in [x["title"] for x in sheet.checks]
    assert sheet.goals == ["自分で歩いて庭に出る"]
    assert sheet.plan_end == "2026-11-30"
    assert sheet.services == ["デイサービスこなら坂 週2回"]


def test_sheet_says_when_visited_this_month(env):
    s, c = env
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-02", kind_read="居宅訪問", body="今月の訪問。"))
    assert build_sheet(c, s, "U04").visited_this_month == "2026-11-02"


def test_sheet_without_records_or_documents_still_opens(env):
    s, c = env
    client = TestClient(create_app(s))
    r = client.get("/users/U04/visit")
    assert r.status_code == 200 and "訪問前の1枚" in r.text and "まだありません" in r.text


def test_sheet_page_shows_all_parts_and_is_printable(env):
    s, c = env
    plan_facts(s, c)
    add_record(c, RecordIn(user_id="U04", event_date="2026-10-20", kind_read="居宅訪問", body="10月の訪問の様子。"))
    meeting(s, c)
    save_run(c, "U04", CheckRun(findings=[finding()]))
    page = TestClient(create_app(s)).get("/users/U04/visit").text
    for word in ("10月の訪問の様子。", "ショートステイ", "週3回を計画に反映する", "自分で歩いて庭に出る", "デイサービスこなら坂", "印刷する", "メモ", "柚木 茂"):
        assert word in page


def test_sheet_does_not_list_unknown_user(env):
    s, _ = env
    assert TestClient(create_app(s)).get("/users/U99/visit").status_code == 404


def test_user_page_links_the_sheet(env):
    s, _ = env
    assert "/users/U04/visit" in TestClient(create_app(s)).get("/users/U04").text
