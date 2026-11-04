"""ケアプランの下書き: 書類に無い事を足さない、意向は書類の言葉だけ、頻度と期間は読めなければ空欄。"""
import io
import pathlib
from datetime import date

import docx
import pytest
from fastapi.testclient import TestClient

from keika.care_plan import (
    BLANK, NOT_FOUND, OUTPUT_DIR, EditConflict, WireGap, WireGaps, WireItem, WirePlan, WireRowCell, WireRow, WireSection, WireWeek, add_line, add_row, check_against_assessment, edit_cell, edit_history, get_care_plan, list_care_plans, make_care_plan, remove_row, save_to_output, to_docx,
)
from keika.claude_checks import APP_RECORDS
from keika.db import connect
from keika.files import scan
from keika.ledger import User, upsert_user
from keika.progress import user_progress
from keika.reading import DocFacts, _save
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.web.app import create_app
from test_reading import FakeClient, answer

FOLDER = "利用者/U01_試験太郎"
ASSESS = f"{FOLDER}/アセスメント_2026-10-01.txt"
PLAN = f"{FOLDER}/居宅サービス計画書_2026-04-01.txt"
A_TEXT = "本人は「家で暮らし続けたい」と話す。長女は「転ばないでほしい」と話す。膝が痛く、長く歩くと休む。階段は手すりを使う。"
P_TEXT = "デイサービスこなら坂 月曜と木曜 9:00〜16:00 週2回。計画の期間 2026-04-01〜2027-03-31。"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / FOLDER).mkdir(parents=True)
    (root / ASSESS).write_text(A_TEXT, encoding="utf-8")
    (root / PLAN).write_text(P_TEXT, encoding="utf-8")
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 13))
    c = connect(s)
    upsert_user(c, User(id="U01", name="試験 太郎", cm="", data={"type": "居宅介護支援", "care_level": "要介護2", "cert_period": ["2026-04-01", "2028-03-31"]}))
    upsert_user(c, User(id="U02", name="予防 花子", cm="", data={"type": "介護予防支援", "care_level": "要支援1", "cert_period": ["2026-04-01", "2027-03-31"]}))
    refs = scan(s)
    _save(c, next(r for r in refs if r.rel_path == ASSESS), DocFacts(doc_type="アセスメント", summary="アセスメント", doc_date="2026-10-01"))
    _save(c, next(r for r in refs if r.rel_path == PLAN), DocFacts(doc_type="居宅サービス計画書", summary="計画書", doc_date="2026-04-01", period_start="2026-04-01", period_end="2027-03-31"))
    add_record(c, RecordIn(user_id="U01", event_date="2026-11-02", kind_read="居宅訪問", body="デイは楽しく行けていると話す。"))
    yield s, c
    c.close()


def item(text, quote, file=ASSESS, who=""):
    return WireItem(text=text, who=who, file=file, quote=quote)


def cell(key, text, quote, file=ASSESS):
    return WireRowCell(key=key, text=text, who="", file=file, quote=quote)


def wire(**kw):
    data = dict(
        sections=[
            WireSection(key="intent", items=[item("家で暮らし続けたいとのこと", "家で暮らし続けたい", who="本人")]),
            WireSection(key="policy", items=[item("膝の痛みに配慮し、歩く力を保てるよう支援します", "膝が痛く、長く歩くと休む")]),
        ],
        rows=[WireRow(cells=[
            cell("need", "長く歩くと休むので、歩く力を保ちたい", "膝が痛く、長く歩くと休む"),
            cell("long_goal", "休まずに歩ける", "長く歩くと休む"),
            cell("frequency", "週2回", "週2回", PLAN),
            cell("provider", "デイサービスこなら坂", "デイサービスこなら坂", PLAN),
            cell("content", "機能訓練", "デイサービスこなら坂", PLAN),
        ])],
        week=[WireWeek(days="月、木", start="9:00", end="16:00", service="通所介護", file=PLAN, quote="デイサービスこなら坂 月曜と木曜 9:00〜16:00")],
    )
    return WirePlan(**(data | kw))


def make(c, s, w=None, user="U01"):
    return make_care_plan(c, FakeClient(answer(w or wire())), s, user)


def plan_of(c, s, w=None, user="U01"):
    return get_care_plan(c, make(c, s, w, user))


def texts(items):
    return [i["text"] for i in items]


def test_kyotaku_user_gets_tables_one_to_three(env):
    s, c = env
    p = plan_of(c, s)
    assert p.kind == "kyotaku" and p.user_id == "U01"
    assert texts(p.sections["policy"]) == ["膝の痛みに配慮し、歩く力を保てるよう支援します"]
    assert p.rows[0]["need"]["quote"] == "膝が痛く、長く歩くと休む" and p.rows[0]["need"]["file"] == ASSESS
    assert not p.rows[0]["need"]["flag"]


def test_empty_sections_stay_blank_and_are_not_filled(env):
    s, c = env
    p = plan_of(c, s)
    assert texts(p.sections["opinion"]) == [BLANK] and texts(p.sections["analysis"]) == [BLANK]


def test_sentence_with_quote_not_in_the_documents_is_flagged(env):
    s, c = env
    p = plan_of(c, s, wire(sections=[WireSection(key="policy", items=[item("毎日散歩を続けます", "毎日の散歩が日課である")])]))
    assert "確かめられません" in p.sections["policy"][0]["flag"] and p.sections["policy"][0]["quote"] == ""


def test_quote_from_a_file_of_someone_else_is_not_accepted(env):
    s, c = env
    p = plan_of(c, s, wire(sections=[WireSection(key="policy", items=[item("膝に配慮します", "膝が痛く", "利用者/U99_別人/記録.txt")])]))
    assert p.sections["policy"][0]["flag"] and p.sections["policy"][0]["file"] == ""


def test_number_not_in_the_quote_is_flagged(env):
    s, c = env
    p = plan_of(c, s, wire(sections=[WireSection(key="policy", items=[item("500m 歩けるよう支援します", "膝が痛く、長く歩くと休む")])]))
    assert "500" in p.sections["policy"][0]["flag"]


def test_intention_uses_only_words_from_the_documents(env):
    s, c = env
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("安心して自宅での生活を継続したい", "家で暮らし続けたい", who="本人")])]))
    assert texts(p.sections["intent"]) == ["本人: 家で暮らし続けたい"]


def test_made_up_intention_is_left_out(env):
    s, c = env
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("旅行に行きたい", "旅行に行きたい", who="本人")])]))
    assert texts(p.sections["intent"]) == [BLANK]


def test_intention_of_a_person_not_in_the_document_is_flagged(env):
    s, c = env
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("転ばないでほしい", "転ばないでほしい", who="次男")])]))
    assert "誰の言葉" in p.sections["intent"][0]["flag"]


