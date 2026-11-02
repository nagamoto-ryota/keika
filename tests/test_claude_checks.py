from datetime import date
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from keika.claude_checks import CheckRun, Dismissal, Resolution, latest_run, load_skills, run_claude_checks, save_run, source_texts, validate_finding
from keika.db import connect
from keika.findings import Evidence, Finding
from keika.ledger import User, upsert_user
from keika.machine_checks import run_machine_checks
from keika.reading import DocFacts, _save
from keika.files import scan
from keika.records import RecordIn, add_record
from keika.settings import Settings
from test_reading import FakeClient, FakeStream

FILE = "利用者/U01_杉森ハル/計画書.txt"
TEXTS = {FILE: "週2回 通所介護 を利用する。転倒に注意する。"}


def f(**kw):
    data = dict(rule_id="R03", user_id="U01", kind="finding", period="2026-10", message="週2回のままです。確認してください。",
                evidence=[Evidence(file=FILE, quote="週2回")])
    return Finding(**(data | kw))


def test_valid_finding_is_kept():
    assert validate_finding(f(), TEXTS) is not None


def test_invented_quote_is_dropped():
    assert validate_finding(f(evidence=[Evidence(file=FILE, quote="週5回")]), TEXTS) is None


def test_quote_check_ignores_spaces_and_newlines():
    assert validate_finding(f(evidence=[Evidence(file=FILE, quote="週 2回\n通所介護")]), TEXTS) is not None


def test_quote_in_scan_without_text_cannot_be_checked_and_is_kept():
    assert validate_finding(f(evidence=[Evidence(file="s.pdf", quote="署名欄")]), {"s.pdf": None}) is not None


def test_finding_without_location_or_scope_is_dropped():
    assert validate_finding(f(evidence=[]), TEXTS) is None


def test_finding_with_searched_scope_only_is_kept():
    assert validate_finding(f(evidence=[], searched_scope="利用者/U01_杉森ハル/ を探しました"), TEXTS) is not None


def test_unknown_file_is_dropped():
    assert validate_finding(f(evidence=[Evidence(file="無い.pdf", quote="週2回")]), TEXTS) is None


def test_one_good_evidence_keeps_finding_and_removes_the_bad_one():
    out = validate_finding(f(evidence=[Evidence(file=FILE, quote="週2回"), Evidence(file=FILE, quote="作り話")]), TEXTS)
    assert [e.quote for e in out.evidence] == ["週2回"]


def test_message_with_medication_advice_is_dropped():
    assert validate_finding(f(message="この薬を減らすべきです。確認してください。"), TEXTS) is None
    assert validate_finding(f(message="すぐ受診が必要です。確認してください。"), TEXTS) is None


def test_suggestion_with_medication_advice_is_dropped():
    out = validate_finding(f(suggestion="薬を中止するよう伝えた。"), TEXTS)
    assert out is not None and out.suggestion is None


def test_prompting_info_sharing_with_doctor_is_kept():
    msg = "転倒が続いています。主治医への情報提供やプランの見直しは検討済みか確認してください。"
    out = validate_finding(f(message=msg, suggestion="主治医へ〇〇の情報提供を行った。"), TEXTS)
    assert out.message == msg and out.suggestion


def test_suggestion_keeps_blanks():
    out = validate_finding(f(suggestion="〇〇月〇〇日に本人へ説明した。"), TEXTS)
    assert "〇〇" in out.suggestion


def test_suggestion_backdating_is_dropped():
    for bad in ("過去の日付で記録を追記する。", "10月20日付で書き足す。", "日付をさかのぼって署名をもらう。", "2026-10-20付で追記する。"):
        assert validate_finding(f(suggestion=bad), TEXTS).suggestion is None


def test_today_dated_history_note_is_kept():
    out = validate_finding(f(suggestion="今日の日付で経緯を残す。"), TEXTS)
    assert out.suggestion


def test_assertive_message_gets_confirm_wording():
    assert "確認してください" in validate_finding(f(message="週2回のままです。"), TEXTS).message


