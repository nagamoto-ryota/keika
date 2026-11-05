"""下書きの確認カードと書類の見た目、やることの「細かい項目」の畳み方。判定と項目の数は変えない。"""
from datetime import date
from html.parser import HTMLParser
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.claude_checks import CheckRun, save_run
from keika.draft_card import WireSentence, add_item, draft_view, edit_item, fill_blank, keep_blank, next_draft, ok_item, revise_item
from keika.drafts import WireAttendee, WireDraft, WireItem, WireSection, get_draft, make_draft, mark_checked
from keika.findings import Evidence, Finding
from keika.ledger import get_user, seed_from_json
from keika.office import office_rows
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.tasks import Task, build_board, person_line
from keika.web.app import create_app
from test_reading import FakeClient, answer

TALK = """2026年11月5日、柚木様のお宅で14時から訪問しました。薬の袋を見たら飲み忘れなしでした。
本人も昼のがないから楽だって言ってました。来る島先生にも伝えます。デイは週2回のまま続けます。"""
THREE = WireItem(text="デイは週3回のまま続ける。", quote="デイは週2回のまま続けます")


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 5))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def draft(s, c, kind="monitoring", items=None, **kw):
    r = add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="会議" if kind == "meeting" else "居宅訪問", monitoring=kind == "monitoring", body="話した。"))
    key = "conclusion" if kind == "meeting" else "services"
    wire = dict(place="", start_time="14:00", end_time="", attendees=[], sections=[WireSection(key=key, items=items or [WireItem(text="本人は昼の薬がなくなり楽だと話した。", quote="本人も昼のがないから楽だって言ってました")])])
    return make_draft(c, FakeClient(answer(WireDraft(**(wire | kw)))), s, r, TALK, "話.txt")


def first(c, i, key="services"):
    return get_draft(c, i).sections[key][0]


def page(s, i):
    return TestClient(create_app(s)).get(f"/drafts/{i}").text


def fields(c, i):
    return {b["field"]: b["question"] for b in draft_view(c, get_draft(c, i))["blanks"]}


# --- 確かめること ---


def test_flagged_sentence_is_one_card_with_what_was_said(env):
    s, c = env
    i = draft(s, c)
    check = draft_view(c, get_draft(c, i))["checks"][0]
    assert check["text"] == "本人は昼の薬がなくなり楽だと話した。" and check["quote"] == "本人も昼のがないから楽だって言ってました"
    html = page(s, i)
    assert "元の発言" in html and check["quote"] in html and ">いいよ</button>" in html and "<summary>違う</summary>" in html
    assert html.index("do-line") < html.index("本人・家族の様子")


def test_check_label_is_not_shown_as_a_word_in_the_sentence(env):
    """「飲めている」は語ではなく判定の名前。文に無い語として見せない（判定は今のまま）。"""
    s, c = env
    i = draft(s, c)
    assert "飲めている" in first(c, i)["flag"]  # 判定は変えない
    check = draft_view(c, get_draft(c, i))["checks"][0]
    assert check["words"] == [] and "薬" in check["note"]
    html = page(s, i)
    assert "飲めている" not in html and "文字起こしにありません" not in html and "赤い線" not in html


def test_real_missing_word_is_named_beside_the_quote(env):
    s, c = env
    i = draft(s, c, items=[WireItem(text="久留島医師に伝える。", quote="来る島先生にも伝えます")])
    assert "久留島" in draft_view(c, get_draft(c, i))["checks"][0]["words"]
    assert "「久留島」はこの発言にありません" in page(s, i)


def test_sentence_without_a_quote_says_so(env):
    s, c = env
    i = draft(s, c, items=[WireItem(text="デイは週2回のまま続ける。", quote="どこにも無い発言です")])
    assert draft_view(c, get_draft(c, i))["checks"][0]["quote"] == ""
    assert "見つかりません" in page(s, i)