@pytest.mark.parametrize("key", ["frequency", "long_period", "short_period", "period"])
def test_frequency_and_period_are_blank_unless_read(env, key):
    s, c = env
    p = plan_of(c, s, wire(rows=[WireRow(cells=[cell("need", "歩きたい", "長く歩くと休む"), cell(key, "週3回", "週2回", PLAN)])]))
    assert p.rows[0][key]["text"] == BLANK and p.rows[0][key]["quote"] == ""


def test_frequency_read_from_the_document_is_kept(env):
    s, c = env
    assert plan_of(c, s).rows[0]["frequency"]["text"] == "週2回"


def test_period_copied_from_the_old_plan_is_flagged(env):
    s, c = env
    p = plan_of(c, s, wire(rows=[WireRow(cells=[cell("need", "歩きたい", "長く歩くと休む"), cell("period", "2026-04-01〜2027-03-31", "2026-04-01〜2027-03-31", PLAN)])]))
    assert "前の計画" in p.rows[0]["period"]["flag"]


def test_provider_not_in_the_documents_is_blank(env):
    s, c = env
    p = plan_of(c, s, wire(rows=[WireRow(cells=[cell("need", "歩きたい", "長く歩くと休む"), cell("provider", "さくらデイ", "さくらデイ", PLAN)])]))
    assert p.rows[0]["provider"]["text"] == BLANK


@pytest.mark.parametrize("sentence", ["受診が必要です", "デイサービスこなら坂がおすすめです", "薬を減らすとよいです"])
def test_medical_judgement_and_recommendation_stay_out(env, sentence):
    s, c = env
    p = plan_of(c, s, wire(sections=[WireSection(key="policy", items=[item(sentence, "膝が痛く、長く歩くと休む")])]))
    assert texts(p.sections["policy"]) == [BLANK]
    assert p.held == [{"quote": "膝が痛く、長く歩くと休む", "file": ASSESS}]


def test_record_is_a_valid_source(env):
    s, c = env
    p = plan_of(c, s, wire(sections=[WireSection(key="analysis", items=[item("デイに楽しく通えている", "デイは楽しく行けている", APP_RECORDS)])]))
    assert not p.sections["analysis"][0]["flag"] and p.sections["analysis"][0]["file"] == APP_RECORDS


def test_week_keeps_read_days_and_times_and_blanks_the_rest(env):
    s, c = env
    p = plan_of(c, s, wire(week=[
        WireWeek(days="月、木", start="9:00", end="16:00", service="通所介護", file=PLAN, quote="デイサービスこなら坂 月曜と木曜 9:00〜16:00"),
        WireWeek(days="金", start="13:00", end="15:00", service="訪問介護", file=PLAN, quote="デイサービスこなら坂 月曜と木曜 9:00〜16:00"),
    ]))
    assert (p.week[0]["days"], p.week[0]["start"], p.week[0]["end"]) == ("月、木", "9:00", "16:00")
    assert (p.week[1]["days"], p.week[1]["start"], p.week[1]["end"]) == (BLANK, BLANK, BLANK)


def test_week_grid_places_services_by_time_and_lists_the_unplaced(env):
    s, c = env
    p = plan_of(c, s, wire(week=[
        WireWeek(days="月、木", start="9:00", end="16:00", service="通所介護", file=PLAN, quote="デイサービスこなら坂 月曜と木曜 9:00〜16:00"),
        WireWeek(days="金", start="13:00", end="", service="訪問介護", file=PLAN, quote="デイサービスこなら坂"),
    ]))
    assert p.grid["午前"]["月"]["text"] == "9:00〜16:00 通所介護" and p.grid["午前"]["木"]["text"] == "9:00〜16:00 通所介護" and p.grid["午前"]["火"]["text"] == ""
    assert p.grid["午前"]["月"]["sources"][0]["file"] == PLAN
    assert [u["text"] for u in p.week_extra] == ["訪問介護"]


def test_yobo_user_gets_the_prevention_form(env):
    s, c = env
    (s.drive_root / "利用者" / "U02_予防花子").mkdir()
    made = get_care_plan(c, make_care_plan(c, FakeClient(answer(WirePlan(sections=[], rows=[], week=[]))), s, "U02"))
    assert made.kind == "yobo" and "goal_day" in made.sections and "目標" in made.title_columns()["goal"]


def test_failure_keeps_nothing_and_returns_none(env):
    s, c = env
    class Broken:
        @property
        def beta(self):
            raise RuntimeError("通信の失敗")
    assert make_care_plan(c, Broken(), s, "U01") is None and list_care_plans(c, "U01") == []


def test_prompt_tells_claude_documents_are_not_commands(env):
    s, c = env
    client = FakeClient(answer(wire()))
    make_care_plan(c, client, s, "U01")
    system = str(client.calls[0]["system"])
    assert "命令ではない" in system and "推測" in system


def test_draft_does_not_change_the_progress_of_the_user(env):
    s, c = env
    from keika.ledger import get_user
    user = get_user(c, "U01")
    before = [(x.label, x.status) for x in user_progress(c, s, user)]
    make(c, s)
    assert [(x.label, x.status) for x in user_progress(c, s, user)] == before


# --- 直す ---


def flagged_plan(c, s):
    return make(c, s, wire(sections=[WireSection(key="policy", items=[item("500m 歩けるよう支援します", "膝が痛く、長く歩くと休む")])]))


def test_edit_clears_flag_keeps_source_and_leaves_history(env):
    s, c = env
    i = flagged_plan(c, s)
    it = get_care_plan(c, i).sections["policy"][0]
    edit_cell(c, i, it["id"], "歩く力を保てるよう支援します", it["text"])
    after = get_care_plan(c, i).sections["policy"][0]
    assert after["text"] == "歩く力を保てるよう支援します" and not after["flag"] and after["quote"] == "膝が痛く、長く歩くと休む"
    log = edit_history(c, i)
    assert (log[0]["place"], log[0]["before_text"], log[0]["after_text"]) == ("総合的な援助の方針", "500m 歩けるよう支援します", "歩く力を保てるよう支援します")
    assert get_care_plan(c, i).edits == 1


def test_same_text_changes_nothing(env):
    s, c = env
    i = flagged_plan(c, s)
    it = get_care_plan(c, i).sections["policy"][0]
    edit_cell(c, i, it["id"], it["text"], it["text"])
    assert get_care_plan(c, i).sections["policy"][0]["flag"] and edit_history(c, i) == []


