from datetime import date

import pytest

from keika.db import connect
from keika.files import scan
from keika.ledger import User, upsert_user
from keika.machine_checks import run_machine_checks
from keika.reading import ActivityFact, DocFacts, Source, _save
from keika.records import RecordIn, add_record
from keika.settings import Settings

FOLDER = "利用者/U01_杉森ハル"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / FOLDER).mkdir(parents=True)
    (root / "事業所").mkdir()
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    conn = connect(s)
    upsert_user(conn, User(id="U01", name="杉森 ハル", cm="CM01", data={"type": "居宅介護支援", "cert_period": ["2026-04-01", "2028-03-31"]}))
    yield s, conn
    conn.close()


def put(s, conn, name, facts, folder=FOLDER):
    path = s.drive_root / folder / name
    path.write_text(name, encoding="utf-8")
    ref = next(r for r in scan(s) if r.rel_path == f"{folder}/{name}")
    _save(conn, ref, facts)
    return ref.rel_path


def visit(conn, day, kind_read="居宅訪問", kind_written="訪問", body="訪問した。", user="U01"):
    return add_record(conn, RecordIn(user_id=user, event_date=day, kind_written=kind_written, kind_read=kind_read, body=body))


def of(findings, rule, kind=None, period=None):
    return [f for f in findings if f.rule_id == rule and (kind is None or f.kind == kind) and (period is None or f.period == period)]


def yobo_user(conn):
    upsert_user(conn, User(id="U01", name="杉森 ハル", data={"type": "介護予防支援", "cert_period": ["2026-04-01", "2028-03-31"]}))


def plan(start="2026-05-01", end="2027-04-30", **kw):
    data = dict(doc_type="介護予防サービス計画書", body_status="本文あり", period_start=start, period_end=end,
                period_source=Source(page=1, quote="計画期間"), consent_date="2026-04-25", consent_source=Source(page=1, quote="同意日"), signature_present=True)
    return DocFacts(**(data | kw))


# --- 月1回の訪問 ---


def test_month_without_home_visit_is_flagged(env):
    s, conn = env
    visit(conn, "2026-08-20")
    visit(conn, "2026-10-20")
    f = of(run_machine_checks(conn, s, "U01"), "R06", "finding")
    assert [x.period for x in f] == ["2026-09"]
    assert f[0].searched_scope and "確認してください" in f[0].message


def test_visit_kind_is_taken_from_kind_read_not_kind_written(env):
    s, conn = env
    visit(conn, "2026-09-20")
    visit(conn, "2026-10-20", kind_read="電話", kind_written="訪問", body="本日は電話で確認。")
    f = of(run_machine_checks(conn, s, "U01"), "R06", "finding", "2026-10")
    assert len(f) == 1
    assert "電話" in f[0].message and "訪問" in f[0].message
    assert f[0].evidence[0].record_date == "2026-10-20"


def test_kind_written_phone_with_home_read_is_fine(env):
    s, conn = env
    visit(conn, "2026-10-20", kind_read="居宅訪問", kind_written="電話")
    assert of(run_machine_checks(conn, s, "U01"), "R06", "finding") == []


def test_kind_not_read_gives_unknown_not_finding(env):
    s, conn = env
    visit(conn, "2026-09-20")
    visit(conn, "2026-10-20", kind_read=None)
    fs = run_machine_checks(conn, s, "U01")
    assert of(fs, "R06", "unknown", "2026-10") and of(fs, "R06", "finding") == []


def test_current_month_gives_nudge_with_days_left(env):
    s, conn = env
    visit(conn, "2026-10-20")
    n = of(run_machine_checks(conn, s, "U01"), "R06", "nudge", "2026-11")
    assert len(n) == 1 and "26日" in n[0].message


def test_current_month_visited_gives_no_nudge(env):
    s, conn = env
    visit(conn, "2026-11-02")
    assert of(run_machine_checks(conn, s, "U01"), "R06", "nudge") == []


def test_no_records_at_all_gives_no_month_findings(env):
    s, conn = env
    assert of(run_machine_checks(conn, s, "U01"), "R06") == []


# --- 予防の3か月 ---


def test_yobo_three_month_window_counts_from_plan_start(env):
    s, conn = env
    yobo_user(conn)
    put(s, conn, "計画.txt", plan())
    visit(conn, "2026-08-10")  # 開始 5/1 なら 6〜8 月に 8 月の訪問で足りる
    assert of(run_machine_checks(conn, s, "U01"), "R06", "finding") == []


