import sqlite3
from datetime import date, datetime, timedelta

import pytest

from conftest import SAMPLE
from keika import records as rec
from keika.db import connect
from keika.files import scan
from keika.ledger import User, seed_from_json, upsert_user
from keika.records import RecordIn, add_record, edit_record, get_record, history, import_csv, list_records, mark_checked
from keika.settings import Settings
from test_reading import FakeClient, answer


def base_in(**kw) -> RecordIn:
    data = dict(user_id="U01", event_date="2026-11-02", event_time="10:00", kind_written="訪問", partner="本人", body="訪問した。", author="真柴", source="manual")
    return RecordIn(**(data | kw))


def test_add_record_is_unchecked_and_keeps_fields(conn):
    r = add_record(conn, base_in())
    assert r.unchecked is True and r.body == "訪問した。" and r.event_date == "2026-11-02"


def test_edit_changes_body_and_keeps_old_version(conn):
    r = add_record(conn, base_in())
    edit_record(conn, r.id, {"body": "訪問した。元気だった。"}, editor="真柴")
    assert get_record(conn, r.id).body == "訪問した。元気だった。"
    vs = history(conn, r.id)
    assert [v.body for v in vs] == ["訪問した。", "訪問した。元気だった。"]
    assert vs[1].editor == "真柴" and vs[1].edited_at


def test_edit_without_change_adds_no_version(conn):
    r = add_record(conn, base_in())
    edit_record(conn, r.id, {"body": "訪問した。"}, editor="真柴")
    assert len(history(conn, r.id)) == 1


def test_first_written_at_never_changes_on_edit(conn):
    r = add_record(conn, base_in())
    edit_record(conn, r.id, {"body": "直した"}, editor="真柴")
    assert get_record(conn, r.id).first_written_at == r.first_written_at


def test_first_written_at_cannot_be_edited(conn):
    r = add_record(conn, base_in())
    with pytest.raises(ValueError):
        edit_record(conn, r.id, {"first_written_at": "2000-01-01"}, editor="真柴")


def test_first_written_at_uses_real_clock_with_as_of_set(conn, sample_settings):
    assert sample_settings.as_of == date(2026, 11, 4)
    r = add_record(conn, base_in())
    assert abs(datetime.fromisoformat(r.first_written_at) - datetime.now().astimezone()) < timedelta(minutes=1)


def test_event_date_can_be_in_the_past(conn):
    assert add_record(conn, base_in(event_date="2026-10-01")).event_date == "2026-10-01"


def test_reassign_user_is_in_history(conn):
    r = add_record(conn, base_in())
    edit_record(conn, r.id, {"user_id": "U02"}, editor="真柴")
    assert [v.user_id for v in history(conn, r.id)] == ["U01", "U02"]


def test_mark_checked_clears_mark_and_is_in_history(conn):
    r = add_record(conn, base_in())
    mark_checked(conn, r.id, editor="真柴")
    assert get_record(conn, r.id).unchecked is False
    assert [v.unchecked for v in history(conn, r.id)] == [True, False]


def test_history_has_no_delete_api():
    names = [n for n in dir(rec) if any(w in n.lower() for w in ("delete", "remove", "purge"))]
    assert names == []


def test_history_rows_cannot_be_deleted_or_changed_even_by_sql(conn):
    r = add_record(conn, base_in())
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM record_versions WHERE record_id = ?", (r.id,))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE record_versions SET snapshot = '{}' WHERE record_id = ?", (r.id,))


def test_records_cannot_be_deleted_by_sql(conn):
    r = add_record(conn, base_in())
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM records WHERE id = ?", (r.id,))


def test_first_written_at_cannot_be_changed_by_sql(conn):
    r = add_record(conn, base_in())
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE records SET first_written_at = '2000-01-01' WHERE id = ?", (r.id,))


def test_list_records_orders_by_event_date(conn):
    add_record(conn, base_in(event_date="2026-11-02"))
    add_record(conn, base_in(event_date="2026-10-01"))
    assert [r.event_date for r in list_records(conn, "U01")] == ["2026-10-01", "2026-11-02"]


# --- CSV の取り込み ---

CSV_TEXT = "日付,時刻,種別,相手,本文,記録者\n2026-10-20,14:00,訪問,長男,本日は電話にて確認。,井波\n2026-10-08,10:00,訪問,本人,事業所で面会。,井波\n"