def test_ok_clears_the_card_without_changing_the_sentence(env):
    s, c = env
    i = draft(s, c)
    before = first(c, i)
    r = TestClient(create_app(s)).post(f"/drafts/{i}/items/{before['id']}/ok", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#kakunin")
    after = first(c, i)
    assert after["text"] == before["text"] and after["flag"] == "" and after["checked_flag"] == before["flag"] and after["quote"] == before["quote"]
    assert draft_view(c, get_draft(c, i))["checks"] == []


def test_ok_unknown_item_is_404(env):
    s, c = env
    i = draft(s, c)
    assert TestClient(create_app(s)).post(f"/drafts/{i}/items/nope/ok").status_code == 404


def test_revise_applies_the_instruction_and_keeps_the_card_open(env):
    s, c = env
    i = draft(s, c, items=[THREE])
    client = FakeClient(answer(WireSentence(text="デイは週2回のまま続ける。")))
    assert revise_item(c, client, s, i, first(c, i)["id"], "週2回です") == ""
    new = first(c, i)
    assert new["text"] == "デイは週2回のまま続ける。" and new["flag"] and new["instructions"] == ["週2回です"]
    assert "「3」を「2」に" in new["changed"]
    assert len(client.calls) == 1 and "週2回です" in str(client.calls[0]) and "薬の袋を見たら" in str(client.calls[0])
    assert draft_view(c, get_draft(c, i))["checks"][0]["changed"] == new["changed"]


def test_revise_refuses_words_not_in_talk_or_instruction(env):
    s, c = env
    i = draft(s, c, items=[THREE])
    item = first(c, i)
    notice = revise_item(c, FakeClient(answer(WireSentence(text="デイは週2回で、99歩歩いた。"))), s, i, item["id"], "週2回です")
    assert "99" in notice and "直しませんでした" in notice
    assert first(c, i)["text"] == item["text"]


def test_revise_accepts_a_word_the_person_wrote(env):
    s, c = env
    i = draft(s, c, items=[THREE])
    assert revise_item(c, FakeClient(answer(WireSentence(text="デイは週4回に増やす。"))), s, i, first(c, i)["id"], "週4回に増やすと決めました") == ""
    assert first(c, i)["text"] == "デイは週4回に増やす。"


def test_revise_refuses_a_medical_judgment(env):
    s, c = env
    i = draft(s, c, items=[THREE])
    item = first(c, i)
    notice = revise_item(c, FakeClient(answer(WireSentence(text="薬を中止すべきである。"))), s, i, item["id"], "直して")
    assert "直しませんでした" in notice and first(c, i)["text"] == item["text"]


def test_revise_route_shows_the_notice_on_the_page(env):
    s, c = env
    i = draft(s, c, items=[THREE])
    client = TestClient(create_app(s, client_factory=lambda: FakeClient(answer(WireSentence(text="デイは週2回で、99歩歩いた。")))))
    r = client.post(f"/drafts/{i}/items/{first(c, i)['id']}/revise", data={"instruction": "週2回です"}, follow_redirects=False)
    assert r.status_code == 303
    assert "直しませんでした" in client.get(f"/drafts/{i}").text


# --- これ足して ---


def test_blank_fields_become_questions(env):
    s, c = env
    i = draft(s, c)
    asked = fields(c, i)
    assert "訪問先" in asked["place"] and "帰りの時間" in asked["end_time"] and "本人・家族の様子" in asked["s:condition"]
    assert "start_time" not in asked  # 話にあった欄は聞かない
    assert 'name="field" value="end_time"' in page(s, i)  # 本文の中でその場で埋める


def test_meeting_asks_in_its_own_words(env):
    s, c = env
    i = draft(s, c, kind="meeting", items=[WireItem(text="デイは週2回のまま続ける。", quote="デイは週2回のまま続けます")])
    asked = fields(c, i)
    assert "開催場所" in asked["place"] and "終わった" in asked["end_time"] and "残された課題" in asked["s:remaining"]


@pytest.mark.parametrize("said,saved", [("15時", "15:00"), ("15時半", "15:30"), ("１５：３０", "15:30"), ("午後3時10分", "15:10"), ("夕方ごろ", "夕方ごろ")])
def test_answer_goes_into_the_time_field(env, said, saved):
    s, c = env
    i = draft(s, c)
    fill_blank(c, i, "end_time", said)
    assert get_draft(c, i).end_time == saved


def test_answer_goes_into_place_and_section_as_written(env):
    s, c = env
    i = draft(s, c)
    fill_blank(c, i, "place", "ご自宅")
    fill_blank(c, i, "s:condition", "変わりなしとのこと。")
    d = get_draft(c, i)
    assert d.place == "ご自宅" and d.sections["condition"][0]["text"] == "変わりなしとのこと。"
    assert not {"place", "s:condition"} & set(fields(c, i))


def test_answer_fills_the_unknown_attendee_only(env):
    s, c = env
    i = draft(s, c, attendees=[WireAttendee(org="", role="", name="誰か不明", quote="いません"), WireAttendee(org="", role="本人", name="柚木 茂", quote="柚木様のお宅")])
    assert "a:0" in fields(c, i)
    fill_blank(c, i, "a:0", "能登さん")
    assert [a["name"] for a in get_draft(c, i).attendees] == ["能登さん", "柚木 茂"]


def test_empty_answer_changes_nothing(env):
    s, c = env
    i = draft(s, c)
    fill_blank(c, i, "place", "  ")
    assert get_draft(c, i).place == "〇〇"


def test_keep_blank_removes_the_question_and_leaves_the_mark(env):
    s, c = env
    i = draft(s, c)
    keep_blank(c, i, "end_time")
    assert get_draft(c, i).end_time == "〇〇" and "end_time" not in fields(c, i)


def test_blank_route_needs_no_ai(env):
    s, c = env
    i = draft(s, c)

    def no_ai():
        raise AssertionError("AI を呼ばない")

    client = TestClient(create_app(s, client_factory=no_ai))
    assert client.post(f"/drafts/{i}/blank", data={"field": "end_time", "answer": "15時"}, follow_redirects=False).status_code == 303
    assert client.post(f"/drafts/{i}/blank", data={"field": "place", "action": "keep"}, follow_redirects=False).status_code == 303
    assert get_draft(c, i).end_time == "15:00" and get_draft(c, i).place == "〇〇" and "place" not in fields(c, i)


# --- 本文 ---


def test_body_reads_as_a_document_and_each_sentence_opens_to_edit(env):
    s, c = env
    i = draft(s, c)
    item = first(c, i)
    html = page(s, i)
    assert f'id="item-{item["id"]}"' in html and 'class="sentence' in html
    assert 'name="s_services"' not in html  # 文ごとの入力欄を並べない
    assert f'action="/drafts/{i}/items/{item["id"]}/text"' in html and "元の発言" in html


def test_sentence_can_be_reached_by_its_own_id(env):
    s, c = env
    i = draft(s, c)
    assert f'id="item-{first(c, i)["id"]}"' in page(s, i)


def test_editing_a_sentence_clears_its_card(env):
    s, c = env
    i = draft(s, c)
    item = first(c, i)
    r = TestClient(create_app(s)).post(f"/drafts/{i}/items/{item['id']}/text", data={"text": "本人は楽だと話した。"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith(f"#item-{item['id']}")
    new = first(c, i)
    assert new["text"] == "本人は楽だと話した。" and new["flag"] == "" and new["quote"] == item["quote"]


def test_emptying_the_only_sentence_leaves_a_blank(env):
    s, c = env
    i = draft(s, c)
    edit_item(c, i, first(c, i)["id"], "")
    assert first(c, i)["text"] == "〇〇"


def test_add_replaces_the_blank_and_appends_otherwise(env):
    s, c = env
    i = draft(s, c)
    add_item(c, i, "condition", "変わりなし。")
    add_item(c, i, "condition", "元気そう。")
    assert [x["text"] for x in get_draft(c, i).sections["condition"]] == ["変わりなし。", "元気そう。"]


def test_head_form_still_saves_place_without_touching_sentences(env):
    s, c = env
    i = draft(s, c)
    before = first(c, i)
    form = {"place": "自宅", "start_time": "14:00", "end_time": "15:00", "attendee_org": "", "attendee_role": "", "attendee_name": ""}
    TestClient(create_app(s)).post(f"/drafts/{i}", data=form, follow_redirects=False)
    d = get_draft(c, i)
    assert d.place == "自宅" and d.sections["services"][0] == before


# --- 次の一手 ---


def finish(c, i):
    for b in draft_view(c, get_draft(c, i))["blanks"]:
        keep_blank(c, i, b["field"])
    for ch in draft_view(c, get_draft(c, i))["checks"]:
        ok_item(c, i, ch["item_id"])


def test_next_step_appears_only_when_nothing_is_left(env):
    s, c = env
    i = draft(s, c)
    assert "Word で保存してください" not in page(s, i)
    finish(c, i)
    html = page(s, i)
    assert "Word で保存してください" in html and f"/drafts/{i}/docx" in html
    assert 'id="kakunin"' in html  # #kakunin で来ても迷わない
    assert ">いいよ</button>" not in html
    assert get_draft(c, i).flagged == 0


def test_sample_mode_hides_the_buttons_but_shows_the_card(env):
    s, c = env
    i = draft(s, c)
    html = TestClient(create_app(s, sample=True)).get(f"/drafts/{i}").text
    assert "元の発言" in html and ">いいよ</button>" not in html and "文を直す" not in html


# --- やること: 細かい項目 ---


def dash(s):
    return TestClient(create_app(s)).get("/todo").text


def test_other_items_have_no_outer_fold(env):
    s, _ = env
    assert 'class="minor"' not in dash(s)


def test_each_person_is_one_row_that_opens(env):
    s, c = env
    draft(s, c, sections=[WireSection(key="next", items=[WireItem(text="主治医に伝える。", quote="来る島先生にも伝えます")])])
    html = dash(s)
    assert 'id="p-U04"' in html and 'class="row person-row"' in html


def test_script_runs_after_the_rows_so_a_link_can_open_them(env):
    s, _ = env
    html = dash(s)
    assert html.index("ほかの項目") < html.index("review_card.js")


def homework_draft(s, c):
    return draft(s, c, sections=[WireSection(key="next", items=[WireItem(text="主治医に伝える。", quote="来る島先生にも伝えます")])])


def test_rows_keep_their_buttons(env):
    s, c = env
    homework_draft(s, c)
    assert "済んだ" in dash(s)


def test_sample_mode_hides_the_buttons_inside_the_rows(env):
    s, c = env
    homework_draft(s, c)
    html = TestClient(create_app(s, sample=True)).get("/todo").text
    assert "主治医に伝える" in html and "済んだ" not in html


def test_person_line_leads_with_nearest_deadline():
    line = person_line([Task("後の用事"), Task("見直し", deadline="2026-11-30"), Task("もっと前", deadline="2026-11-20")])
    assert line["count"] == 3 and line["lead"] == "もっと前" and line["deadline"] == "2026-11-20"


def test_person_line_leads_with_first_task_when_no_deadline():
    line = person_line([Task("最初"), Task("次")])
    assert line["lead"] == "最初" and line["deadline"] is None


def rule_task(title, rule, deadline=None, suggestion=""):
    return Task(title, NS(finding=NS(rule_id=rule, suggestion=suggestion)), deadline)


def test_overlapping_update_tasks_group_without_changing_the_count():
    tasks = [rule_task("計画の見直しの準備", "R45", "2026-11-30"), rule_task("認定の更新申請の援助", "R44", "2026-11-30"), rule_task("更新申請の状況を確かめる", "R11"), rule_task("デイの変更を反映", "R11"), rule_task("同意を取る", "R21")]
    line = person_line(tasks)
    assert line["count"] == 5
    group = line["rows"][0]
    assert group["group"] == "更新の準備" and [x.title for x in group["tasks"]] == ["計画の見直しの準備", "認定の更新申請の援助", "更新申請の状況を確かめる"]
    assert group["deadline"] == "2026-11-30"
    assert [r["task"].title for r in line["rows"][1:]] == ["デイの変更を反映", "同意を取る"]


def test_single_update_task_is_not_grouped():
    assert person_line([rule_task("見直し", "R45", "2026-11-30")])["rows"][0]["task"].title == "見直し"


def test_every_task_says_what_opening_it_does():
    assert Task("宿題", homework=[object()]).opens == "宿題の一覧"
    assert rule_task("案あり", "R01", suggestion="案").opens == "根拠と書き足す文の案"
    assert rule_task("案なし", "R01").opens == "根拠と次の手順"
    assert Task("進捗だけ").opens == ""  # 行き先は利用者のページへの直リンク


def test_task_without_details_is_a_direct_link(env, monkeypatch):
    s, c = env
    html = dash(s)
    board = build_board(c, s)
    for person in board.people:
        for t in person.tasks:
            if not t.opens:
                assert f'href="/users/{person.user.id}"' in html


def test_draft_row_links_to_what_is_left(env):
    s, c = env
    i = draft(s, c)
    html = dash(s)
    assert f"/drafts/{i}#kakunin" in html and "確かめること" in html


def test_every_task_of_the_board_is_on_the_page(env):
    """畳むだけで、項目は減らさない。"""
    s, c = env
    draft(s, c)
    html = dash(s)
    board = build_board(c, s)
    assert board.total > 0
    for person in board.people:
        for t in person.tasks:
            assert t.title in html


def test_done_homework_returns_to_that_person(env):
    s, c = env
    i = draft(s, c, items=[WireItem(text="デイは週2回のまま続ける。", quote="デイは週2回のまま続けます")], sections=[WireSection(key="next", items=[WireItem(text="主治医に伝える。", quote="来る島先生にも伝えます")])])
    item = first(c, i, "next")
    r = TestClient(create_app(s)).post(f"/drafts/{i}/items/{item['id']}/done", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/todo#p-U04"


# --- 押す回数 ---


class _Depth(HTMLParser):
    """語や属性が、いくつの畳み（details）の中にあるか。畳みを1つ開くのが1回押す事にあたる。"""

    def __init__(self, needle):
        super().__init__()
        self.needle, self.depth, self.found = needle, 0, []

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.depth += 1
        if any(self.needle in (v or "") for _, v in attrs):
            self.found.append(self.depth)

    def handle_endtag(self, tag):
        if tag == "details":
            self.depth -= 1

    def handle_data(self, data):
        if self.needle in data:
            self.found.append(self.depth)


def folds_around(html, needle):
    parser = _Depth(needle)
    parser.feed(html)
    assert parser.found, needle
    return max(parser.found)


def finding(rule, todo, message):
    return Finding(rule_id=rule, user_id="U04", kind="finding", period="2026-10", source="claude", message=message, evidence=[Evidence(file="x.csv", quote="計画")], todo=todo)


def test_a_draft_answer_is_one_press_from_the_todo_page(env):
    """1通1行のリンクが畳みの外にあるので、押せばすぐ答える画面に着く。"""
    s, c = env
    i = draft(s, c)
    html = dash(s)
    assert folds_around(html, f"/drafts/{i}#kakunin") == 0
    assert 'id="kakunin"' in page(s, i) and ">いいよ</button>" in page(s, i)


def test_draft_rows_come_right_after_the_consultation(env):
    s, c = env
    draft(s, c)
    html = dash(s)
    assert html.index("ケイカからの相談") < html.index("書類の下書きの確認") < html.index("ほかの項目")


def test_person_rows_are_listed_from_the_start(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding("R07", "主治医への交付を確かめる", "交付の記録が見当たりません。")]))
    parser = _Depth("p-U04")
    parser.feed(dash(s))
    assert min(parser.found) == 1  # 人の行そのもの。外側の畳みが無い


def test_finding_and_next_step_are_within_two_presses(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding("R07", "主治医への交付を確かめる", "交付の記録が見当たりません。")]))
    html = dash(s)
    assert folds_around(html, "交付の記録が見当たりません") == 2 and folds_around(html, "次の手順と下書きを作る") == 2