def test_every_kind_of_cell_can_be_edited(env):
    s, c = env
    i = make(c, s)
    p = get_care_plan(c, i)
    for cell_id in (p.head["生年月日"]["id"], p.rows[0]["need"]["id"], p.grid["午前"]["火"]["id"], p.sections["reason"][0]["id"]):
        edit_cell(c, i, cell_id, "人が打った")
    p = get_care_plan(c, i)
    assert {p.head["生年月日"]["text"], p.rows[0]["need"]["text"], p.grid["午前"]["火"]["text"], p.sections["reason"][0]["text"]} == {"人が打った"}


def test_cell_can_be_emptied(env):
    s, c = env
    i = make(c, s)
    it = get_care_plan(c, i).sections["policy"][0]
    edit_cell(c, i, it["id"], "")
    assert get_care_plan(c, i).sections["policy"][0]["text"] == ""


def test_edit_made_elsewhere_is_not_overwritten(env):
    s, c = env
    i = make(c, s)
    it = get_care_plan(c, i).sections["policy"][0]
    edit_cell(c, i, it["id"], "別の画面の手直し", it["text"])
    with pytest.raises(EditConflict):
        edit_cell(c, i, it["id"], "古い画面からの文", it["text"])
    assert get_care_plan(c, i).sections["policy"][0]["text"] == "別の画面の手直し"


def test_unknown_cell_is_refused(env):
    s, c = env
    with pytest.raises(KeyError):
        edit_cell(c, make(c, s), "i9999", "x")


def test_saves_to_different_cells_at_once_are_all_kept(env):
    import threading
    s, c = env
    i = make(c, s)
    cells = [d["id"] for d in get_care_plan(c, i).grid["午前"].values()] + [d["id"] for d in get_care_plan(c, i).grid["午後"].values()]

    def save(cell_id):
        own = connect(s)
        edit_cell(own, i, cell_id, "同時の手直し")
        own.close()

    threads = [threading.Thread(target=save, args=(x,)) for x in cells]
    [x.start() for x in threads]
    [x.join() for x in threads]
    p = get_care_plan(c, i)
    assert all(d["text"] == "同時の手直し" for band in ("午前", "午後") for d in p.grid[band].values()) and len(edit_history(c, i)) == len(cells)


def test_history_cannot_be_changed_or_deleted(env):
    import sqlite3
    s, c = env
    i = make(c, s)
    edit_cell(c, i, get_care_plan(c, i).sections["policy"][0]["id"], "直した")
    for sql in ("UPDATE care_plan_edits SET after_text = 'x'", "DELETE FROM care_plan_edits"):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql)


def test_making_again_keeps_the_old_draft_and_its_edits(env):
    s, c = env
    i = make(c, s)
    edit_cell(c, i, get_care_plan(c, i).sections["policy"][0]["id"], "人の手直し")
    j = make(c, s)
    assert j != i and get_care_plan(c, i).sections["policy"][0]["text"] == "人の手直し"
    assert len(list_care_plans(c, "U01")) == 2


def test_add_lines_and_rows_by_hand(env):
    s, c = env
    i = make(c, s)
    add_line(c, i, "policy")
    add_row(c, i)
    p = get_care_plan(c, i)
    assert p.sections["policy"][-1]["text"] == "" and all(x["text"] == "" for x in p.rows[-1].values())
    edit_cell(c, i, p.rows[-1]["need"]["id"], "人が書いた課題")
    assert get_care_plan(c, i).rows[-1]["need"]["text"] == "人が書いた課題"


def test_removing_a_row_keeps_its_words_in_history(env):
    s, c = env
    i = make(c, s)
    remove_row(c, i, get_care_plan(c, i).rows[0]["need"]["id"])
    assert get_care_plan(c, i).rows == []
    assert "長く歩くと休むので" in edit_history(c, i)[0]["before_text"]


# --- 出力先 ---


def test_export_goes_outside_what_is_read_and_changes_no_document(env):
    s, c = env
    before = {r.rel_path: r.sha256 for r in scan(s)}
    p = plan_of(c, s)
    rel = save_to_output(s, p)
    assert rel.startswith(OUTPUT_DIR + "/") and (s.drive_root / rel).is_file()
    assert {r.rel_path: r.sha256 for r in scan(s)} == before  # 点検は読まない。今ある書類も同じ


def test_export_never_overwrites(env, monkeypatch):
    from datetime import datetime
    s, c = env
    p = plan_of(c, s)
    fixed = datetime(2026, 11, 13, 9, 0, 0)
    monkeypatch.setattr("keika.care_plan.now_real", lambda: fixed)
    save_to_output(s, p)
    with pytest.raises(FileExistsError):
        save_to_output(s, p)


def test_export_route_says_where_it_was_put(env):
    s, c = env
    i = make(c, s)
    client = TestClient(create_app(s))
    page = client.post(f"/careplans/{i}/export").text
    assert OUTPUT_DIR in page and "今ある書類は変えていません" in page


def test_export_outside_the_folder_is_impossible(env):
    from keika.settings import safe_path, OutsideRootError
    s, _ = env
    with pytest.raises(OutsideRootError):
        safe_path(s, "../外.docx")


# --- アセスメントとの食い違い ---


def gaps(*found):
    return WireGaps(gaps=list(found))


def test_compare_keeps_only_gaps_quoted_from_the_assessment(env):
    s, c = env
    i = make(c, s)
    run = check_against_assessment(c, FakeClient(answer(gaps(
        WireGap(plan_text="休まずに歩ける", file=ASSESS, quote="膝が痛く、長く歩くと休む", note="アセスメントでは長く歩くと休むとあります"),
        WireGap(plan_text="休まずに歩ける", file=ASSESS, quote="どこにも無い文", note="無い引用"),
        WireGap(plan_text="書いていない文", file=ASSESS, quote="膝が痛く", note="計画に無い文"),
        WireGap(plan_text="休まずに歩ける", file=ASSESS, quote="膝が痛く", note="受診が必要です"),
    ))), s, i)
    assert run is True
    found = get_care_plan(c, i).gaps
    assert len(found) == 1 and found[0]["quote"] == "膝が痛く、長く歩くと休む" and found[0]["note"].endswith("確認してください。")


def test_compare_is_marked_stale_after_an_edit(env):
    s, c = env
    i = make(c, s)
    check_against_assessment(c, FakeClient(answer(gaps())), s, i)
    assert get_care_plan(c, i).gaps_fresh
    it = get_care_plan(c, i).sections["policy"][0]
    edit_cell(c, i, it["id"], "直した方針")
    assert not get_care_plan(c, i).gaps_fresh


def test_compare_without_assessment_says_so(env):
    s, c = env
    i = make_care_plan(c, FakeClient(answer(WirePlan(sections=[], rows=[], week=[]))), s, "U02")
    assert check_against_assessment(c, FakeClient(answer(gaps())), s, i) is False