def mapping(**kw):
    data = dict(
        date_column="日付", time_column="時刻", kind_column="種別", partner_column="相手", body_column="本文", author_column="記録者",
        rows=[
            dict(row=1, kind_read="電話", monitoring=True, reason="本文に電話とある"),
            dict(row=2, kind_read="事業所訪問", monitoring=False, reason="事業所で面会"),
        ],
    )
    return rec.WireCsv(**(data | kw))


@pytest.fixture
def csv_env(tmp_path):
    root = tmp_path / "drive"
    folder = root / "利用者" / "杉森ハル"
    folder.mkdir(parents=True)
    (folder / "経過.csv").write_text(CSV_TEXT, encoding="utf-8")
    s = Settings(drive_root=root, db_path=tmp_path / "x.db")
    c = connect(s)
    upsert_user(c, User(id="U01", name="杉森 ハル"))
    yield s, c, folder / "経過.csv"
    c.close()


def do_import(s, c, client=None):
    client = client or FakeClient(answer(mapping()))
    return import_csv(c, client, s, scan(s)[0]), client


def test_csv_rows_become_records(csv_env):
    s, c, _ = csv_env
    result, _ = do_import(s, c)
    assert result.added == 2
    rows = list_records(c, "U01")
    assert [r.event_date for r in rows] == ["2026-10-08", "2026-10-20"]
    assert rows[1].kind_written == "訪問" and rows[1].kind_read == "電話" and rows[1].partner == "長男" and rows[1].monitoring is True


def test_csv_record_first_written_at_is_unknown(csv_env):
    s, c, _ = csv_env
    do_import(s, c)
    assert list_records(c, "U01")[0].first_written_at is None


def test_csv_records_start_unchecked_false(csv_env):
    # 介護ソフトの既存の記録は人が書いた物なので「未確認」にしない
    s, c, _ = csv_env
    do_import(s, c)
    assert list_records(c, "U01")[0].unchecked is False


def test_reimport_same_csv_adds_nothing(csv_env):
    s, c, _ = csv_env
    do_import(s, c)
    result, client = do_import(s, c)
    assert result.added == 0 and result.changed == [] and result.unchanged == 2
    assert len(list_records(c, "U01")) == 2
    assert client.calls == []  # 同じ中身なら Claude を呼ばない


def test_reimport_changed_row_adds_a_version_and_reports_it(csv_env):
    s, c, path = csv_env
    do_import(s, c)
    path.write_text(CSV_TEXT.replace("事業所で面会。", "事業所で面会した。"), encoding="utf-8")
    result, _ = do_import(s, c)
    assert result.added == 0 and len(result.changed) == 1
    changed = next(r for r in list_records(c, "U01") if r.event_date == "2026-10-08")
    assert changed.body == "事業所で面会した。"
    assert [v.body for v in history(c, changed.id)] == ["事業所で面会。", "事業所で面会した。"]


def test_csv_api_failure_reports_error_and_saves_nothing(csv_env):
    s, c, _ = csv_env
    result, _ = do_import(s, c, FakeClient(ValueError("壊れている")))
    assert result.error and list_records(c, "U01") == []


def test_csv_with_unknown_column_reports_error(csv_env):
    s, c, _ = csv_env
    result, _ = do_import(s, c, FakeClient(answer(mapping(body_column="無い列"))))
    assert result.error and list_records(c, "U01") == []


def test_csv_folder_without_user_reports_error(tmp_path):
    root = tmp_path / "drive"
    folder = root / "利用者" / "台帳に無い人"
    folder.mkdir(parents=True)
    (folder / "経過.csv").write_text(CSV_TEXT, encoding="utf-8")
    s = Settings(drive_root=root, db_path=tmp_path / "x.db")
    c = connect(s)
    result, client = do_import(s, c)
    assert result.error and client.calls == []
    c.close()


def test_sample_csv_header_can_be_mapped(conn, sample_settings):
    seed_from_json(conn, SAMPLE / "ledger.json")
    ref = next(r for r in scan(sample_settings) if r.ext == "csv" and r.user_folder.startswith("U04"))
    wire = mapping(rows=[])
    result = import_csv(conn, FakeClient(answer(wire)), sample_settings, ref)
    assert result.added > 5
    assert all(r.first_written_at is None for r in list_records(conn, "U04"))
    assert all(r.kind_read is None for r in list_records(conn, "U04"))  # 読めなかった行は「不明」のまま
