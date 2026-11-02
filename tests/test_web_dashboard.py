import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.claude_checks import CheckRun, Dismissal, Resolution, save_run
from keika.db import connect
from keika.findings import Evidence, Finding
from keika.files import scan
from keika.ledger import get_user, seed_from_json
from keika.reading import DocFacts, Source, TextFact, _save
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.web.app import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(drive_root=SAMPLE / "drive", db_path=tmp_path / "keika.db", as_of=date(2026, 11, 4))


@pytest.fixture
def conn(settings):
    c = connect(settings)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield c
    c.close()


@pytest.fixture
def client(settings, conn):
    return TestClient(create_app(settings))


def test_dashboard_lists_users_of_selected_cm(client):
    body = client.get("/", params={"cm": "CM02"}).text
    assert "柚木 茂" in body and "杉森 ハル" not in body


def test_dashboard_lists_everyone_without_cm(client):
    body = client.get("/").text
    assert "柚木 茂" in body and "杉森 ハル" in body


def test_dashboard_can_switch_person_in_charge(client):
    body = client.get("/").text
    assert "真柴 洋子" in body and "井波 拓真" in body


def test_dashboard_shows_demo_date_banner(client):
    assert "デモ基準日 2026-11-04" in client.get("/").text


def test_no_banner_without_as_of(tmp_path):
    s = Settings(drive_root=SAMPLE / "drive", db_path=tmp_path / "x.db")
    assert "デモ基準日" not in TestClient(create_app(s)).get("/").text


def test_every_page_shows_demo_banner_and_final_judgment_note(client):
    for path in ("/", "/users/U04", "/ledger"):
        body = client.get(path).text
        assert "デモ基準日" in body and "最終判断は事業所と保険者" in body


def test_dashboard_shows_cert_end_and_month_visit_status(client, conn):
    add_record(conn, RecordIn(user_id="U04", event_date="2026-11-02", kind_read="居宅訪問", body="訪問した。"))
    body = client.get("/board", params={"cm": "CM02"}).text
    assert "2027/11/30" in body
    assert "✓" in body


def test_ledger_add_and_edit(client, conn):
    r = client.post("/ledger", data={"id": "", "name": "試験 花子", "kana": "しけん はなこ", "cm": "CM01", "care_level": "要介護1", "cert_start": "2026-01-01", "cert_end": "2026-12-31", "type": "居宅介護支援"}, follow_redirects=False)
    assert r.status_code == 303
    assert "試験 花子" in client.get("/ledger").text
    new_id = next(u for u in __import__("keika.ledger", fromlist=["list_users"]).list_users(conn) if u.name == "試験 花子").id
    client.post("/ledger", data={"id": new_id, "name": "試験 花", "kana": "", "cm": "CM01", "care_level": "要介護2", "cert_start": "", "cert_end": "", "type": ""})
    assert get_user(conn, new_id).name == "試験 花"
    assert get_user(conn, new_id).data["care_level"] == "要介護2"


def test_ledger_edit_keeps_other_data(client, conn):
    before = get_user(conn, "U04").data
    client.post("/ledger", data={"id": "U04", "name": "柚木 茂", "kana": "ゆのき しげる", "cm": "CM02", "care_level": "要介護3", "cert_start": "2025-12-01", "cert_end": "2027-11-30", "type": "居宅介護支援"})
    after = get_user(conn, "U04").data
    assert after["care_level"] == "要介護3" and after["family"] == before["family"]


def test_user_page_shows_extracted_points_with_source(client, conn, settings):
    ref = next(r for r in scan(settings) if r.user_folder and r.user_folder.startswith("U04") and "計画書" in r.rel_path)
    facts = DocFacts(doc_type="居宅サービス計画書", body_status="本文あり", summary="更新時の計画書",
                     issues=[TextFact(text="転ばずに歩きたい", source=Source(page=3, quote="転ばずに自分の足で歩きたい"))])
    _save(conn, ref, facts)
    body = client.get("/users/U04").text
    assert "転ばずに歩きたい" in body and "転ばずに自分の足で歩きたい" in body and "3ページ" in body
    assert ref.rel_path.split("/")[-1] in body