# --- Word と画面 ---


def doc_text(data):
    d = docx.Document(io.BytesIO(data))
    return "\n".join([p.text for p in d.paragraphs] + [cell.text for t in d.tables for r in t.rows for cell in r.cells])


def test_docx_has_standard_labels_and_marks(env):
    s, c = env
    i = make(c, s, wire(sections=[WireSection(key="policy", items=[item("500m 歩けるよう支援します", "膝が痛く、長く歩くと休む")])]))
    text = doc_text(to_docx(get_care_plan(c, i)))
    for word in ("第１表", "居宅サービス計画書（１）", "第２表", "居宅サービス計画書（２）", "第３表", "週間サービス計画表", "利用者名", "居宅サービス計画作成者氏名", "認定の有効期間", "要介護状態区分",
                 "利用者及び家族の生活に対する意向を踏まえた課題分析の結果", "介護認定審査会の意見及びサービスの種類の指定", "総合的な援助の方針", "生活援助中心型の算定理由",
                 "生活全般の解決すべき課題（ニーズ）", "長期目標", "短期目標", "サービス内容", "サービス種別", "頻度", "期間", "主な日常生活上の活動", "週単位以外のサービス",
                 "下書き", "【確かめる】500m", "試験 太郎", "要介護2", "2026-04-01"):
        assert word in text, word


def test_docx_shows_what_a_person_typed(env):
    s, c = env
    i = make(c, s)
    edit_cell(c, i, get_care_plan(c, i).grid["午前"]["火"]["id"], "人が打った予定")
    assert "人が打った予定" in doc_text(to_docx(get_care_plan(c, i)))


def test_docx_has_plain_tables_that_other_editors_can_take(env):
    """結合したマスは、変換の時に崩れやすく、打ち直しにくい。A4 横で幅を決める。"""
    s, c = env
    data = to_docx(plan_of(c, s))
    d = docx.Document(io.BytesIO(data))
    xml = "".join(t._tbl.xml for t in d.tables)
    assert "w:gridSpan" not in xml and "w:vMerge" not in xml
    assert "w:tblHeader" in xml
    section = d.sections[0]
    assert round(section.page_width.mm) == 297 and round(section.page_height.mm) == 210
    usable = section.page_width.mm - section.left_margin.mm - section.right_margin.mm
    for table in d.tables:
        assert sum(col.width.mm for col in table.columns) <= usable + 1


def test_docx_does_not_say_consent_or_delivery_is_done(env):
    s, c = env
    text = doc_text(to_docx(plan_of(c, s)))
    assert "同意し" not in text and "交付を受け" not in text and "交付済" not in text and "同意済" not in text


def test_page_is_the_form_with_a_box_in_every_cell(env):
    s, c = env
    i = make(c, s)
    p = get_care_plan(c, i)
    page = TestClient(create_app(s)).get(f"/careplans/{i}").text
    assert "AI の案" in page and "確定は人" in page and f"/careplans/{i}/docx" in page and "膝が痛く、長く歩くと休む" in page
    assert "/doc?path=" in page and "同意済" not in page and "交付済" not in page
    assert "第１表" in page and "第２表" in page and "第３表" in page
    for cell_id in (p.head["住所"]["id"], p.rows[0]["need"]["id"], p.rows[0]["benefit"]["id"], p.grid["夜間"]["日"]["id"]):
        assert f'data-item="{cell_id}"' in page
    assert "/static/care_plan.js" in page


def test_typed_html_is_shown_as_text(env):
    s, c = env
    i = make(c, s)
    edit_cell(c, i, get_care_plan(c, i).sections["policy"][0]["id"], "<script>alert(1)</script>")
    page = TestClient(create_app(s)).get(f"/careplans/{i}").text
    assert "<script>alert(1)</script>" not in page


def test_cell_route_saves_and_reports_conflict(env):
    s, c = env
    i = make(c, s)
    it = get_care_plan(c, i).sections["policy"][0]
    client = TestClient(create_app(s))
    assert client.post(f"/careplans/{i}/cells", data={"item": it["id"], "text": "直した方針", "before": it["text"], "before_sent": "1"}).status_code == 200
    assert client.post(f"/careplans/{i}/cells", data={"item": it["id"], "text": "古い文", "before": it["text"], "before_sent": "1"}).status_code == 409
    assert client.post(f"/careplans/{i}/cells", data={"item": "i9999", "text": "x"}).status_code == 404
    assert get_care_plan(c, i).sections["policy"][0]["text"] == "直した方針"


def test_history_is_on_the_page(env):
    s, c = env
    i = make(c, s)
    edit_cell(c, i, get_care_plan(c, i).sections["policy"][0]["id"], "直した方針")
    assert "直した方針" in TestClient(create_app(s)).get(f"/careplans/{i}").text.split("変更の履歴")[1]


def test_page_is_not_kept_for_the_back_button(env):
    s, c = env
    assert TestClient(create_app(s)).get(f"/careplans/{make(c, s)}").headers["cache-control"] == "no-store"


def test_sample_mode_page_is_read_only(env):
    s, c = env
    i = make(c, s)
    page = TestClient(create_app(s, client_factory=lambda: None, sample=True)).get(f"/careplans/{i}").text
    assert "readonly" in page and "事業所のフォルダの出力先に置く" not in page


def test_user_page_has_button_and_list(env):
    s, c = env
    i = make(c, s)
    page = TestClient(create_app(s)).get("/users/U01").text
    assert 'action="/users/U01/careplan"' in page and f"/careplans/{i}" in page