def test_grouped_update_tasks_do_not_add_a_press(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[finding("R09", "計画更新のアセスメントを確かめる", "更新のアセスメントが見当たりません。"), finding("R11", "更新申請の状況を確かめる", "更新申請の状況が見当たりません。")]))
    html = dash(s)
    assert "更新の準備" in html and folds_around(html, "更新のアセスメントが見当たりません") == 2 and folds_around(html, "更新申請の状況が見当たりません") == 2


def test_homework_is_within_two_presses(env):
    s, c = env
    homework_draft(s, c)
    assert folds_around(dash(s), "主治医に伝える") == 2


def test_visit_row_sits_with_the_people(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[Finding(rule_id="R06", user_id="U04", kind="nudge", period="2026-11", source="claude", message="今月の訪問がまだです。", evidence=[], todo="今月の訪問")]))
    html = dash(s)
    assert html.index("ほかの項目") < html.index("今月の訪問がまだの方")
    assert folds_around(html, "今月の訪問がまだの方") == 1


# --- 見出し ---


def test_heading_counts_what_needs_deciding_and_the_rest_stay_counted(env):
    s, c = env
    i = draft(s, c)
    j = homework_draft(s, c)
    keep = [b["field"] for b in draft_view(c, get_draft(c, j))["blanks"]]
    for field in keep:
        keep_blank(c, j, field)
    for ch in draft_view(c, get_draft(c, j))["checks"]:
        ok_item(c, j, ch["item_id"])
    board = build_board(c, s)
    html = dash(s)
    assert '今決めること <span class="count">1</span>' in html  # 確かめることが残る下書きだけ。j は済んだ
    assert f"ほかに {board.total + 1 + len(office_todo(c, s))}件" in html
    assert i != j