def test_user_page_shows_records_with_written_at(client, conn):
    add_record(conn, RecordIn(user_id="U04", event_date="2026-11-01", kind_written="訪問", kind_read="電話", body="電話で確認した。"))
    body = client.get("/users/U04").text
    assert "電話で確認した。" in body and "未確認" in body


def test_user_page_shows_finding_with_basis_and_confirm_wording(client, conn):
    add_record(conn, RecordIn(user_id="U04", event_date="2026-09-01", kind_read="電話", kind_written="訪問", body="電話で確認。"))
    body = client.get("/users/U04").text
    assert "確認してください" in body and "13条14号" in body


def test_unknown_user_gives_404(client):
    assert client.get("/users/UX").status_code == 404


def test_html_is_escaped(client, conn):
    add_record(conn, RecordIn(user_id="U04", event_date="2026-11-01", body="<script>alert(1)</script>"))
    assert "<script>alert(1)" not in client.get("/users/U04").text


def _claude(**kw):
    data = dict(rule_id="R11", user_id="U04", kind="finding", period="2026-09", source="claude", message="週2回のままです。確認してください。",
                evidence=[Evidence(file="利用者/U04_柚木茂/利用票.pdf", page=1, quote="月・水・木")], suggestion="〇〇月〇〇日に経緯を確認した。")
    return Finding(**(data | kw))


def test_user_page_shows_claude_finding_with_suggestion_blank(client, conn):
    save_run(conn, "U04", CheckRun(findings=[_claude()]))
    body = client.get("/users/U04").text
    assert "週2回のままです" in body and "月・水・木" in body and "書き足し案" in body and 'class="blank"' in body


def test_dashboard_counts_claude_findings(client, conn):
    save_run(conn, "U04", CheckRun(findings=[_claude()]))
    assert "週2回のままです" in client.get("/", params={"cm": "CM02"}).text


def test_dismissed_candidate_is_hidden_and_shown_as_a_note(client, conn):
    add_record(conn, RecordIn(user_id="U04", event_date="2026-08-05", kind_read="居宅訪問", body="訪問。"))
    add_record(conn, RecordIn(user_id="U04", event_date="2026-09-10", kind_read="電話", kind_written="電話", body="入院中のため訪問できず。"))
    add_record(conn, RecordIn(user_id="U04", event_date="2026-10-05", kind_read="居宅訪問", body="訪問。"))
    assert "9月に居宅を訪問" in client.get("/users/U04").text
    save_run(conn, "U04", CheckRun(dismissed=[Dismissal("R06", "2026-09", "入院中", "2026-09-10", "入院中のため")]))
    body = client.get("/users/U04").text
    assert "9月に居宅を訪問" not in body and "読み直して取り下げました" in body and "入院中" in body


def test_failed_check_is_shown_as_not_run(client, conn):
    save_run(conn, "U04", CheckRun(not_run=True, error="APIConnectionError"))
    assert "未実施" in client.get("/users/U04").text


def _one_finding(conn):
    save_run(conn, "U04", CheckRun(findings=[_claude(evidence=[Evidence(file="利用者/U04_柚木茂/担当者変更のお知らせ_2026-07-01.docx", quote="担当")])]))


def test_evidence_opens_original_on_click(client, conn):
    add_record(conn, RecordIn(user_id="U04", event_date="2026-10-08", kind_read="事業所訪問", kind_written="訪問", body="デイで面会した。全文はここまで。"))
    save_run(conn, "U04", CheckRun(findings=[_claude(evidence=[Evidence(file="支援経過（アプリの記録）", record_date="2026-10-08", quote="デイで面会")])]))
    body = client.get("/users/U04").text
    assert "<details" in body and "支援経過 10/8" in body and "全文はここまで" in body


