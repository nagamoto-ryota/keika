from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
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
    body = client.get("/", params={"cm": "CM02"}).text
    assert "2027-11-30" in body
    assert "済" in body


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