def test_yobo_window_missing_visit_is_flagged(env):
    s, conn = env
    yobo_user(conn)
    put(s, conn, "計画.txt", plan())
    visit(conn, "2026-06-01", kind_read="電話")
    f = of(run_machine_checks(conn, s, "U01"), "R06", "finding")
    assert [x.period for x in f] == ["2026-06〜2026-08"]


def test_yobo_window_partly_before_records_is_unknown(env):
    s, conn = env
    yobo_user(conn)
    put(s, conn, "計画.txt", plan())
    visit(conn, "2026-08-10", kind_read="電話")  # 記録は 8 月から。6・7 月は見えない
    fs = run_machine_checks(conn, s, "U01")
    assert of(fs, "R06", "finding") == [] and of(fs, "R06", "unknown", "2026-06〜2026-08")


def test_yobo_without_plan_gives_unknown(env):
    s, conn = env
    yobo_user(conn)
    visit(conn, "2026-08-10")
    assert of(run_machine_checks(conn, s, "U01"), "R06", "unknown")


def test_yobo_window_in_progress_gives_nudge_in_last_month(env):
    s, conn = env
    yobo_user(conn)
    put(s, conn, "計画.txt", plan(start="2026-08-01"))  # 9〜11 月の窓の最後の月
    visit(conn, "2026-09-05", kind_read="電話")
    n = of(run_machine_checks(conn, s, "U01"), "R06", "nudge")
    assert len(n) == 1 and n[0].period == "2026-09〜2026-11"


# --- 署名 ---


def usage(month, signed):
    return DocFacts(doc_type="利用票", body_status="本文あり", target_month=month, signature_present=signed, signature_source=Source(page=1, quote="署名欄"))


def test_missing_signature_month_is_flagged_when_signature_false(env):
    s, conn = env
    rel = put(s, conn, "利用票_2026-09_変更.txt", usage("2026-09", False))
    f = of(run_machine_checks(conn, s, "U01"), "R13", "finding")
    assert len(f) == 1 and f[0].period == "2026-09"
    assert f[0].evidence[0].file == rel and f[0].evidence[0].page == 1


def test_signature_none_gives_unknown(env):
    s, conn = env
    put(s, conn, "利用票.txt", usage("2026-09", None))
    fs = run_machine_checks(conn, s, "U01")
    assert of(fs, "R13", "unknown") and of(fs, "R13", "finding") == []


def test_signature_true_is_silent(env):
    s, conn = env
    put(s, conn, "利用票.txt", usage("2026-09", True))
    assert of(run_machine_checks(conn, s, "U01"), "R13") == []


def test_future_month_usage_is_not_checked(env):
    s, conn = env
    put(s, conn, "利用票.txt", usage("2026-12", False))
    assert of(run_machine_checks(conn, s, "U01"), "R13") == []


# --- 同意 ---


def test_consent_after_plan_start_is_flagged(env):
    s, conn = env
    rel = put(s, conn, "計画.txt", plan(doc_type="居宅サービス計画書", consent_date="2026-05-10"))
    f = of(run_machine_checks(conn, s, "U01"), "R10", "finding")
    assert len(f) == 1 and f[0].evidence[0].file == rel and "2026-05-10" in f[0].message


def test_consent_before_plan_start_is_silent(env):
    s, conn = env
    put(s, conn, "計画.txt", plan(doc_type="居宅サービス計画書"))
    assert of(run_machine_checks(conn, s, "U01"), "R10") == []


def test_consent_date_missing_gives_unknown(env):
    s, conn = env
    put(s, conn, "計画.txt", plan(doc_type="居宅サービス計画書", consent_date=None, consent_source=None))
    assert of(run_machine_checks(conn, s, "U01"), "R10", "unknown")


def test_older_plan_is_not_checked_for_consent(env):
    s, conn = env
    put(s, conn, "旧.txt", plan(doc_type="居宅サービス計画書", start="2025-05-01", end="2026-04-30", consent_date="2025-05-10"))
    put(s, conn, "新.txt", plan(doc_type="居宅サービス計画書"))
    assert of(run_machine_checks(conn, s, "U01"), "R10") == []


# --- 先回り ---


def test_plan_expiring_gives_nudge(env):
    s, conn = env
    put(s, conn, "計画.txt", plan(doc_type="居宅サービス計画書", end="2026-12-31"))
    n = of(run_machine_checks(conn, s, "U01"), "R45", "nudge")
    assert len(n) == 1 and "2026-12-31" in n[0].message


def test_plan_far_from_expiry_is_silent(env):
    s, conn = env
    put(s, conn, "計画.txt", plan(doc_type="居宅サービス計画書"))
    assert of(run_machine_checks(conn, s, "U01"), "R45") == []