def test_card_hides_file_names_and_long_dates(client, conn):
    _one_finding(conn)
    body = client.get("/users/U04").text
    card = re.sub(r"<[^>]+>", "", body[body.index('class="card'):body.index("</article>")])
    assert ".docx" not in card and "2026-07-01" not in card


def test_doc_route_opens_inside_and_refuses_outside(client):
    assert client.get("/doc", params={"path": "利用者/U04_柚木茂/契約書_2024-02-15.pdf"}).status_code == 200
    assert client.get("/doc", params={"path": "../ledger.json"}).status_code == 403
    assert client.get("/doc", params={"path": "利用者/無い.pdf"}).status_code == 404


def test_respond_off_target_hides_finding_and_lists_it(client, conn):
    from keika.findings import fingerprint
    _one_finding(conn)
    fp = fingerprint(_claude(evidence=[Evidence(file="利用者/U04_柚木茂/担当者変更のお知らせ_2026-07-01.docx", quote="担当")]))
    r = client.post("/users/U04/respond", data={"fp": fp, "kind": "off_target"}, follow_redirects=False)
    assert r.status_code == 303
    assert "週2回のままです" not in client.get("/users/U04").text
    assert "週2回のままです" in client.get("/silenced").text


def test_respond_history_noted_adds_todays_record(client, conn):
    from keika.findings import fingerprint
    _one_finding(conn)
    fp = fingerprint(_claude(evidence=[Evidence(file="利用者/U04_柚木茂/担当者変更のお知らせ_2026-07-01.docx", quote="担当")]))
    client.post("/users/U04/respond", data={"fp": fp, "kind": "history_noted", "note": "今日、経緯を確認した。"})
    body = client.get("/users/U04").text
    assert "今日、経緯を確認した。" in body and "経緯あり" in body
    assert client.post("/users/U04/respond", data={"fp": fp, "kind": "history_noted", "note": " "}).status_code == 400
    from keika.records import list_records
    assert [r.event_date for r in list_records(conn, "U04") if r.kind_written == "経緯"] == ["2026-11-04"]


def test_respond_unknown_finding_gives_404(client):
    assert client.post("/users/U04/respond", data={"fp": "nope", "kind": "done"}).status_code == 404


def test_blank_marks_keep_the_blank_characters(client, conn):
    save_run(conn, "U04", CheckRun(findings=[_claude()]))
    assert '<mark class="blank">〇〇</mark>月' in client.get("/users/U04").text


def test_user_page_shows_closed_findings_with_the_record(client, conn):
    save_run(conn, "U04", CheckRun(resolved=[Resolution("R11", "2026-10", "転倒後の見直しを確認してください。", "見直しの記録あり", "2026-11-04", "計画を見直した")]))
    body = client.get("/users/U04").text
    assert "記録で確認して閉じました" in body and "転倒後の見直し" in body and "計画を見直した" in body


def test_no_done_button(client, conn):
    save_run(conn, "U04", CheckRun(findings=[_claude()]))
    body = client.get("/users/U04").text
    assert "対応した" not in body and "確認した・問題なし" in body


def test_dashboard_summary_per_person_in_charge(client, conn):
    from keika.responses import respond
    from keika.settings import Settings
    s = Settings(drive_root=SAMPLE / "drive", db_path=conn.execute("PRAGMA database_list").fetchone()["file"])
    one, two = _claude(), _claude(period="2026-10", message="別の指摘です。確認してください。")
    save_run(conn, "U04", CheckRun(findings=[one, two], resolved=[Resolution("R11", "2026-08", "前の指摘", "直った", "2026-11-04", "直した")]))
    respond(conn, s, two, "off_target", who="井波")
    body = client.get("/board").text
    row = body[body.index('class="summary"'):body.index("</table>")]
    assert "井波 拓真" in row
    i = row.index("井波 拓真")
    assert "問題なし 1" in row[i:] and "閉じた 1" in row[i:]