# --- 実行 ---


def tool(name, input, id="t1"):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def msg(*blocks, stop_reason="tool_use"):
    usage = SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=usage)


END = msg(SimpleNamespace(type="text", text="終わり"), stop_reason="end_turn")
REL = "利用者/U01_杉森ハル/計画書.txt"


def report(**kw):
    data = dict(rule_id="R03", kind="finding", period="2026-10", message="週2回のままです。確認してください。", suggestion="",
                searched_scope="", evidence=[dict(file=REL, page=0, quote="週2回", record_date="")])
    return tool("report_finding", data | kw)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    folder = root / "利用者" / "U01_杉森ハル"
    folder.mkdir(parents=True)
    (folder / "計画書.txt").write_text(TEXTS[FILE], encoding="utf-8")
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    c = connect(s)
    upsert_user(c, User(id="U01", name="杉森 ハル", cm="CM01", data={"type": "居宅介護支援", "cert_period": ["2026-04-01", "2028-03-31"]}))
    _save(c, scan(s)[0], DocFacts(doc_type="居宅サービス計画書", summary="計画書"))
    yield s, c
    c.close()


class Scripted(FakeClient):
    def _stream(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        return FakeStream(self.results.pop(0))


def test_reported_finding_comes_back_marked_as_claude(env):
    s, c = env
    run = run_claude_checks(c, Scripted(msg(report()), END), s, "U01")
    assert run.not_run is False and len(run.findings) == 1
    assert run.findings[0].source == "claude" and run.findings[0].user_id == "U01"


def test_invented_finding_is_recorded_as_dropped(env):
    s, c = env
    bad = report(evidence=[dict(file=REL, page=0, quote="週5回", record_date="")])
    run = run_claude_checks(c, Scripted(msg(bad), END), s, "U01")
    assert run.findings == [] and len(run.dropped) == 1


def test_rule_not_for_claude_is_dropped(env):
    s, c = env
    run = run_claude_checks(c, Scripted(msg(report(rule_id="R99")), END), s, "U01")
    assert run.findings == [] and len(run.dropped) == 1


def test_read_document_returns_file_text(env):
    s, c = env
    client = Scripted(msg(tool("read_document", {"file": REL})), END)
    run_claude_checks(c, client, s, "U01")
    sent = str(client.calls[1]["messages"][-1]["content"])
    assert "週2回" in sent and "tool_result" in sent


def test_read_document_outside_root_is_refused(env):
    s, c = env
    client = Scripted(msg(tool("read_document", {"file": "../answer_key.csv"})), END)
    run_claude_checks(c, client, s, "U01")
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True


def test_read_document_of_other_user_is_refused(env):
    s, c = env
    other = s.drive_root / "利用者" / "U02_別人"
    other.mkdir()
    (other / "秘密.txt").write_text("他の人", encoding="utf-8")
    client = Scripted(msg(tool("read_document", {"file": "利用者/U02_別人/秘密.txt"})), END)
    run_claude_checks(c, client, s, "U01")
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_lookup_ledger_finds_person(env):
    s, c = env
    client = Scripted(msg(tool("lookup_ledger", {"name": "杉森"})), END)
    run_claude_checks(c, client, s, "U01")
    assert "杉森 ハル" in str(client.calls[1]["messages"][-1]["content"])


def test_api_failure_marks_check_not_run(env):
    s, c = env
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    run = run_claude_checks(c, FakeClient(err), s, "U01")
    assert run.not_run is True and run.findings == [] and run.error


def test_refusal_marks_check_not_run(env):
    s, c = env
    run = run_claude_checks(c, Scripted(msg(stop_reason="refusal")), s, "U01")
    assert run.not_run is True


def test_max_tokens_marks_check_not_run(env):
    s, c = env
    run = run_claude_checks(c, Scripted(msg(stop_reason="max_tokens")), s, "U01")
    assert run.not_run is True


def test_loop_has_a_limit(env):
    s, c = env
    client = Scripted(*[msg(tool("lookup_ledger", {"name": "杉森"})) for _ in range(60)])
    assert run_claude_checks(c, client, s, "U01").not_run is True


def test_records_and_facts_are_in_first_message_and_system_is_cached(env):
    s, c = env
    add_record(c, RecordIn(user_id="U01", event_date="2026-10-20", body="週3回に増やす話が出た。"))
    client = Scripted(END)
    run_claude_checks(c, client, s, "U01")
    kw = client.calls[0]
    text = str(kw["messages"][0]["content"])
    assert "週3回に増やす話が出た。" in text and "計画書.txt" in text
    assert kw["system"][-1]["cache_control"]["type"] == "ephemeral"
    assert kw["model"] == "claude-opus-5-5" and kw["thinking"] == {"type": "adaptive"}


def test_machine_candidate_can_be_dismissed_with_record_evidence(env):
    s, c = env
    add_record(c, RecordIn(user_id="U01", event_date="2026-08-05", kind_read="居宅訪問", body="訪問。"))
    add_record(c, RecordIn(user_id="U01", event_date="2026-09-10", kind_read="電話", kind_written="電話", body="本人は入院中のため訪問できず。"))
    add_record(c, RecordIn(user_id="U01", event_date="2026-10-05", kind_read="居宅訪問", body="訪問。"))
    candidates = [x for x in run_machine_checks(c, s, "U01") if x.rule_id == "R06" and x.kind == "finding"]
    assert [x.period for x in candidates] == ["2026-09"]
    dismiss = tool("dismiss_candidate", dict(rule_id="R06", period="2026-09", reason="入院中", record_date="2026-09-10", quote="入院中のため訪問できず"))
    run = run_claude_checks(c, Scripted(msg(dismiss), END), s, "U01")
    assert [(d.rule_id, d.period, d.reason) for d in run.dismissed] == [("R06", "2026-09", "入院中")]


def test_dismiss_with_invented_quote_is_ignored(env):
    s, c = env
    add_record(c, RecordIn(user_id="U01", event_date="2026-09-10", kind_read="電話", body="電話した。"))
    dismiss = tool("dismiss_candidate", dict(rule_id="R06", period="2026-09", reason="入院中", record_date="2026-09-10", quote="入院中"))
    run = run_claude_checks(c, Scripted(msg(dismiss), END), s, "U01")
    assert run.dismissed == []


def test_source_texts_include_records_and_documents(env):
    s, c = env
    add_record(c, RecordIn(user_id="U01", event_date="2026-09-10", body="手入力の記録。"))
    texts = source_texts(c, s, "U01")
    assert "週2回" in texts[REL] and "手入力の記録。" in texts["支援経過（アプリの記録）"]


def test_skills_have_no_sample_answers():
    text = load_skills()
    assert "R01" in text and "dismiss_candidate" in text
    for word in ("杉森", "柚木", "綿貫", "戸叶", "U03", "H02", "T01", "こなら坂"):
        assert word not in text


def test_run_is_saved_and_latest_is_returned(env):
    s, c = env
    save_run(c, "U01", CheckRun(findings=[f()], dismissed=[Dismissal("R06", "2026-09", "入院中", "2026-09-10", "入院")], cost=0.5))
    run, run_at = latest_run(c, "U01")
    assert run.findings[0].rule_id == "R03" and run.dismissed[0].reason == "入院中" and run_at


def test_failed_run_is_latest_so_old_results_are_not_shown_silently(env):
    s, c = env
    save_run(c, "U01", CheckRun(findings=[f()]))
    save_run(c, "U01", CheckRun(not_run=True, error="通信"))
    assert latest_run(c, "U01")[0].not_run is True


def test_no_run_yet_returns_none(env):
    assert latest_run(env[1], "U01") is None


def test_consent_candidate_can_be_dismissed_too(env):
    s, c = env
    plan = DocFacts(doc_type="居宅サービス計画書", period_start="2026-08-01", period_end="2027-07-31", consent_date="2026-08-05")
    _save(c, scan(s)[0], plan)
    add_record(c, RecordIn(user_id="U01", event_date="2026-08-05", body="本日説明し同意を得た。サービス開始は8/6から。"))
    dismiss = tool("dismiss_candidate", dict(rule_id="R10", period="2026-08-01", reason="開始は同意の後", record_date="2026-08-05", quote="サービス開始は8/6から"))
    assert run_claude_checks(c, Scripted(msg(dismiss), END), s, "U01").dismissed[0].rule_id == "R10"


def test_dismiss_of_non_candidate_is_refused(env):
    s, c = env
    add_record(c, RecordIn(user_id="U01", event_date="2026-09-10", kind_read="居宅訪問", body="入院中。"))
    dismiss = tool("dismiss_candidate", dict(rule_id="R06", period="2026-09", reason="入院中", record_date="2026-09-10", quote="入院中"))
    run = run_claude_checks(c, Scripted(msg(dismiss), END), s, "U01")
    assert run.dismissed == [] and run.dropped[0]["why"] == "候補に無い"


def test_confirmed_findings_are_told_to_claude(env):
    s, c = env
    from keika.responses import respond
    respond(c, s, f(message="人が問題なしとした指摘です。確認してください。"), "off_target")
    client = Scripted(END)
    run_claude_checks(c, client, s, "U01")
    assert "人が問題なしとした指摘です" in str(client.calls[0]["messages"][0]["content"])


def test_suggestion_rule_forbids_invented_actions():
    from keika.claude_checks import COMMON
    assert "記録や書類に無い行動" in COMMON


def test_record_quote_under_wrong_file_name_is_pointed_to_the_real_source(env):
    # Claude がファイル名を取り違えても、日付と語句で記録が特定できれば本来の出どころに直す
    s, c = env
    (s.drive_root / "利用者" / "U01_杉森ハル" / "経過.csv").write_text("日付,本文\n2026-09-12,夜中に転んだ。\n", encoding="utf-8")
    add_record(c, RecordIn(user_id="U01", event_date="2026-09-12", body="夜中に転んだ。", source="csv:利用者/U01_杉森ハル/経過.csv"))
    e = dict(file="支援経過（アプリの記録）", page=0, quote="夜中に転んだ", record_date="2026-09-12")
    run = run_claude_checks(c, Scripted(msg(report(rule_id="R11", evidence=[e])), END), s, "U01")
    assert run.dropped == []
    assert run.findings[0].evidence[0].file == "利用者/U01_杉森ハル/経過.csv"


def test_record_quote_not_in_that_date_is_still_dropped(env):
    s, c = env
    add_record(c, RecordIn(user_id="U01", event_date="2026-09-12", body="夜中に転んだ。", source="csv:利用者/U01_杉森ハル/経過.csv"))
    e = dict(file="支援経過（アプリの記録）", page=0, quote="作り話", record_date="2026-09-12")
    run = run_claude_checks(c, Scripted(msg(report(rule_id="R11", evidence=[e])), END), s, "U01")
    assert run.findings == [] and len(run.dropped) == 1


# --- 記録を読んで AI が指摘を閉じる ---


def _prior(c):
    save_run(c, "U01", CheckRun(findings=[f(rule_id="R11", message="転倒後の見直しを確認してください。")]))


def test_prior_findings_are_told_to_claude(env):
    s, c = env
    _prior(c)
    client = Scripted(END)
    run_claude_checks(c, client, s, "U01")
    assert "転倒後の見直しを確認してください。" in str(client.calls[0]["messages"][0]["content"])
    assert "P1" in str(client.calls[0]["messages"][0]["content"])


def test_prior_finding_closes_when_record_shows_the_response(env):
    s, c = env
    _prior(c)
    add_record(c, RecordIn(user_id="U01", event_date="2026-11-04", body="担当者会議を開き計画を見直した。"))
    close = tool("resolve_finding", dict(prior_id="P1", reason="見直しの記録あり", evidence=[dict(file="支援経過（アプリの記録）", page=0, quote="計画を見直した", record_date="2026-11-04")]))
    run = run_claude_checks(c, Scripted(msg(close), END), s, "U01")
    assert run.findings == []
    assert [(r.rule_id, r.reason) for r in run.resolved] == [("R11", "見直しの記録あり")]
    assert "転倒後の見直し" in run.resolved[0].message and "計画を見直した" in run.resolved[0].quote


def test_close_with_invented_evidence_is_refused_and_finding_stays(env):
    s, c = env
    _prior(c)
    close = tool("resolve_finding", dict(prior_id="P1", reason="見直した", evidence=[dict(file="支援経過（アプリの記録）", page=0, quote="作り話", record_date="2026-11-04")]))
    run = run_claude_checks(c, Scripted(msg(close), END), s, "U01")
    assert run.resolved == [] and [x.rule_id for x in run.findings] == ["R11"] and len(run.dropped) == 1


def test_prior_finding_not_mentioned_is_carried_over_not_lost(env):
    s, c = env
    _prior(c)
    run = run_claude_checks(c, Scripted(END), s, "U01")
    assert [x.rule_id for x in run.findings] == ["R11"]


def test_prior_finding_reported_again_is_not_doubled(env):
    s, c = env
    _prior(c)
    again = report(rule_id="R11", message="転倒後の見直しを確かめてください。確認してください。")
    run = run_claude_checks(c, Scripted(msg(again), END), s, "U01")
    assert len(run.findings) == 1


def test_finding_silenced_by_person_is_not_carried_over(env):
    s, c = env
    from keika.responses import respond
    _prior(c)
    respond(c, s, f(rule_id="R11", message="転倒後の見直しを確認してください。"), "off_target")
    assert run_claude_checks(c, Scripted(END), s, "U01").findings == []


def test_failed_run_keeps_prior_findings(env):
    s, c = env
    _prior(c)
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    run = run_claude_checks(c, FakeClient(err), s, "U01")
    assert run.not_run is True and [x.rule_id for x in run.findings] == ["R11"]


def test_resolution_is_saved_and_loaded(env):
    s, c = env
    save_run(c, "U01", CheckRun(resolved=[Resolution("R11", "2026-10", "転倒", "見直した", "2026-11-04", "見直した")]))
    assert latest_run(c, "U01")[0].resolved[0].reason == "見直した"


def _progress(**kw):
    data = dict(step="delivery", status="done", date="2026-08-03", note="", file=REL, quote="週2回")
    return tool("report_progress", {"marks": [data | kw]})


def test_progress_is_kept_with_evidence(env):
    s, c = env
    run = run_claude_checks(c, Scripted(msg(_progress()), END), s, "U01")
    assert run.progress[0].status == "done" and run.progress[0].step == "delivery"


def test_progress_done_without_real_quote_becomes_unknown(env):
    s, c = env
    run = run_claude_checks(c, Scripted(msg(_progress(quote="作り話")), END), s, "U01")
    assert run.progress[0].status == "unknown" and len(run.dropped) == 1


def test_progress_todo_needs_no_quote(env):
    s, c = env
    run = run_claude_checks(c, Scripted(msg(_progress(status="todo", quote="", file="")), END), s, "U01")
    assert run.progress[0].status == "todo"


def test_time_limit_marks_check_not_run(env, monkeypatch):
    from keika import claude_checks
    s, c = env
    _prior(c)
    monkeypatch.setattr(claude_checks, "TIME_LIMIT_SECONDS", -1)
    client = Scripted(msg(tool("lookup_ledger", {"name": "杉森"})), msg(tool("lookup_ledger", {"name": "杉森"})), END)
    run = run_claude_checks(c, client, s, "U01")
    assert run.not_run is True and "時間" in run.error and [x.rule_id for x in run.findings] == ["R11"]