def office_todo(c, s):
    return [r for r in office_rows(c, s) if r.status in ("todo", "unknown")]


# --- 次の下書きへ ---


def done(c, i):
    finish(c, i)


def test_next_draft_button_goes_to_the_one_still_open(env):
    s, c = env
    a, b = draft(s, c), draft(s, c)
    done(c, a)
    html = page(s, a)
    assert "次の下書きへ" in html and f'href="/drafts/{b}#kakunin"' in html and "やることへ戻る" not in html


def test_last_draft_offers_the_way_back(env):
    s, c = env
    a = draft(s, c)
    done(c, a)
    html = page(s, a)
    assert "次の下書きへ" not in html and "やることへ戻る" in html and "Word で保存する" in html


def test_next_draft_skips_finished_and_set_aside_ones(env):
    s, c = env
    a, b, d = draft(s, c), draft(s, c), draft(s, c)
    done(c, a)
    done(c, b)
    mark_checked(c, d)
    assert next_draft(c, get_draft(c, a)) is None


def test_next_draft_goes_forward_then_wraps(env):
    s, c = env
    a, b, d = draft(s, c), draft(s, c), draft(s, c)
    done(c, b)
    assert next_draft(c, get_draft(c, b)) == d
    done(c, d)
    assert next_draft(c, get_draft(c, d)) == a


def test_next_draft_prefers_the_same_care_manager(env):
    s, c = env
    a = draft(s, c)
    r = add_record(c, RecordIn(user_id="U05", event_date="2026-11-05", kind_read="居宅訪問", monitoring=True, body="話した。"))
    other = make_draft(c, FakeClient(answer(WireDraft(place="", start_time="", end_time="", attendees=[], sections=[]))), s, r, TALK, "話.txt")
    b = draft(s, c)
    done(c, a)
    same = get_user(c, "U04").cm == get_user(c, "U05").cm
    assert next_draft(c, get_draft(c, a)) == (other if same else b)


def test_no_all_at_once_button(env):
    s, c = env
    assert "全部いいよ" not in page(s, draft(s, c))