def test_cert_renewal_30_days_gives_nudge(env):
    s, conn = env
    upsert_user(conn, User(id="U01", name="杉森 ハル", data={"type": "居宅介護支援", "cert_period": ["2025-12-01", "2026-11-30"]}))
    n = of(run_machine_checks(conn, s, "U01"), "R44", "nudge")
    assert len(n) == 1 and "2026-11-30" in n[0].message and n[0].searched_scope


def test_cert_far_from_expiry_is_silent(env):
    s, conn = env
    assert of(run_machine_checks(conn, s, "U01"), "R44") == []


# --- 書類の有無 ---


def test_missing_doc_finding_shows_searched_scope(env):
    s, conn = env
    put(s, conn, "計画.txt", plan(doc_type="居宅サービス計画書"))
    f = of(run_machine_checks(conn, s, "U01"), "R33", "finding")
    assert {x.period for x in f} == {"アセスメント", "サービス担当者会議の要点"}
    assert all(FOLDER in x.searched_scope for x in f)
    assert all("別の場所" in x.message for x in f)


def test_unread_folder_gives_no_missing_doc_findings(env):
    # まだ読み取っていない書類を「無い」と言わない
    s, conn = env
    (s.drive_root / FOLDER / "未読.txt").write_text("x", encoding="utf-8")
    assert of(run_machine_checks(conn, s, "U01"), "R33") == []


def test_folder_with_unread_files_gives_unknown_not_missing(env):
    s, conn = env
    put(s, conn, "計画.txt", plan(doc_type="居宅サービス計画書"))
    (s.drive_root / FOLDER / "未読.txt").write_text("x", encoding="utf-8")
    fs = run_machine_checks(conn, s, "U01")
    assert of(fs, "R33", "finding") == [] and of(fs, "R33", "unknown")


# --- 事業所 ---


def committee(date_str):
    return DocFacts(doc_type="委員会の記録", body_status="本文あり", doc_date=date_str, doc_date_source=Source(quote="開催日"),
                    activities=[ActivityFact(kind="委員会", date=date_str, title="感染症対策委員会")])


def test_committee_interval_over_6_months_is_flagged(env):
    s, conn = env
    put(s, conn, "感染1.txt", committee("2025-09-12"), "事業所")
    rel = put(s, conn, "感染2.txt", committee("2026-03-10"), "事業所")
    f = of(run_machine_checks(conn, s), "R24", "finding")
    assert len(f) == 1 and f[0].user_id is None and f[0].evidence[0].file == rel
    assert "2026-03-10" in f[0].message


def test_committee_gap_between_meetings_is_flagged(env):
    s, conn = env
    put(s, conn, "感染1.txt", committee("2025-01-10"), "事業所")
    put(s, conn, "感染2.txt", committee("2026-08-10"), "事業所")
    assert len(of(run_machine_checks(conn, s), "R24", "finding")) == 1


def test_committee_within_6_months_is_silent(env):
    s, conn = env
    put(s, conn, "感染1.txt", committee("2026-03-10"), "事業所")
    put(s, conn, "感染2.txt", committee("2026-09-10"), "事業所")
    assert of(run_machine_checks(conn, s), "R24", "finding") == []


def test_user_filter_skips_office(env):
    s, conn = env
    put(s, conn, "感染.txt", committee("2020-01-01"), "事業所")
    assert of(run_machine_checks(conn, s, "U01"), "R24") == []


def test_same_kind_written_and_read_is_not_described_as_a_mismatch(env):
    s, conn = env
    visit(conn, "2026-09-20")
    visit(conn, "2026-10-08", kind_read="事業所訪問", kind_written="事業所訪問", body="デイで面会。")
    visit(conn, "2026-10-20", kind_read="電話", kind_written="訪問", body="電話で確認。")
    f = of(run_machine_checks(conn, s, "U01"), "R06", "finding", "2026-10")[0]
    assert "2026-10-20" in f.message and "2026-10-08 は種別" not in f.message
    assert "デイで面会。" in [e.quote for e in f.evidence]


def test_meeting_written_in_another_document_is_not_called_missing(env):
    s, conn = env
    meeting = ActivityFact(kind="会議", date="2026-04-28", title="サービス担当者会議（本人宅）")
    put(s, conn, "計画.txt", plan(doc_type="居宅サービス計画書", activities=[meeting]))
    fs = run_machine_checks(conn, s, "U01")
    assert {x.period for x in of(fs, "R33", "finding")} == {"アセスメント"}
    told = of(fs, "R33", "unknown")
    assert len(told) == 1 and "2026-04-28" in told[0].message and "要点" in told[0].message
