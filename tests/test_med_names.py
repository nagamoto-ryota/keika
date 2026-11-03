"""薬名の確認: 話のとおり保存し、書類か確定済みの記録にある名前だけを並べ、押した時だけ直す。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.ledger import seed_from_json
from keika.med_names import candidates, decide_fix, decide_keep, decisions, message, open_checks, register_checks
from keika.records import RecordIn, add_record, get_record, history, list_records, mark_checked
from keika.settings import Settings
from keika.web.app import create_app
from test_intake import entry, run

F = "利用者/U04_柚木茂"
HEARD = "アムロジンピン"
GUESSING = ("のことですね", "おそらく", "と思われ", "でしょうか", "かもしれません", "正しくは", "の間違い")


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / F).mkdir(parents=True)
    (root / "利用者" / "U01_杉森ハル").mkdir()
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def doc(s, name="お薬情報_2026-10-01.txt", text="朝食後 アムロジピン 5mg 1錠", folder=F):
    (s.drive_root / folder / name).write_text(text, encoding="utf-8")


def record(c, body=f"朝は{HEARD}を1錠内服しているとのこと。", user="U04"):
    return add_record(c, RecordIn(user_id=user, event_date="2026-11-02", kind_written="訪問", kind_read="居宅訪問", body=body))


def body_of(c, record_id):
    return get_record(c, record_id).body


def test_saved_as_spoken_without_stopping(env):
    s, c = env
    doc(s)
    body = f"朝は{HEARD}を1錠内服しているとのこと。"
    results, _ = run(s, c, f"柚木さん宅を訪問、朝の薬は{HEARD}を1錠", entry(body=body, unsure_words=[HEARD]))
    assert results[0].record is not None
    assert list_records(c, "U04")[0].body == body  # 自動で直っていない
    assert [m.heard for m in open_checks(c, s)] == [HEARD]


def test_candidate_comes_from_this_users_document_with_its_place(env):
    s, c = env
    doc(s)
    found = candidates(c, s, "U04", HEARD)
    assert [(x.name, x.where) for x in found] == [("アムロジピン", "お薬情報")]
    assert found[0].doc_path == f"{F}/お薬情報_2026-10-01.txt"


def test_other_users_documents_are_not_used(env):
    s, c = env
    doc(s, folder="利用者/U01_杉森ハル")
    assert candidates(c, s, "U04", HEARD) == []


def test_no_candidate_outside_the_documents(env):
    """一般知識なら アムロジピン と分かる語でも、書類に無ければ出さない。"""
    s, c = env
    doc(s, text="朝食後 メトホルミン 1錠")
    assert candidates(c, s, "U04", HEARD) == []


def test_without_candidate_only_asks_to_check(env):
    s, c = env
    rid = record(c).id
    register_checks(c, s, get_record(c, rid), [HEARD])
    assert message(open_checks(c, s)[0]) == [f"「{HEARD}」と聞こえました。", "薬の名前を確かめてください。"]


def test_message_lines_up_heard_and_written_without_guessing(env):
    s, c = env
    doc(s)
    register_checks(c, s, record(c), [HEARD])
    text = "".join(message(open_checks(c, s)[0]))
    assert text == f"「{HEARD}」と聞こえました。お薬情報には「アムロジピン」とあります。"
    assert not any(g in text for g in GUESSING)


def test_confirmed_record_can_be_the_basis(env):
    s, c = env
    old = record(c, "アムロジピンを内服中。")
    mark_checked(c, old.id, editor="真柴")
    found = candidates(c, s, "U04", HEARD)
    assert [(x.name, x.where) for x in found] == [("アムロジピン", "11/2の確定済みの記録")]


def test_unconfirmed_record_is_not_the_basis(env):
    s, c = env
    record(c, "アムロジピンを内服中。")
    assert candidates(c, s, "U04", HEARD) == []


def test_fix_only_when_pressed_and_kept_in_history(env):
    s, c = env
    doc(s)
    r = record(c)
    register_checks(c, s, r, [HEARD])
    assert HEARD in body_of(c, r.id)
    check = open_checks(c, s)[0]
    decide_fix(c, s, check.id, "アムロジピン", editor="真柴")
    assert body_of(c, r.id) == "朝はアムロジピンを1錠内服しているとのこと。"
    assert [HEARD in v.body for v in history(c, r.id)] == [True, False]
    assert open_checks(c, s) == []
    d = decisions(c, r.id)[0]
    assert (d.status, d.heard, d.chosen, d.editor) == ("fixed", HEARD, "アムロジピン", "真柴") and d.decided_at


def test_keep_changes_nothing_and_is_recorded(env):
    s, c = env
    doc(s)
    r = record(c)
    register_checks(c, s, r, [HEARD])
    decide_keep(c, open_checks(c, s)[0].id, editor="真柴")
    assert body_of(c, r.id) == r.body and open_checks(c, s) == []
    assert decisions(c, r.id)[0].status == "kept"


def test_fix_refuses_a_name_that_is_not_a_candidate(env):
    s, c = env
    doc(s)
    r = record(c)
    register_checks(c, s, r, [HEARD])
    with pytest.raises(ValueError):
        decide_fix(c, s, open_checks(c, s)[0].id, "ノルバスク", editor="真柴")
    assert body_of(c, r.id) == r.body


def test_name_matching_the_document_exactly_needs_no_check(env):
    s, c = env
    doc(s)
    register_checks(c, s, record(c, "朝はアムロジピンを1錠内服。"), ["アムロジピン"])
    assert open_checks(c, s) == []


def test_near_name_in_medicine_sentence_is_found_even_if_claude_did_not_flag(env):
    s, c = env
    doc(s)
    register_checks(c, s, record(c), [])
    assert [m.heard for m in open_checks(c, s)] == [HEARD]


def test_katakana_outside_medicine_sentence_is_ignored(env):
    s, c = env
    doc(s, text="ヘルパーステーション")
    register_checks(c, s, record(c, "ヘルパーステーショソへ連絡した。"), [])
    assert open_checks(c, s) == []


def test_edit_that_removes_the_word_closes_the_check(env):
    from keika.records import edit_record
    s, c = env
    r = record(c)
    register_checks(c, s, r, [HEARD])
    edit_record(c, r.id, {"body": "朝の薬を1錠内服。"}, editor="真柴")
    assert open_checks(c, s) == []


def test_screen_shows_the_check_on_todo_list_user_page_and_board(env):
    s, c = env
    doc(s)
    r = record(c)
    register_checks(c, s, r, [HEARD])
    client = TestClient(create_app(s))
    page = client.get("/").text
    assert "薬の名前の確認" in page and f"「{HEARD}」と聞こえました。" in page and "「アムロジピン」に直す" in page
    assert not any(g in page for g in ("のことですね", "おそらく", "正しくは"))
    assert "薬の名前が未確認" in client.get("/board").text and "薬の名前が未確認" in client.get("/users/U04").text
    check = open_checks(c, s)[0]
    assert client.post(f"/med/{check.id}/fix", data={"chosen": "ノルバスク"}, follow_redirects=False).status_code == 400
    assert body_of(c, r.id) == r.body
    assert client.post(f"/med/{check.id}/fix", data={"chosen": "アムロジピン", "editor": "真柴"}, follow_redirects=False).status_code == 303
    assert "アムロジピン" in body_of(c, r.id)
    hist = client.get(f"/records/{r.id}/history").text
    assert "直しました" in hist and HEARD in hist


def test_screen_keep_button(env):
    s, c = env
    r = record(c)
    register_checks(c, s, r, [HEARD])
    client = TestClient(create_app(s))
    check = open_checks(c, s)[0]
    assert client.post(f"/med/{check.id}/keep", data={}, follow_redirects=False).status_code == 303
    assert "そのままにしました" in client.get(f"/records/{r.id}/history").text


def test_decisions_cannot_be_deleted(env):
    s, c = env
    r = record(c)
    register_checks(c, s, r, [HEARD])
    decide_keep(c, open_checks(c, s)[0].id, editor="")
    with pytest.raises(Exception):
        c.execute("DELETE FROM med_checks")


def test_process_text_runs_without_a_document(env):
    s, c = env
    results, _ = run(s, c, f"柚木さん宅を訪問、薬は{HEARD}", entry(body=f"{HEARD}を内服。", unsure_words=[HEARD]))
    assert results[0].record and message(open_checks(c, s)[0])[1] == "薬の名前を確かめてください。"


def test_a_person_called_unsure_is_not_a_medicine_check(env):
    s, c = env
    run(s, c, "柚木さん宅を訪問。能登さんが測ってくれた。", entry(body="能登さんが測っていた。", unsure_words=["能登さん"]))
    assert c.execute("SELECT COUNT(*) FROM med_checks").fetchone()[0] == 0
