from datetime import date

import docx
import pytest
from fastapi.testclient import TestClient

from keika.db import connect
from keika.files import scan
from keika.ledger import upsert_person
from keika.office import declare, office_findings, office_rows
from keika.reading import ActivityFact, DocFacts, Source, _save
from keika.settings import Settings
from keika.web.app import create_app


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "事業所").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    c = connect(s)
    upsert_person(c, "CM01", "staff", None, "真柴 洋子", "", {"hired": "2019-04-01"})
    upsert_person(c, "CM02", "staff", None, "井波 拓真", "", {"hired": "2026-07-01"})
    yield s, c
    c.close()


def put(s, c, name, **kw):
    path = s.drive_root / "事業所" / name
    if not path.exists():
        path.write_text(kw.pop("text", name), encoding="utf-8")
    ref = next(r for r in scan(s) if r.rel_path == f"事業所/{name}")
    _save(c, ref, DocFacts(**({"body_status": "本文あり"} | kw)))
    return ref.rel_path


def row(s, c, rule):
    return next(r for r in office_rows(c, s) if r.rule.id == rule)


def activity(kind, title, day, participants=None):
    return ActivityFact(kind=kind, title=title, date=day, participants=participants)


def test_unread_folder_is_unknown_not_todo(env):
    s, c = env
    assert row(s, c, "R23").status == "unknown"


def test_draft_with_only_toc_is_not_done(env):
    s, c = env
    put(s, c, "BCP_感染症編.txt", doc_type="業務継続計画", summary="感染症編")
    put(s, c, "BCP_自然災害編_下書き.txt", doc_type="業務継続計画", body_status="表紙や目次だけ", summary="自然災害編の下書き")
    r = row(s, c, "R23")
    assert r.status == "todo" and "災害" in r.next_step


def test_bcp_done_with_both_and_training(env):
    s, c = env
    a = put(s, c, "BCP_感染症編.txt", doc_type="業務継続計画", summary="感染症編")
    put(s, c, "BCP_災害編.txt", doc_type="業務継続計画", summary="自然災害編")
    put(s, c, "記録.txt", doc_type="研修の記録", activities=[activity("研修", "業務継続計画の研修", "2026-05-01"), activity("訓練", "BCP訓練", "2026-06-01")])
    r = row(s, c, "R23")
    assert r.status == "done" and r.evidence_rel_path == a


def test_bcp_without_training_is_todo(env):
    s, c = env
    put(s, c, "BCP_感染症編.txt", doc_type="業務継続計画", summary="感染症編")
    put(s, c, "BCP_災害編.txt", doc_type="業務継続計画", summary="自然災害編")
    r = row(s, c, "R23")
    assert r.status == "todo" and "研修" in r.next_step


def test_done_row_links_evidence_file(env):
    s, c = env
    rel = put(s, c, "ハラスメント防止の方針と相談窓口.txt", doc_type="指針・マニュアル", summary="ハラスメント防止の方針")
    r = row(s, c, "R28")
    assert r.status == "done" and r.evidence_rel_path == rel
    body = TestClient(create_app(s)).get("/office").text
    assert "/doc?path=" in body and "開く" in body


def test_evidence_outside_root_is_rejected(env):
    s, c = env
    assert TestClient(create_app(s)).get("/doc", params={"path": "../x.db"}).status_code == 403


def test_web_posting_without_evidence_is_unknown(env):
    s, c = env
    put(s, c, "重要事項の掲示.txt", doc_type="掲示", summary="重要事項の掲示")
    r = row(s, c, "R27")
    assert r.status == "unknown" and "ウェブ" in r.next_step


def test_declare_records_who_when_and_basis(env):
    s, c = env
    put(s, c, "重要事項の掲示.txt", doc_type="掲示", summary="重要事項の掲示")
    declare(c, "R27", who="真柴", basis="法人のホームページで確認した")
    r = row(s, c, "R27")
    assert r.status == "declared" and "真柴" in r.note and "ホームページ" in r.note and r.declared_at


def test_declare_needs_who_and_basis(env):
    s, c = env
    with pytest.raises(ValueError):
        declare(c, "R27", who="", basis="")


def test_declared_is_shown_differently_from_done(env):
    s, c = env
    put(s, c, "重要事項の掲示.txt", doc_type="掲示", summary="重要事項の掲示")
    declare(c, "R27", who="真柴", basis="確認した")
    body = TestClient(create_app(s)).get("/office").text
    assert "申告" in body


def _abuse_docs(s, c, trainings):
    put(s, c, "虐待の防止のための指針.txt", doc_type="指針・マニュアル", summary="虐待の防止のための指針")
    put(s, c, "運営規程.txt", doc_type="規程", summary="運営規程", text="第十条 虐待の防止のための措置を講じる。")
    put(s, c, "虐待防止委員会.txt", doc_type="委員会の記録", activities=[activity("委員会", "虐待防止委員会", "2026-06-15")])
    put(s, c, "記録.txt", doc_type="研修の記録", activities=trainings)


