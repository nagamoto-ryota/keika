"""ケアプランとアセスメントの下書きのマス: 元の書類は押した時だけ出す、見比べたい語だけ赤くする、
直すか〔これでいい〕で赤が消える（履歴に残る）、日付の書き方違いや元にある語のつながりは赤くしない。"""
import json
import re

from fastapi.testclient import TestClient

from keika.care_plan import COMPARE, BLANK, WireItem, WirePlan, WireSection, edit_cell, edit_history, get_care_plan, make_care_plan
from keika.claude_checks import APP_RECORDS
from keika.records import RecordIn, add_record
from keika.web.app import create_app
from test_care_plan import ASSESS, env  # noqa: F401  （env は fixture）
from test_reading import FakeClient, answer

RECORD = "夜間のトイレは毎回恵子さんが起きて付き添っている。次女は近くに住む。"


def plan_with(s, c, *items):
    add_record(c, RecordIn(user_id="U01", event_date="2026-11-02", kind_read="居宅訪問", body=RECORD))
    wire = WirePlan(sections=[WireSection(key=k, items=[WireItem(text=t, who="", file=f, quote=q)]) for k, t, q, f in items], rows=[], week=[])
    pid = make_care_plan(c, FakeClient(answer(wire)), s, "U01", kind="assessment")
    return get_care_plan(c, pid)


def cell(p, key):
    return p.sections[key][0]