def test_make_route_redirects_to_the_new_draft(env):
    s, c = env
    client = TestClient(create_app(s, client_factory=lambda: FakeClient(answer(wire()))))
    r = client.post("/users/U01/careplan", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/careplans/")


def test_make_route_says_when_it_failed(env):
    s, c = env
    def no_key():
        raise RuntimeError("キーがありません")
    client = TestClient(create_app(s, client_factory=no_key))
    assert "作れませんでした" in client.post("/users/U01/careplan").text


def test_docx_route(env):
    s, c = env
    i = make(c, s)
    edit_cell(c, i, get_care_plan(c, i).sections["policy"][0]["id"], "直した方針")
    r = TestClient(create_app(s)).get(f"/careplans/{i}/docx")
    assert "wordprocessingml" in r.headers["content-type"] and "直した方針" in doc_text(r.content)


def test_unknown_routes_are_404(env):
    s, _ = env
    client = TestClient(create_app(s))
    assert client.post("/users/U99/careplan").status_code == 404 and client.get("/careplans/999").status_code == 404


def test_sample_mode_refuses_and_hides_the_button(env):
    s, c = env
    client = TestClient(create_app(s, client_factory=lambda: None, sample=True))
    assert client.post("/users/U01/careplan").status_code == 403
    assert client.post("/careplans/1/cells", data={"item": "i1", "text": "x"}).status_code == 403
    assert 'action="/users/U01/careplan"' not in client.get("/users/U01").text


# --- 本物の Claude で分かったこと ---


def test_quote_from_a_document_of_the_user_not_sent_in_full_is_accepted(env):
    s, c = env
    other = f"{FOLDER}/支援経過_2026-08-01_2026-11-03.csv"
    (s.drive_root / other).write_text("入浴はデイで週3回。", encoding="utf-8")
    p = plan_of(c, s, wire(rows=[WireRow(cells=[cell("need", "歩きたい", "長く歩くと休む"), cell("frequency", "週3回", "入浴はデイで週3回。", other)])]))
    assert p.rows[0]["frequency"]["text"] == "週3回" and p.rows[0]["frequency"]["file"] == other


def test_document_of_someone_else_in_the_folder_is_not_a_source(env):
    s, c = env
    other = f"{FOLDER}/取り違え_2026-08-01.txt"
    (s.drive_root / other).write_text("別の人は週5回通う。", encoding="utf-8")
    _save(c, next(r for r in scan(s) if r.rel_path == other), DocFacts(doc_type="その他", summary="別人", user_name="別人 次郎"))
    p = plan_of(c, s, wire(rows=[WireRow(cells=[cell("need", "歩きたい", "長く歩くと休む"), cell("frequency", "週5回", "別の人は週5回通う。", other)])]))
    assert p.rows[0]["frequency"]["text"] == BLANK


def test_intention_quote_cut_at_a_line_end_is_read_to_the_end_of_the_sentence(env):
    s, c = env
    (s.drive_root / ASSESS).write_text("妻「転ばないか心配。自分も腰が弱いので、お\n風呂はデイでお願いしたい」と話す。", encoding="utf-8")
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("転ばないか心配", "自分も腰が弱いので、お", who="妻")])]))
    assert texts(p.sections["intent"]) == ["妻: 自分も腰が弱いので、お風呂はデイでお願いしたい"]


def test_intention_does_not_repeat_who_said_it(env):
    s, c = env
    (s.drive_root / ASSESS).write_text("本人「できるだけ自分の足で歩きたい」", encoding="utf-8")
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("歩きたい", "本人「できるだけ自分の足で歩きたい」", who="本人")])]))
    assert texts(p.sections["intent"]) == ["本人: できるだけ自分の足で歩きたい"]


def test_speaker_is_found_when_the_quote_crosses_a_line_end(env):
    s, c = env
    (s.drive_root / ASSESS).write_text("妻：転ばないか心配。自分も腰が弱いので、お\n風呂はデイでお願いしたい。", encoding="utf-8")
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("心配", "自分も腰が弱いので、お\n風呂はデイでお願いしたい。", who="妻")])]))
    assert not p.sections["intent"][0]["flag"]


def test_short_answer_of_the_committee_opinion_is_not_flagged(env):
    s, c = env
    (s.drive_root / PLAN).write_text("介護認定審査会の意見及びサービスの種類の指定\nなし", encoding="utf-8")
    p = plan_of(c, s, wire(sections=[WireSection(key="opinion", items=[item("なし", "なし", PLAN)])]))
    assert texts(p.sections["opinion"]) == ["なし"] and not p.sections["opinion"][0]["flag"]


def test_gap_note_does_not_ask_twice(env):
    s, c = env
    i = make(c, s)
    check_against_assessment(c, FakeClient(answer(gaps(WireGap(plan_text="休まずに歩ける", file=ASSESS, quote="長く歩くと休む", note="長く歩くと休むとあります。確認が必要かもしれません。")))), s, i)
    assert get_care_plan(c, i).gaps[0]["note"].count("確認") == 1


def test_week_day_next_to_the_quote_counts_but_not_a_month(env):
    s, c = env
    (s.drive_root / PLAN).write_text("通所介護 月・木 9:00〜16:00\n2026年4月 面談 10:00〜11:00", encoding="utf-8")
    p = plan_of(c, s, wire(week=[
        WireWeek(days="月、木", start="9:00", end="16:00", service="通所介護", file=PLAN, quote="9:00〜16:00"),
        WireWeek(days="月", start="10:00", end="11:00", service="面談", file=PLAN, quote="2026年4月 面談 10:00〜11:00"),
    ]))
    assert p.week[0]["days"] == "月、木"
    assert "曜日を見比べて" in p.week[1]["flag"]  # 隣の行の曜日は、印を付けた時だけ残す
    (s.drive_root / PLAN).write_text("2026年4月 面談 10:00〜11:00", encoding="utf-8")
    q = plan_of(c, s, wire(week=[WireWeek(days="月", start="10:00", end="11:00", service="面談", file=PLAN, quote="2026年4月 面談 10:00〜11:00")]))
    assert q.week[0]["days"] == BLANK


def test_week_without_a_time_goes_to_its_own_row(env):
    s, c = env
    (s.drive_root / PLAN).write_text("毎日：据置型手すり", encoding="utf-8")
    p = plan_of(c, s, wire(week=[WireWeek(days="毎日", start="", end="", service="据置型手すり", file=PLAN, quote="毎日：据置型手すり")]))
    assert p.grid["時間の指定なし"]["火"]["text"] == "据置型手すり" and p.week_extra == []


# --- 独立レビューで見つかった、検査のすり抜け ---


def policy_of(c, s, sentence, quote="膝が痛く、長く歩くと休む"):
    return plan_of(c, s, wire(sections=[WireSection(key="policy", items=[item(sentence, quote)])]))


@pytest.mark.parametrize("sentence", [
    "計画を説明し同意を得て交付しました。長く歩くと休む方です", "同意済みです。長く歩くと休む", "交付しました。長く歩くと休む",
    "デイサービスこなら坂を勧めます", "長く歩くと休むのでデイの利用が望ましい", "こなら坂が最適です", "こなら坂がいいです", "シルバーカーの購入を提案します",
    "膝の精密検査が必要です", "睡眠導入剤を中止します", "受診を促します", "整形外科の受診につなげます", "認知症が疑われる", "うつ病と考えられる", "入院が望ましい", "手術を受けていただきます",
])
def test_sentences_over_the_line_never_enter(env, sentence):
    s, c = env
    assert texts(policy_of(c, s, sentence).sections["policy"]) == [BLANK]