def test_new_hire_training_missing_is_flagged(env):
    s, c = env
    _abuse_docs(s, c, [activity("研修", "虐待防止研修", "2026-02-01", "真柴 洋子")])
    r = row(s, c, "R25")
    assert r.status == "todo" and "井波" in r.next_step and "新規採用" in r.next_step


def test_new_hire_training_done_when_named(env):
    s, c = env
    _abuse_docs(s, c, [activity("研修", "虐待防止研修", "2026-07-10", "真柴 洋子、井波 拓真")])
    assert row(s, c, "R25").status == "done"


def test_rules_without_abuse_clause_in_regulation_is_todo(env):
    s, c = env
    _abuse_docs(s, c, [activity("研修", "虐待防止研修", "2026-07-10", "真柴 洋子、井波 拓真")])
    (s.drive_root / "事業所" / "運営規程.txt").write_text("第十条 その他", encoding="utf-8")
    put(s, c, "運営規程.txt", doc_type="規程", summary="運営規程")
    r = row(s, c, "R25")
    assert r.status == "todo" and "運営規程" in r.next_step


def test_regulation_text_is_read_from_docx(env):
    s, c = env
    d = docx.Document()
    d.add_paragraph("虐待の防止のための措置に関する事項")
    d.save(s.drive_root / "事業所" / "運営規程.docx")
    ref = next(r for r in scan(s) if r.rel_path == "事業所/運営規程.docx")
    _save(c, ref, DocFacts(doc_type="規程", body_status="本文あり"))
    assert "運営規程" not in row(s, c, "R25").next_step


def test_infection_committee_interval_appears(env):
    s, c = env
    put(s, c, "指針.txt", doc_type="指針・マニュアル", summary="感染症の予防及びまん延の防止のための指針")
    put(s, c, "委員会.txt", doc_type="委員会の記録", doc_date="2026-03-10", doc_date_source=Source(quote="開催日"), activities=[activity("委員会", "感染症対策委員会", "2026-03-10")])
    put(s, c, "記録.txt", doc_type="研修の記録", activities=[activity("研修", "感染症研修", "2026-05-01"), activity("訓練", "感染症訓練", "2026-05-02")])
    r = row(s, c, "R24")
    assert r.status == "todo" and "委員会" in r.next_step


def test_pledge_missing_for_staff_is_flagged(env):
    s, c = env
    put(s, c, "秘密保持誓約書_真柴 洋子.txt", doc_type="誓約書", user_name="真柴 洋子", summary="誓約書")
    r = row(s, c, "R31")
    assert r.status == "todo" and "井波" in r.next_step


def test_office_findings_only_for_todo_and_unknown_has_scope(env):
    s, c = env
    put(s, c, "BCP_自然災害編_下書き.txt", doc_type="業務継続計画", body_status="表紙や目次だけ", summary="自然災害編の下書き")
    found = office_findings(c, s)
    bcp = next(f for f in found if f.rule_id == "R23")
    assert bcp.user_id is None and bcp.kind == "finding" and (bcp.evidence or bcp.searched_scope)
    assert "確認してください" in bcp.message and bcp.todo
    assert next(f for f in found if f.rule_id == "R27").kind == "unknown"


def test_office_page_lists_rows_with_plain_words(env):
    s, c = env
    body = TestClient(create_app(s)).get("/office").text
    assert "業務継続計画" in body and "済んでいるもの" in body


def test_declare_from_screen(env):
    s, c = env
    client = TestClient(create_app(s))
    r = client.post("/office/R27/declare", data={"who": "真柴", "basis": "ホームページで確認"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.post("/office/R27/declare", data={"who": "", "basis": ""}).status_code == 400


def test_home_links_to_office_with_count(env):
    s, c = env
    assert "事業所のこと" in TestClient(create_app(s)).get("/").text


def test_committee_mentioning_a_planned_training_is_not_a_training(env):
    s, c = env
    _abuse_docs(s, c, [activity("委員会", "虐待防止委員会（新任職員研修の予定）", "2026-06-15", "真柴 洋子、井波 拓真")])
    assert "虐待防止の研修を年1回以上" in row(s, c, "R25").next_step


def test_training_recorded_under_another_kind_still_counts(env):
    s, c = env
    _abuse_docs(s, c, [activity("会議", "虐待防止研修", "2026-07-10", "真柴 洋子、井波 拓真")])
    assert row(s, c, "R25").status == "done"


def test_old_training_says_when_it_was_last_done(env):
    s, c = env
    _abuse_docs(s, c, [activity("研修", "虐待防止研修", "2025-03-10", "真柴 洋子、井波 拓真")])
    assert "虐待防止の研修は最後が 2025-03-10 で、1年を超えています" in row(s, c, "R25").next_step


def test_latest_of_several_old_trainings_is_named(env):
    s, c = env
    _abuse_docs(s, c, [activity("研修", "虐待防止研修", "2024-03-10"), activity("研修", "虐待防止研修", "2025-03-10")])
    assert "最後が 2025-03-10" in row(s, c, "R25").next_step


def test_training_within_a_year_is_not_called_old(env):
    s, c = env
    _abuse_docs(s, c, [activity("研修", "虐待防止研修", "2026-07-10", "真柴 洋子、井波 拓真")])
    assert "1年を超えています" not in row(s, c, "R25").next_step

