import pytest

from keika.db import connect
from keika.findings import Evidence, Finding, fingerprint
from keika.records import RecordIn, add_record
from keika.responses import evidence_hash, respond, silenced_list, sort_findings
from keika.settings import Settings


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者" / "U01_杉森ハル").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db")
    c = connect(s)
    (root / "利用者" / "U01_杉森ハル" / "計画書.txt").write_text("同意日 8/5", encoding="utf-8")
    yield s, c
    c.close()


REL = "利用者/U01_杉森ハル/計画書.txt"


def f(user="U01", rule="R10", period="2026-08", file=REL):
    return Finding(rule_id=rule, user_id=user, kind="finding", period=period, message="確認してください。", evidence=[Evidence(file=file, quote="同意日")])


def test_new_finding_is_open(env):
    s, c = env
    v = sort_findings(c, s, "U01", [f()])
    assert [x.finding.rule_id for x in v.open] == ["R10"] and v.noted == []


def test_off_target_silences_only_that_finding(env):
    s, c = env
    one, other_month, other_user = f(), f(period="2026-09"), f(user="U02")
    respond(c, s, one, "off_target")
    v = sort_findings(c, s, "U01", [one, other_month])
    assert [x.finding.period for x in v.open] == ["2026-09"]
    assert [x.finding.user_id for x in sort_findings(c, s, "U02", [other_user]).open] == ["U02"]


def test_off_target_returns_when_evidence_changes(env):
    s, c = env
    one = f()
    respond(c, s, one, "off_target")
    (s.drive_root / REL).write_text("同意日 8/1 に書き換え", encoding="utf-8")
    v = sort_findings(c, s, "U01", [one])
    assert len(v.open) == 1 and v.open[0].evidence_changed is True


def test_silenced_findings_can_be_listed_for_the_manager(env):
    s, c = env
    one = f()
    respond(c, s, one, "off_target", who="真柴")
    rows = silenced_list(c)
    assert len(rows) == 1 and rows[0]["who"] == "真柴" and rows[0]["rule_id"] == "R10" and rows[0]["message"]


def test_history_noted_requires_record_link(env):
    s, c = env
    with pytest.raises(ValueError):
        respond(c, s, f(), "history_noted")


def test_history_noted_moves_out_of_open_count(env):
    s, c = env
    one = f()
    rec = add_record(c, RecordIn(user_id="U01", event_date="2026-11-04", body="経緯を残した。"))
    respond(c, s, one, "history_noted", record_id=rec.id)
    v = sort_findings(c, s, "U01", [one])
    assert v.open == [] and len(v.noted) == 1 and v.noted[0].record_id == rec.id


def test_changed_evidence_file_shows_notice(env):
    s, c = env
    one = f()
    rec = add_record(c, RecordIn(user_id="U01", event_date="2026-11-04", body="経緯。"))
    respond(c, s, one, "history_noted", record_id=rec.id)
    (s.drive_root / REL).write_text("差し替え", encoding="utf-8")
    assert sort_findings(c, s, "U01", [one]).noted[0].evidence_changed is True


def test_latest_response_wins(env):
    s, c = env
    one = f()
    rec = add_record(c, RecordIn(user_id="U01", event_date="2026-11-04", body="経緯。"))
    respond(c, s, one, "off_target")
    respond(c, s, one, "history_noted", record_id=rec.id)
    assert len(sort_findings(c, s, "U01", [one]).noted) == 1


def test_responses_are_kept_as_history(env):
    s, c = env
    one = f()
    respond(c, s, one, "off_target")
    respond(c, s, one, "off_target")
    n = c.execute("SELECT COUNT(*) AS n FROM finding_responses WHERE fingerprint = ?", (fingerprint(one),)).fetchone()["n"]
    assert n == 2


def test_evidence_hash_is_stable_without_change(env):
    s, c = env
    assert evidence_hash(s, f()) == evidence_hash(s, f())


def test_unknown_kind_is_refused(env):
    s, c = env
    with pytest.raises(ValueError):
        respond(c, s, f(), "delete")


def test_people_do_not_press_done():
    # 対応したかどうかは、記録を読んで AI が判断する
    from keika.responses import KINDS
    assert "done" not in KINDS