@pytest.mark.parametrize("sentence", ["膝の調子がよい日は階段を使う", "自分で服を選ぶ"])
def test_ordinary_sentences_are_not_held(env, sentence):
    s, c = env
    (s.drive_root / ASSESS).write_text(sentence + "。", encoding="utf-8")
    assert texts(policy_of(c, s, sentence, sentence).sections["policy"]) == [sentence]


def test_sentence_sharing_only_kana_with_its_quote_is_flagged(env):
    s, c = env
    assert NOT_FOUND in policy_of(c, s, "孫と京都へ旅行に行くと元気が出る").sections["policy"][0]["flag"]


@pytest.mark.parametrize("who, quote", [("本人", "転ばないでほしい"), ("長女", "家で暮らし続けたい")])
def test_words_given_to_the_wrong_person_are_flagged(env, who, quote):
    s, c = env
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item(quote, quote, who=who)])]))
    assert "誰の言葉" in p.sections["intent"][0]["flag"]


def test_right_person_is_not_flagged(env):
    s, c = env
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("x", "転ばないでほしい", who="長女")])]))
    assert not p.sections["intent"][0]["flag"]


def test_next_person_words_are_not_joined(env):
    s, c = env
    (s.drive_root / ASSESS).write_text("本人：家で暮らしたい\n長女：施設も考えてほしい\n次男：費用が心配", encoding="utf-8")
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("x", "家で暮らしたい", who="本人")])]))
    assert texts(p.sections["intent"]) == ["本人: 家で暮らしたい"]


def test_saving_during_compare_is_kept(env):
    s, c = env
    i = make(c, s)
    cell_id = get_care_plan(c, i).sections["policy"][0]["id"]
    from keika import care_plan as cp
    real = cp.ask_structured

    def slow(*a, **k):
        edit_cell(c, i, cell_id, "読み比べ中の手直し")
        return real(*a, **k)

    cp_patch = pytest.MonkeyPatch()
    cp_patch.setattr(cp, "ask_structured", slow)
    try:
        check_against_assessment(c, FakeClient(answer(gaps())), s, i)
    finally:
        cp_patch.undo()
    p = get_care_plan(c, i)
    assert p.sections["policy"][0]["text"] == "読み比べ中の手直し" and not p.gaps_fresh


def test_summary_of_someone_elses_document_is_not_sent(env):
    s, c = env
    other = f"{FOLDER}/取り違え_2026-08-01.txt"
    (s.drive_root / other).write_text("別の人の話。", encoding="utf-8")
    _save(c, next(r for r in scan(s) if r.rel_path == other), DocFacts(doc_type="その他", summary="別人の要点", user_name="別人 次郎"))
    client = FakeClient(answer(wire()))
    make_care_plan(c, client, s, "U01")
    assert "別人の要点" not in str(client.calls[0]["messages"])


def test_control_characters_do_not_break_word(env):
    s, c = env
    i = make(c, s, wire(week=[WireWeek(days="月", start="9:00", end="16:00", service="通所\x0c介護", file=PLAN, quote="デイサービスこなら坂 月曜と木曜 9:00〜16:00")]))
    p = get_care_plan(c, i)
    edit_cell(c, i, p.grid["午前"]["火"]["id"], "訪問\x0b介護")
    assert TestClient(create_app(s)).get(f"/careplans/{i}/docx").status_code == 200


def test_flag_on_a_week_entry_reaches_word(env):
    s, c = env
    data = to_docx(plan_of(c, s, wire(week=[WireWeek(days="月", start="9:00", end="16:00", service="買い物同行500m", file=PLAN, quote="デイサービスこなら坂 月曜と木曜 9:00〜16:00")])))
    assert "【確かめる】9:00〜16:00 買い物同行500m" in doc_text(data)


@pytest.mark.parametrize("key, value, quote", [("frequency", "", "週2回"), ("policy_text", "", "膝が痛く"), ("need", BLANK, "膝が痛く")])
def test_empty_answers_stay_blank_without_marks(env, key, value, quote):
    s, c = env
    if key == "policy_text":
        p = plan_of(c, s, wire(sections=[WireSection(key="policy", items=[item(value, quote)])]))
        assert texts(p.sections["policy"]) == [BLANK] and not p.sections["policy"][0]["flag"]
    else:
        file = PLAN if key == "frequency" else ASSESS
        p = plan_of(c, s, wire(rows=[WireRow(cells=[cell("long_goal", "休まずに歩ける", "長く歩くと休む"), cell(key, value, quote, file)])]))
        assert p.rows[0][key]["text"] == BLANK and not p.rows[0][key]["flag"]


def test_row_with_nothing_in_it_is_dropped(env):
    s, c = env
    assert plan_of(c, s, wire(rows=[WireRow(cells=[cell("need", BLANK, "膝が痛く")])])).rows == []


def test_document_never_sent_cannot_back_up_a_value(env):
    s, c = env
    other = f"{FOLDER}/写真_2026-08-01.png"
    (s.drive_root / other).write_bytes(b"\x89PNG")
    p = plan_of(c, s, wire(rows=[WireRow(cells=[cell("need", "歩きたい", "長く歩くと休む"), cell("frequency", "週5回", "週5回", other)])]))
    assert p.rows[0]["frequency"]["text"] == BLANK


def test_short_committee_answer_needs_the_committee_nearby(env):
    s, c = env
    (s.drive_root / ASSESS).write_text("転倒歴なし。", encoding="utf-8")
    p = plan_of(c, s, wire(sections=[WireSection(key="opinion", items=[item("なし", "なし")])]))
    assert p.sections["opinion"][0]["flag"]


def test_day_and_time_on_another_line_are_not_borrowed(env):
    s, c = env
    (s.drive_root / PLAN).write_text("デイサービス 月曜 9:00〜16:00\n訪問介護 木曜 13:00〜15:00", encoding="utf-8")
    p = plan_of(c, s, wire(week=[WireWeek(days="木", start="13:00", end="15:00", service="デイサービス", file=PLAN, quote="デイサービス 月曜 9:00〜16:00")]))
    assert (p.week[0]["start"], p.week[0]["end"]) == (BLANK, BLANK)  # 別の行の時刻は借りない
    assert p.week[0]["days"] == "木" and "曜日を見比べて" in p.week[0]["flag"]  # 表では曜日が別の行に出るので、印を付けて残す


def test_day_far_from_the_quote_is_blank(env):
    s, c = env
    (s.drive_root / PLAN).write_text("木曜 別の話\n間の行\nデイサービス 9:00〜16:00", encoding="utf-8")
    p = plan_of(c, s, wire(week=[WireWeek(days="木", start="9:00", end="16:00", service="デイサービス", file=PLAN, quote="デイサービス 9:00〜16:00")]))
    assert p.week[0]["days"] == BLANK