def test_date_written_differently_is_not_marked(env):
    s, c = env
    p = plan_with(s, c, ("a1", "2026-11-02に居宅訪問し、付き添いの話を聞いた。", "毎回恵子さんが起きて付き添っている", APP_RECORDS),
                  ("a2", "11/2の訪問で、夜間のトイレは毎回付き添うと聞いた。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    assert cell(p, "a1")["flag"] == "" and cell(p, "a2")["flag"] == ""


def test_words_joined_from_the_source_are_not_marked_but_new_ones_are(env):
    s, c = env
    p = plan_with(s, c, ("a16", "夜間のトイレは毎回次女が付き添う。", "毎回恵子さんが起きて付き添っている", APP_RECORDS),
                  ("a10", "夜間のトイレで入院した。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    assert cell(p, "a16")["flag"] == "" and cell(p, "a16")["words"] == []
    assert "入院" in cell(p, "a10")["words"] and cell(p, "a10")["flag"]


def page(s, p):
    return TestClient(create_app(s)).get(f"/careplans/{p.id}").text


def test_source_is_hidden_until_the_sentence_is_pressed(env):
    s, c = env
    p = plan_with(s, c, ("a11", "階段は手すりを使う", "階段は手すりを使う", ASSESS))
    html = page(s, p)
    box = html.split(f'id="cell-{cell(p, "a11")["id"]}"')[1].split("</details>")[0]
    assert '<details class="cell-more">' in html.split(f'id="cell-{cell(p, "a11")["id"]}"')[1][:400]
    assert box.index("<summary") < box.index("元の書類") and "<details class=\"cell-more\" open" not in html
    assert "▸ 元の書類" not in html and "<summary class=\"muted\">元の書類</summary>" not in html


def test_only_the_words_to_compare_are_red_and_the_long_note_is_gone(env):
    s, c = env
    p = plan_with(s, c, ("a10", "夜間のトイレで入院した。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    html = page(s, p)
    assert '<mark class="cmp">入院</mark>' in html
    assert COMPARE not in html


def test_editing_the_cell_clears_the_red_and_the_count(env):
    s, c = env
    p = plan_with(s, c, ("a10", "夜間のトイレで入院した。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    before = p.flagged
    edit_cell(c, p.id, cell(p, "a10")["id"], "夜間のトイレは毎回付き添う。")
    q = get_care_plan(c, p.id)
    assert cell(q, "a10")["flag"] == "" and cell(q, "a10")["words"] == [] and q.flagged == before - 1


def test_this_is_fine_clears_the_red_and_stays_in_the_history(env):
    s, c = env
    p = plan_with(s, c, ("a10", "夜間のトイレで入院した。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    client = TestClient(create_app(s))
    assert ">これでいい</button>" in client.get(f"/careplans/{p.id}").text
    r = client.post(f"/careplans/{p.id}/ok", data={"item": cell(p, "a10")["id"]}, follow_redirects=False)
    assert r.status_code == 303
    q = get_care_plan(c, p.id)
    assert cell(q, "a10")["flag"] == "" and cell(q, "a10")["text"] == "夜間のトイレで入院した。" and q.flagged == p.flagged - 1
    assert any("これでいい" in h["after_text"] for h in edit_history(c, p.id))


def test_old_drafts_are_looked_at_again_with_the_new_rule_once(env):
    s, c = env
    p = plan_with(s, c, ("a1", "2026-11-02に居宅訪問し、付き添いの話を聞いた。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    row = c.execute("SELECT payload FROM care_plans WHERE id = ?", (p.id,)).fetchone()
    d = json.loads(row["payload"])
    target = d["sections"]["a1"][0]
    target["flag"] = f"{COMPARE}02、11、2026"
    target.pop("words", None)
    d.pop("marks_v", None)
    c.execute("UPDATE care_plans SET payload = ? WHERE id = ?", (json.dumps(d, ensure_ascii=False), p.id))
    c.commit()
    assert get_care_plan(c, p.id).flagged == 1
    page(s, p)
    assert get_care_plan(c, p.id).flagged == 0


def test_blank_cells_stay_plain_and_red(env):
    s, c = env
    p = plan_with(s, c, ("a11", "階段は手すりを使う", "階段は手すりを使う", ASSESS))
    html = page(s, p)
    blank_id = cell(p, "a12")["id"]
    assert cell(p, "a12")["text"] == BLANK
    part = html.split(f'id="cell-{blank_id}"')[0].rsplit("<div", 1)[1]
    assert "blank-cell" in part


# --- 2回目の直し（赤の意味が伝わる形に） ---

META = "障害高齢者の日常生活自立度：B2（前回アセスメントの記載。判定を確認した書類の記載なし）。"


def test_top_line_says_what_red_means_and_what_to_press(env):
    s, c = env
    p = plan_with(s, c, ("a10", "夜間のトイレで入院した。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    html = page(s, p)
    assert "赤い字は、元の書類や記録に同じ言葉が見つからない所です。合っていれば〔これでいい〕、違えば直してください。" in html


def test_this_is_fine_is_shown_without_opening_and_the_reason_is_next_to_it(env):
    s, c = env
    p = plan_with(s, c, ("a10", "夜間のトイレで入院した。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    html = page(s, p)
    box = html.split(f'id="cell-{cell(p, "a10")["id"]}"')[1].split('<div class="cell-print">')[0]
    after = box.split("</details>")[1]
    assert ">これでいい</button>" in after and "「入院」は元の書類に無い言葉" in after
    assert ">これでいい</button>" not in box.split("</details>")[0]


def test_pressing_this_is_fine_lowers_the_count_on_the_page(env):
    s, c = env
    p = plan_with(s, c, ("a10", "夜間のトイレで入院した。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    client = TestClient(create_app(s))
    before = int(re.search(r"あと (\d+) 件", client.get(f"/careplans/{p.id}").text)[1])
    client.post(f"/careplans/{p.id}/ok", data={"item": cell(p, "a10")["id"]})
    html = client.get(f"/careplans/{p.id}").text
    assert (int(re.search(r"あと (\d+) 件", html)[1]) if "あと " in html else 0) == before - 1


def test_scan_only_cells_get_a_pale_note_not_red(env):
    s, c = env
    p = plan_with(s, c, ("a11", "階段は手すりを使う", "階段は手すりを使う", ASSESS))
    d = json.loads(c.execute("SELECT payload FROM care_plans WHERE id = ?", (p.id,)).fetchone()["payload"])
    d["sections"]["a11"][0]["flag"] = "スキャンのため現物で確かめてください"
    c.execute("UPDATE care_plans SET payload = ? WHERE id = ?", (json.dumps(d, ensure_ascii=False), p.id))
    c.commit()
    q = get_care_plan(c, p.id)
    assert q.flagged == 0
    html = page(s, q)
    part = html.split(f'id="cell-{cell(q, "a11")["id"]}"')[0].rsplit("<div", 1)[1]
    assert "scan-cell" in part and "flagged" not in part
    assert "スキャンのため確かめられません" in html.split(f'id="cell-{cell(q, "a11")["id"]}"')[1].split('<div class="cell-print">')[0]


def test_ai_notes_about_its_own_work_are_taken_out(env):
    s, c = env
    from keika.care_plan import ASSESS_SYSTEM

    assert "作業の説明" in ASSESS_SYSTEM
    p = plan_with(s, c, ("a5", META, "膝が痛く、長く歩くと休む", ASSESS))
    assert cell(p, "a5")["text"] == "障害高齢者の日常生活自立度：B2。"


def test_old_drafts_lose_the_ai_notes_once_and_the_history_says_so(env):
    s, c = env
    p = plan_with(s, c, ("a11", "階段は手すりを使う", "階段は手すりを使う", ASSESS))
    d = json.loads(c.execute("SELECT payload FROM care_plans WHERE id = ?", (p.id,)).fetchone()["payload"])
    d["sections"]["a5"][0].update(text=META, flag=f"{COMPARE}前回、記載、判定、書類", words=["前回", "記載", "判定", "書類"])
    d["marks_v"] = 2
    c.execute("UPDATE care_plans SET payload = ? WHERE id = ?", (json.dumps(d, ensure_ascii=False), p.id))
    c.commit()
    page(s, p)
    q = get_care_plan(c, p.id)
    assert cell(q, "a5")["text"] == "障害高齢者の日常生活自立度：B2。" and cell(q, "a5")["flag"] == ""
    assert any("作業の説明" in h["after_text"] for h in edit_history(c, p.id))


# --- 数字は書き方をそろえて値で比べる（10/30・10月30日・2026-10-30、全角、漢数字） ---


def plan_with_fall(s, c, text):
    """日付は別の記録に漢数字で書いてあるだけ。文の元（アセスメント）には日付が無い。"""
    add_record(c, RecordIn(user_id="U01", event_date="2026-11-03", kind_read="電話", body="十月三十日の夜に転倒したと長女から電話があった。"))
    return plan_with(s, c, ("a2", text, "膝が痛く、長く歩くと休む", ASSESS))


def test_slash_date_is_the_same_value(env):
    s, c = env
    assert cell(plan_with_fall(s, c, "10/30の夜に転倒した。"), "a2")["words"] == []


def test_full_width_date_is_the_same_value(env):
    s, c = env
    assert cell(plan_with_fall(s, c, "１０月３０日の夜に転倒した。"), "a2")["words"] == []


def test_year_month_day_is_the_same_value(env):
    s, c = env
    assert cell(plan_with_fall(s, c, "2026年10月30日の夜に転倒した。"), "a2")["words"] == []


def test_a_number_with_a_different_value_is_still_red(env):
    s, c = env
    assert "29" in cell(plan_with_fall(s, c, "10月29日の夜に転倒した。"), "a2")["words"]


def test_month_compared_with_another_month_in_the_quote_is_not_red_when_the_date_exists(env):
    s, c = env
    add_record(c, RecordIn(user_id="U01", event_date="2026-11-03", kind_read="電話", body="十月三十日の夜に転倒した。9月にも同じ時間帯に転倒している。"))
    p = plan_with(s, c, ("a2", "9月と10月30日に転倒した。", "9月にも同じ時間帯に転倒している", APP_RECORDS))
    assert cell(p, "a2")["words"] == []


def test_count_changed_from_the_quote_is_still_red(env):
    s, c = env
    p = plan_with(s, c, ("a15", "デイは週3回。", "週2回", "利用者/U01_試験太郎/居宅サービス計画書_2026-04-01.txt"))
    assert "3" in cell(p, "a15")["words"]
