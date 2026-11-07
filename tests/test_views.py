import pytest

from keika.findings import Evidence
from keika.records import Record
from keika.settings import Settings
from keika.views import evidence_view, hdate


@pytest.fixture
def s(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者" / "U01_杉森ハル").mkdir(parents=True)
    (root / "利用者" / "U01_杉森ハル" / "居宅サービス計画書_2026-07-30.txt").write_text("前の行\n計画の説明・同意：2026-08-05 本人に説明\n後の行\n遠い行\n遠い行2", encoding="utf-8")
    return Settings(drive_root=root, db_path=tmp_path / "x.db")


def rec(day="2026-10-08", body="デイで面会した。長い本文です。", **kw):
    data = dict(id=1, user_id="U01", event_date=day, event_time="10:00", kind_written="訪問", kind_read="事業所訪問", monitoring=None,
                partner="本人", body=body, author="井波", first_written_at=None, source="csv:利用者/U01_杉森ハル/支援経過_2026-08-01_2026-11-03.csv", unchecked=False)
    return Record(**(data | kw))


def test_hdate_shortens_dates_of_this_year():
    assert hdate("2026-08-05 が同意日", 2026) == "8/5 が同意日"
    assert hdate("2026-09 に訪問", 2026) == "9月 に訪問"
    assert hdate("2026-09〜2026-11", 2026) == "9月〜11月"


def test_hdate_keeps_year_when_other_year():
    assert hdate("2025-11-27", 2026) == "2025/11/27"
    assert hdate("2025-09", 2026) == "2025年9月"


def test_record_evidence_label_is_short_and_context_is_full_record(s):
    e = Evidence(file="利用者/U01_杉森ハル/支援経過_2026-08-01_2026-11-03.csv", record_date="2026-10-08", quote="デイで面会")
    v = evidence_view(s, e, [rec()], 2026)
    assert v.label == "支援経過 10/8"
    assert "デイで面会した。長い本文です。" in v.context
    assert ".csv" not in v.label


def test_doc_evidence_label_drops_extension_and_date(s):
    e = Evidence(file="利用者/U01_杉森ハル/居宅サービス計画書_2026-07-30.txt", quote="計画の説明・同意")
    v = evidence_view(s, e, [], 2026)
    assert v.label == "居宅サービス計画書"


def test_doc_label_has_page(s):
    e = Evidence(file="利用者/U01_杉森ハル/利用票_2026-09_変更.pdf", page=2, quote="署名")
    assert evidence_view(s, e, [], 2026).label == "利用票 2ページ"


def test_text_doc_context_shows_lines_around_quote(s):
    e = Evidence(file="利用者/U01_杉森ハル/居宅サービス計画書_2026-07-30.txt", quote="計画の説明・同意")
    v = evidence_view(s, e, [], 2026)
    assert "前の行" in v.context and "後の行" in v.context and "遠い行2" not in v.context
    assert v.highlight == "計画の説明・同意"


def test_missing_file_context_falls_back_to_quote(s):
    e = Evidence(file="利用者/U01_杉森ハル/無い.txt", quote="引用")
    v = evidence_view(s, e, [], 2026)
    assert v.context == "引用" and v.open_url is None


def test_pdf_has_link_to_open_at_page(s):
    (s.drive_root / "利用者" / "U01_杉森ハル" / "a.pdf").write_bytes(b"%PDF-1.4")
    e = Evidence(file="利用者/U01_杉森ハル/a.pdf", page=3, quote="署名欄")
    v = evidence_view(s, e, [], 2026)
    assert v.context == "署名欄"
    assert "/doc?" in v.open_url and v.open_url.endswith("#page=3")