def test_minutes_are_compared_too(env):
    s, c = env
    p = plan_of(c, s, wire(week=[WireWeek(days="月", start="9:45", end="16:50", service="通所介護", file=PLAN, quote="デイサービスこなら坂 月曜と木曜 9:00〜16:00")]))
    assert (p.week[0]["start"], p.week[0]["end"]) == (BLANK, BLANK)


@pytest.mark.parametrize("key, value, quote", [("provider", "デイ", "デイサービスこなら坂"), ("frequency", "2", "週2回")])
def test_scrap_of_a_word_is_not_a_value(env, key, value, quote):
    s, c = env
    p = plan_of(c, s, wire(rows=[WireRow(cells=[cell("need", "歩きたい", "長く歩くと休む"), cell(key, value, quote, PLAN)])]))
    assert p.rows[0][key]["text"] == BLANK


def test_provider_only_tried_once_is_not_a_value(env):
    s, c = env
    (s.drive_root / PLAN).write_text("さくらデイは見学したが合わなかった。", encoding="utf-8")
    p = plan_of(c, s, wire(rows=[WireRow(cells=[cell("need", "歩きたい", "長く歩くと休む"), cell("provider", "さくらデイ", "さくらデイは見学したが合わなかった", PLAN)])]))
    assert p.rows[0]["provider"]["text"] == BLANK


def test_gap_with_only_an_empty_ask_or_a_recommendation_is_dropped(env):
    s, c = env
    i = make(c, s)
    check_against_assessment(c, FakeClient(answer(gaps(
        WireGap(plan_text="休まずに歩ける", file=ASSESS, quote="長く歩くと休む", note="確認が必要かもしれません"),
        WireGap(plan_text="休まずに歩ける", file=ASSESS, quote="長く歩くと休む", note="デイサービスこなら坂がおすすめです"),
    ))), s, i)
    assert get_care_plan(c, i).gaps == []


def test_support_level_without_a_type_is_the_prevention_form(env):
    s, c = env
    upsert_user(c, User(id="U03", name="支援 三郎", cm="", data={"type": "", "care_level": "要支援1"}))
    (s.drive_root / "利用者" / "U03_支援三郎").mkdir()
    assert get_care_plan(c, make_care_plan(c, FakeClient(answer(WirePlan(sections=[], rows=[], week=[]))), s, "U03")).kind == "yobo"


def test_head_uses_what_the_ledger_knows(env):
    s, c = env
    upsert_user(c, User(id="U01", name="試験 太郎", cm="", data={"type": "居宅介護支援", "care_level": "要介護2", "birth": "1940-01-02", "address": "架空市1-2"}))
    p = plan_of(c, s)
    assert p.head["生年月日"]["text"] == "1940-01-02" and p.head["住所"]["text"] == "架空市1-2"


def test_weekdays_word_is_monday_to_friday(env):
    from keika.care_plan import _days_of
    assert _days_of("平日") == list("月火水木金") and _days_of("土日") == list("土日")


def test_huge_number_in_the_address_is_not_found(env):
    s, _ = env
    assert TestClient(create_app(s)).get("/careplans/99999999999999999999").status_code == 404


def test_form_items_from_the_standard_are_there(env):
    s, c = env
    p = plan_of(c, s)
    assert "認定の状態" in p.head and "深夜（0〜4時）" in p.grid and "深夜" in p.grid
    text = doc_text(to_docx(p))
    assert "作成年月日" in text and "月分より" in text


def test_prevention_form_items_from_the_standard_are_there(env):
    s, c = env
    (s.drive_root / "利用者" / "U02_予防花子").mkdir()
    p = get_care_plan(c, make_care_plan(c, FakeClient(answer(WirePlan(sections=[], rows=[], week=[]))), s, "U02"))
    text = doc_text(to_docx(p))
    for word in ("認定年月日", "初回作成日", "初回・紹介・継続", "認定済・申請中", "担当地域包括支援センター", "基本チェックリストの該当数", "必要な事業プログラム", "健康状態についての留意点", "総合的課題", "課題に対する目標と具体策の提案", "本来行うべき支援ができない場合",
                 "現在の状況", "本人・家族の意欲・意向", "領域における課題", "目標についての支援のポイント"):
        assert word in text, word


# --- 2回目の独立レビュー ---


def test_empty_cell_also_detects_an_edit_made_elsewhere(env):
    s, c = env
    i = make(c, s)
    cell_id = get_care_plan(c, i).grid["午前"]["火"]["id"]
    client = TestClient(create_app(s))
    form = {"item": (None, cell_id), "text": (None, "画面Aの文"), "before": (None, ""), "before_sent": (None, "1")}
    assert client.post(f"/careplans/{i}/cells", files=form).status_code == 200
    form["text"] = (None, "画面Bの文")
    assert client.post(f"/careplans/{i}/cells", files=form).status_code == 409
    assert get_care_plan(c, i).grid["午前"]["火"]["text"] == "画面Aの文"


def test_text_starting_with_a_line_break_survives_the_page(env):
    s, c = env
    i = make(c, s)
    cell_id = get_care_plan(c, i).grid["午前"]["火"]["id"]
    edit_cell(c, i, cell_id, "\n改行で始まる文")
    page = TestClient(create_app(s)).get(f"/careplans/{i}").text
    assert f'data-item="{cell_id}"' in page and '>\n\n改行で始まる文</textarea>' in page


def test_resend_after_a_lost_answer_is_not_a_conflict(env):
    s, c = env
    i = make(c, s)
    it = get_care_plan(c, i).sections["policy"][0]
    edit_cell(c, i, it["id"], "直した方針", it["text"])
    edit_cell(c, i, it["id"], "直した方針", it["text"])
    assert len(edit_history(c, i)) == 1


@pytest.mark.parametrize("next_line", ["医師は薬を中止すべきと話す。", "ケアマネ：デイの回数を増やしましょう。", "認知症の疑いと言われた。", "同意を得た。計画書を交付した。"])
def test_finished_words_are_not_joined_to_the_next_line(env, next_line):
    s, c = env
    (s.drive_root / ASSESS).write_text(f"本人：家で暮らしたい\n{next_line}", encoding="utf-8")
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("x", "家で暮らしたい", who="本人")])]))
    assert texts(p.sections["intent"]) == ["本人: 家で暮らしたい"]


def test_words_read_over_a_line_end_get_a_mark(env):
    s, c = env
    (s.drive_root / ASSESS).write_text("妻：自分も腰が弱いので、お\n風呂はデイでお願いしたい。", encoding="utf-8")
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("x", "自分も腰が弱いので、お", who="妻")])]))
    assert texts(p.sections["intent"]) == ["妻: 自分も腰が弱いので、お風呂はデイでお願いしたい"] and "行をまたいで" in p.sections["intent"][0]["flag"]


@pytest.mark.parametrize("sentence, quote", [
    ("前回の計画を説明し同意を得た", "前回の計画を説明し同意を得た"), ("計画に同意をいただきました", "長く歩くと休む"), ("計画は説明済みです", "長く歩くと休む"), ("計画書をお渡ししました", "長く歩くと休む"),
    ("計画の交付を完了", "長く歩くと休む"), ("受診しましょう", "長く歩くと休む"), ("膝の痛みには通院が必要です", "膝が痛く、長く歩くと休む"), ("福祉用具を東西社で利用するとよいでしょう", "長く歩くと休む"),
])
def test_done_judgement_and_recommendation_are_held_even_if_quoted(env, sentence, quote):
    s, c = env
    (s.drive_root / ASSESS).write_text(A_TEXT + quote, encoding="utf-8")
    assert texts(policy_of(c, s, sentence, quote).sections["policy"]) == [BLANK]


def test_word_not_in_any_document_is_flagged(env):
    s, c = env
    p = policy_of(c, s, "膝の痛みに配慮し、東西社の杖を使います")
    assert "東西社" in p.sections["policy"][0]["flag"] and "配慮" not in p.sections["policy"][0]["flag"]


@pytest.mark.parametrize("line", ["本人の意向：自宅で暮らしたい。", "利用者の希望：自宅で暮らしたい。", "意向：自宅で暮らしたい。"])
def test_form_labels_are_not_taken_for_speakers(env, line):
    s, c = env
    (s.drive_root / ASSESS).write_text(line, encoding="utf-8")
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("x", "自宅で暮らしたい。", who="本人")])]))
    assert not p.sections["intent"][0]["flag"]


def test_speaker_on_the_line_above_is_seen(env):
    s, c = env
    (s.drive_root / ASSESS).write_text("長女：\n施設に入れたくない。", encoding="utf-8")
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("x", "施設に入れたくない。", who="本人")])]))
    assert "誰の言葉" in p.sections["intent"][0]["flag"]


@pytest.mark.parametrize("line, words", [("長女さん：施設は考えていない。", "施設は考えていない"), ("長女は「施設は考えていない」", "施設は考えていない")])
def test_speaker_words_are_removed_whole(env, line, words):
    s, c = env
    (s.drive_root / ASSESS).write_text(line, encoding="utf-8")
    p = plan_of(c, s, wire(sections=[WireSection(key="intent", items=[item("x", line, who="長女")])]))
    assert texts(p.sections["intent"]) == [f"長女: {words}"]


@pytest.mark.parametrize("line, days", [("毎週月曜 9:00〜15:00 通所介護", "月"), ("通所水曜 9:00〜15:00", "水"), ("週2回(月・木) 9:00〜15:00", "月、木"), ("(月) 9:00〜15:00", "月"), ("月木 9:00〜15:00", "月、木"), ("平日 9:00〜15:00", "平日")])
def test_usual_ways_of_writing_days_are_read(env, line, days):
    s, c = env
    (s.drive_root / PLAN).write_text(line, encoding="utf-8")
    p = plan_of(c, s, wire(week=[WireWeek(days=days, start="9:00", end="15:00", service="通所介護", file=PLAN, quote=line)]))
    assert p.week[0]["days"] == days


def test_every_month_is_not_monday(env):
    s, c = env
    (s.drive_root / PLAN).write_text("毎月 9:00〜15:00 面談", encoding="utf-8")
    p = plan_of(c, s, wire(week=[WireWeek(days="月", start="9:00", end="15:00", service="面談", file=PLAN, quote="毎月 9:00〜15:00 面談")]))
    assert p.week[0]["days"] == BLANK


def test_monday_written_in_full_is_not_sunday_too():
    from keika.care_plan import _days_of
    assert _days_of("月曜日") == ["月"]


@pytest.mark.parametrize("line, start", [("月曜 9時30分〜15時", "9:30"), ("月曜 10時15分〜15時", "10:15"), ("月曜 9時00分〜15時", "9:00"), ("月曜 09:30〜15時", "9:30")])
def test_time_written_with_minutes_is_read(env, line, start):
    s, c = env
    (s.drive_root / PLAN).write_text(line, encoding="utf-8")
    p = plan_of(c, s, wire(week=[WireWeek(days="月", start=start, end="", service="通所介護", file=PLAN, quote=line)]))
    assert p.week[0]["start"] == start


@pytest.mark.parametrize("value, quote", [("週1回", "隔週1回"), ("月1回", "3か月1回"), ("週3回", "週3回の利用は断った"), ("週", "週2回")])
def test_part_of_a_frequency_that_changes_the_meaning_is_blank(env, value, quote):
    s, c = env
    (s.drive_root / PLAN).write_text(quote, encoding="utf-8")
    p = plan_of(c, s, wire(rows=[WireRow(cells=[cell("need", "歩きたい", "長く歩くと休む"), cell("frequency", value, quote, PLAN)])]))
    assert p.rows[0]["frequency"]["text"] == BLANK


@pytest.mark.parametrize("user_id", ["../利用者/U01_試験太郎/x", "../../外", "a/b"])
def test_export_stays_in_its_folder_whatever_the_user_id_is(env, user_id):
    from dataclasses import replace
    s, c = env
    p = replace(plan_of(c, s), user_id=user_id)
    rel = save_to_output(s, p)
    assert rel.count("/") == 1 and rel.startswith(OUTPUT_DIR + "/") and (s.drive_root / rel).is_file()


def test_script_treats_a_removed_cell_as_a_conflict_and_marks_before():
    from keika.web import app as web
    js = (pathlib.Path(web.__file__).parent / "static" / "care_plan.js").read_text(encoding="utf-8")
    assert 'body.append("before_sent", "1")' in js and "r.status === 404" in js and "defaultValue" in js and "new Blob" in js


def test_saved_text_with_old_line_ends_can_be_edited(env):
    s, c = env
    i = make(c, s)
    row = c.execute("SELECT payload FROM care_plans WHERE id = ?", (i,)).fetchone()
    import json
    d = json.loads(row["payload"])
    d["sections"]["policy"][0]["text"] = "一行目\r\n二行目"
    c.execute("UPDATE care_plans SET payload = ? WHERE id = ?", (json.dumps(d, ensure_ascii=False), i))
    c.commit()
    edit_cell(c, i, d["sections"]["policy"][0]["id"], "直した", "一行目\n二行目")
    assert get_care_plan(c, i).sections["policy"][0]["text"] == "直した"
