"""独立レビューの指摘の再現試験。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika import files as files_mod
from keika.claude_checks import CheckRun, Dismissal, latest_run, run_claude_checks, save_run, validate_finding
from keika.db import connect
from keika.files import scan, to_payload
from keika.findings import Evidence, Finding
from keika.intake import WireEntry, WireFix, WireIntake, open_reviews, process_text, resolve_review
from keika.ledger import User, seed_from_json, upsert_user
from keika.machine_checks import user_refs
from keika.reading import DocFacts, _save, read_doc
from keika.records import RecordIn, add_record, edit_record, import_csv, list_records, normalize_date, WireCsv
from keika.responses import respond, silenced_for_prompt
from keika.settings import Settings
from keika.watcher import handle_file, start_watcher
from keika.web.app import create_app
from test_claude_checks import END, REL, Scripted, msg, report, tool
from test_reading import FakeClient, answer


class RemoteProtocolError(Exception):
    """SDK が包まずに投げる通信切れの形。"""


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者" / "U01_杉森ハル").mkdir(parents=True)
    (root / "入口").mkdir()
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    (root / "利用者" / "U01_杉森ハル" / "計画書.txt").write_text("週2回 通所介護 を利用する。転倒に注意する。", encoding="utf-8")
    _save(c, next(r for r in scan(s) if r.scope == "user"), DocFacts(doc_type="居宅サービス計画書", summary="計画書"))
    yield s, c
    c.close()


def entry(**kw):
    data = dict(user_id="U04", spoken_name="柚木さん", ambiguous=False, ambiguity_note="", event_date="2026-11-04", event_time="", kind_read="居宅訪問",
                monitoring=True, partner="本人", body="訪問した。", name_fixes=[], unsure_words=[])
    return WireEntry(**(data | kw))


def intake(s, c, text, *entries, **kw):
    return process_text(c, FakeClient(answer(WireIntake(entries=list(entries)))), s, text, "test", **kw)


# 1. 接続の共有


def test_parallel_requests_do_not_fail(env):
    s, c = env
    client = TestClient(create_app(s), raise_server_exceptions=False)
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(lambda i: client.get("/users/U01" if i % 2 else "/ledger").status_code, range(60)))
    assert set(codes) == {200}


def test_parallel_edits_all_keep_history(env):
    s, c = env
    r = add_record(c, RecordIn(user_id="U01", event_date="2026-11-01", body="もと"))
    client = TestClient(create_app(s), raise_server_exceptions=False)
    data = dict(user_id="U01", event_date="2026-11-01", event_time="", kind_written="", kind_read="", partner="", author="")

    def edit(i):
        return client.post(f"/records/{r.id}/edit", data=data | {"body": f"版{i}"}, follow_redirects=False).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert set(pool.map(edit, range(30))) == {303}
    from keika.records import get_record, history
    assert get_record(c, r.id).body == history(c, r.id)[-1].body


# 2. 通信切れ


def test_stream_cut_keeps_dictation_in_review_box(env):
    s, c = env
    results = process_text(c, FakeClient(RemoteProtocolError("切れた")), s, "大事な話。", "test")
    assert results[0].review_id and open_reviews(c)[0]["original"] == "大事な話。"


def test_stream_cut_marks_check_not_run(env):
    s, c = env
    run = run_claude_checks(c, FakeClient(RemoteProtocolError("切れた")), s, "U01")
    assert run.not_run is True


def test_watcher_survives_any_error(env):
    s, c = env
    calls = []

    def factory():
        calls.append(1)
        raise RemoteProtocolError("切れた")

    stop = start_watcher(s, factory, interval=0.01)
    import time
    time.sleep(0.2)
    stop.set()
    assert len(calls) >= 2


# 3. 開けないファイル


def test_locked_file_does_not_stop_scan(env, monkeypatch):
    s, c = env
    (s.drive_root / "利用者" / "U01_杉森ハル" / "ロック.xlsx").write_text("x", encoding="utf-8")
    real = files_mod._hash

    def locked(path):
        if path.name == "ロック.xlsx":
            raise PermissionError("使用中")
        return real(path)

    monkeypatch.setattr(files_mod, "_hash", locked)
    refs = {Path(r.rel_path).name: r for r in scan(s)}
    assert refs["ロック.xlsx"].sha256.startswith("unreadable")
    assert TestClient(create_app(s)).get("/users/U01").status_code == 200


def test_locked_file_result_is_not_saved(env, monkeypatch):
    s, c = env
    (s.drive_root / "利用者" / "U01_杉森ハル" / "ロック.txt").write_text("x", encoding="utf-8")
    ref = next(r for r in scan(s) if r.rel_path.endswith("ロック.txt"))
    locked = ref.__class__(ref.rel_path, "unreadable:" + ref.rel_path, ref.ext, ref.scope, ref.user_folder)
    assert read_doc(c, FakeClient(ValueError("x")), s, locked).readable is False
    assert c.execute("SELECT 1 FROM doc_facts WHERE sha256 = ?", (locked.sha256,)).fetchone() is None


def test_broken_image_is_unreadable(env):
    s, c = env
    (s.drive_root / "入口" / "bad.png").write_bytes(b"not an image")
    assert to_payload(s, next(r for r in scan(s) if r.ext == "png")).unreadable


# 4. 根拠の捏造


TEXTS = {"f.txt": "週2回 通所介護 を利用する。"}


def f(**kw):
    return Finding(**(dict(rule_id="R03", user_id="U01", kind="finding", message="確認してください。", evidence=[Evidence(file="f.txt", quote="週2回")]) | kw))


def test_blank_quote_does_not_pass():
    assert validate_finding(f(evidence=[Evidence(file="f.txt", quote=" ")]), TEXTS) is None


def test_one_character_quote_does_not_pass():
    assert validate_finding(f(evidence=[Evidence(file="f.txt", quote="を")]), TEXTS) is None


def test_invented_quote_with_scope_is_dropped_not_kept():
    bad = f(evidence=[Evidence(file="f.txt", quote="作り話です")], searched_scope="全部探した")
    assert validate_finding(bad, TEXTS) is None


def test_quote_in_scan_is_marked_unverified():
    out = validate_finding(f(evidence=[Evidence(file="s.pdf", quote="署名欄")]), {"s.pdf": None})
    assert out.evidence[0].unverified is True
    assert validate_finding(f(), TEXTS).evidence[0].unverified is False


def _prior(c):
    save_run(c, "U01", CheckRun(findings=[f(rule_id="R11", message="転倒後の見直しを確認してください。", period="2026-10")], last_record_id=99))


def _close(quote, day, file="支援経過（アプリの記録）"):
    return tool("resolve_finding", dict(prior_id="P1", reason="対応あり", evidence=[dict(file=file, page=0, quote=quote, record_date=day)]))


def test_old_record_cannot_close_a_finding(env):
    s, c = env
    add_record(c, RecordIn(user_id="U01", event_date="2026-08-20", body="自宅で話を聞いた。"))
    _prior(c)  # 記録の後に点検が走った
    run = run_claude_checks(c, Scripted(msg(_close("自宅で話を聞いた", "2026-08-20")), END), s, "U01")
    assert run.resolved == [] and [x.rule_id for x in run.findings] == ["R11"]


def test_new_record_can_close_a_finding(env):
    s, c = env
    save_run(c, "U01", CheckRun(findings=[f(rule_id="R11", message="見直しを確認してください。", period="2026-10")], last_record_id=0))
    add_record(c, RecordIn(user_id="U01", event_date="2026-11-04", body="担当者会議で計画を見直した。"))
    run = run_claude_checks(c, Scripted(msg(_close("計画を見直した", "2026-11-04")), END), s, "U01")
    assert len(run.resolved) == 1


def test_unchanged_document_cannot_close_a_finding(env):
    s, c = env
    run0 = CheckRun(findings=[f(rule_id="R11", message="見直しを確認してください。", period="2026-10")], file_hashes={REL: scan(s)[0].sha256})
    save_run(c, "U01", run0)
    run = run_claude_checks(c, Scripted(msg(_close("週2回 通所介護", "", REL)), END), s, "U01")
    assert run.resolved == []


def test_replaces_with_other_rule_does_not_erase_prior(env):
    s, c = env
    _prior(c)
    other = report(rule_id="R03", replaces="P1", message="関係ない指摘です。確認してください。")
    run = run_claude_checks(c, Scripted(msg(other), END), s, "U01")
    assert sorted(x.rule_id for x in run.findings) == ["R03", "R11"]


def test_wrong_record_date_is_corrected_to_the_real_one(env):
    s, c = env
    add_record(c, RecordIn(user_id="U01", event_date="2026-09-05", body="本人に電話した。"))
    e = dict(file="支援経過（アプリの記録）", page=0, quote="本人に電話した", record_date="2026-10-20")
    run = run_claude_checks(c, Scripted(msg(report(evidence=[e])), END), s, "U01")
    assert run.findings[0].evidence[0].record_date == "2026-09-05"


def _dismiss(quote, day):
    return tool("dismiss_candidate", dict(rule_id="R06", period="2026-09", reason="入院中", record_date=day, quote=quote))


def _sept_candidate(c):
    add_record(c, RecordIn(user_id="U01", event_date="2026-08-05", kind_read="居宅訪問", body="訪問。"))
    add_record(c, RecordIn(user_id="U01", event_date="2026-09-10", kind_read="電話", body="本人は入院中のため訪問できず。"))
    add_record(c, RecordIn(user_id="U01", event_date="2026-10-05", kind_read="居宅訪問", body="訪問。"))


def test_short_quote_cannot_dismiss(env):
    s, c = env
    _sept_candidate(c)
    for quote in (" ", "に", "本人"):
        assert run_claude_checks(c, Scripted(msg(_dismiss(quote, "2026-09-10")), END), s, "U01").dismissed == []


def test_record_far_from_the_month_cannot_dismiss(env):
    s, c = env
    _sept_candidate(c)
    add_record(c, RecordIn(user_id="U01", event_date="2026-02-01", kind_read="電話", body="入院中のため訪問できず。"))
    assert run_claude_checks(c, Scripted(msg(_dismiss("入院中のため訪問できず", "2026-02-01")), END), s, "U01").dismissed == []


def test_progress_done_with_unknown_file_is_unknown(env):
    s, c = env
    add_record(c, RecordIn(user_id="U01", event_date="2026-09-05", body="計画書を交付した。"))
    mark = dict(step="delivery", status="done", date="", note="", file="無い.pdf", quote="計画書を交付した")
    run = run_claude_checks(c, Scripted(msg(tool("report_progress", {"marks": [mark]})), END), s, "U01")
    assert run.progress[0].status == "unknown"


def test_progress_na_only_for_medical(env):
    s, c = env
    mark = dict(step="delivery", status="na", date="", note="", file="", quote="")
    run = run_claude_checks(c, Scripted(msg(tool("report_progress", {"marks": [mark]})), END), s, "U01")
    assert run.progress[0].status == "unknown"


# 5. 振り分け


@pytest.mark.parametrize("spoken", ["鈴木さん", "", "おばあちゃん", "杉森さんの奥さん"])
def test_name_not_matching_the_user_goes_to_review_box(env, spoken):
    s, c = env
    assert intake(s, c, "訪問した。", entry(spoken_name=spoken))[0].review_id


def test_family_member_of_that_user_is_accepted(env):
    s, c = env
    assert intake(s, c, "房江さんから電話。", entry(spoken_name="房江さん", kind_read="電話", body="房江さんから電話。"))[0].record


def test_staff_name_goes_to_review_box(env):
    s, c = env
    assert intake(s, c, "井波さんの話。", entry(spoken_name="井波さん", body="井波さんの話。"))[0].review_id


def test_misheard_name_listed_in_fixes_is_accepted(env):
    s, c = env
    fix = [WireFix(heard="ゆの木", fixed="柚木 茂")]
    assert intake(s, c, "ゆの木さん宅を訪問した。", entry(spoken_name="ゆの木さん", name_fixes=fix))[0].record


def test_misheard_name_not_listed_goes_to_review_box(env):
    s, c = env
    assert intake(s, c, "ゆの木さん宅を訪問した。", entry(spoken_name="ゆの木さん"))[0].review_id


def test_two_callings_in_spoken_name_are_accepted(env):
    s, c = env
    assert intake(s, c, "わたぬきさん宅を訪問した。", entry(user_id="U03", spoken_name="わたぬきさん／キヨさん"))[0].record


def test_no_entries_goes_to_review_box(env):
    s, c = env
    results = intake(s, c, "大事な話。")
    assert results[0].review_id and open_reviews(c)[0]["original"] == "大事な話。"


def test_original_text_is_kept_with_saved_record(env):
    s, c = env
    rid = intake(s, c, "柚木さん宅を訪問した。", entry())[0].record.id
    assert "柚木さん宅を訪問した。" in TestClient(create_app(s)).get(f"/records/{rid}/history").text


def test_review_cannot_be_saved_twice(env):
    s, c = env
    rid = intake(s, c, "田中さん。", entry(user_id=""))[0].review_id
    resolve_review(c, rid, user_id="U04", s=s)
    with pytest.raises(ValueError):
        resolve_review(c, rid, user_id="U04", s=s)


def test_no_api_key_keeps_dictation_in_review_box(env):
    s, c = env

    def no_key():
        raise RuntimeError("API キーがありません")

    r = TestClient(create_app(s, client_factory=no_key)).post("/records/new", data={"text": "大事な話。", "mode": "shape"})
    assert r.status_code == 200 and open_reviews(c)[0]["original"] == "大事な話。"


# 6. 同姓同名


def test_same_name_users_do_not_share_folders(env):
    s, c = env
    (s.drive_root / "利用者" / "U05_田中一郎").mkdir()
    (s.drive_root / "利用者" / "U05_田中一郎" / "a.txt").write_text("x", encoding="utf-8")
    (s.drive_root / "利用者" / "U06_田中一郎").mkdir()
    (s.drive_root / "利用者" / "U06_田中一郎" / "b.txt").write_text("x", encoding="utf-8")
    upsert_user(c, User(id="U05", name="田中 一郎"))
    upsert_user(c, User(id="U06", name="田中 一郎"))
    assert [Path(r.rel_path).name for r in user_refs(c, s, User(id="U05", name="田中 一郎"))] == ["a.txt"]


# 7. 日付


@pytest.mark.parametrize("given,want", [("2026/10/05", "2026-10-05"), ("2026-1-5", "2026-01-05"), ("2026.10.5", "2026-10-05"), ("令和8年10月5日", "2026-10-05"), ("R8.10.5", "2026-10-05"), ("2026-10-05", "2026-10-05")])
def test_normalize_date(given, want):
    assert normalize_date(given) == want


@pytest.mark.parametrize("given", ["10/05", "そのうち", "2026-13-40"])
def test_normalize_date_refuses_what_it_cannot_read(given):
    assert normalize_date(given) is None


def test_csv_dates_are_normalized_and_bad_rows_reported(env):
    s, c = env
    path = s.drive_root / "利用者" / "U01_杉森ハル" / "経過.csv"
    path.write_text("日付,本文\n2026/10/05,電話した\n10/05,読めない日付\n", encoding="utf-8")
    upsert_user(c, User(id="U09", name="杉森 ハル"))
    ref = next(r for r in scan(s) if r.ext == "csv")
    wire = WireCsv(date_column="日付", time_column="", kind_column="", partner_column="", body_column="本文", author_column="", rows=[])
    result = import_csv(c, FakeClient(answer(wire)), s, ref)
    assert result.bad_dates == 1 or result.error
    rows = [r for u in ("U01", "U09") for r in list_records(c, u)]
    assert all(r.event_date == "2026-10-05" for r in rows)


def test_add_record_refuses_bad_date(env):
    s, c = env
    with pytest.raises(ValueError):
        add_record(c, RecordIn(user_id="U01", event_date="10/05", body="x"))


def test_edit_screen_refuses_bad_date_and_unknown_user(env):
    s, c = env
    r = add_record(c, RecordIn(user_id="U01", event_date="2026-11-01", body="もと"))
    client = TestClient(create_app(s))
    data = dict(user_id="U01", event_date="2026-11-01", event_time="", kind_written="", kind_read="", partner="", body="x", author="")
    assert client.post(f"/records/{r.id}/edit", data=data | {"event_date": "10/05"}).status_code == 400
    assert client.post(f"/records/{r.id}/edit", data=data | {"user_id": "NOBODY"}).status_code == 400


def test_edit_screen_changes_kind_read(env):
    s, c = env
    r = add_record(c, RecordIn(user_id="U01", event_date="2026-11-01", kind_read="居宅訪問", body="もと"))
    data = dict(user_id="U01", event_date="2026-11-01", event_time="", kind_written="", kind_read="電話", partner="", body="本当は電話", author="")
    TestClient(create_app(s)).post(f"/records/{r.id}/edit", data=data)
    assert list_records(c, "U01")[0].kind_read == "電話"


# 8. 見張りの点検


def test_check_saves_not_run_when_key_is_missing(env):
    from keika.claude_checks import check_and_save
    s, c = env

    def no_key():
        raise RuntimeError("キーなし")

    check_and_save(s, no_key, "U01")
    assert latest_run(c, "U01")[0].not_run is True


def test_serve_gives_watcher_a_check(monkeypatch):
    from keika import cli
    import inspect
    assert "on_saved" in inspect.getsource(cli.serve)


# 9. 同期の途中


def test_file_changing_during_read_is_left_for_later(env, monkeypatch):
    s, c = env
    import os
    import time
    from keika import watcher
    path = s.drive_root / "入口" / "a.txt"
    path.write_text("訪問した。", encoding="utf-8")
    old = time.time() - 60
    os.utime(path, (old, old))
    real_sleep = time.sleep

    def grow(_):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("続き")
        os.utime(path, (old, old))
        real_sleep(0)

    monkeypatch.setattr(watcher.time, "sleep", grow)
    client = FakeClient(answer(WireIntake(entries=[entry()])))
    assert handle_file(c, client, s, path) is False and client.calls == []


# 11. 未実施と状態の引き継ぎ


def test_failed_run_keeps_dismissed(env):
    s, c = env
    save_run(c, "U01", CheckRun(dismissed=[Dismissal("R06", "2026-09", "入院中", "2026-09-10", "入院中のため")]))
    run = run_claude_checks(c, FakeClient(RemoteProtocolError("切れた")), s, "U01")
    assert run.not_run and run.dismissed[0].period == "2026-09"


def test_history_noted_claude_finding_survives_next_run(env):
    s, c = env
    one = f(rule_id="R11", message="転倒の見直しを確認してください。", period="2026-10")
    save_run(c, "U01", CheckRun(findings=[one]))
    rec = add_record(c, RecordIn(user_id="U01", event_date="2026-11-04", body="経緯。"))
    respond(c, s, one, "history_noted", record_id=rec.id)
    run = run_claude_checks(c, Scripted(END), s, "U01")
    assert [x.rule_id for x in run.findings] == ["R11"]


def test_silenced_finding_is_not_told_to_claude_after_evidence_changes(env):
    s, c = env
    one = f(evidence=[Evidence(file=REL, quote="週2回")], message="問題なしとした指摘。確認してください。")
    respond(c, s, one, "off_target")
    assert silenced_for_prompt(c, s, "U01")
    (s.drive_root / REL).write_text("書き換えた", encoding="utf-8")
    assert silenced_for_prompt(c, s, "U01") == []


# 12. 書類の中の指示


def test_reading_and_csv_prompts_refuse_instructions_in_documents():
    from keika.reading import SYSTEM
    from keika.records import CSV_SYSTEM
    assert "指示" in SYSTEM and "指示" in CSV_SYSTEM


# 13. /doc と外からの送信


def test_doc_refuses_html_and_svg(env):
    s, c = env
    (s.drive_root / "利用者" / "evil.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (s.drive_root / "利用者" / "e.svg").write_text("<svg/>", encoding="utf-8")
    client = TestClient(create_app(s))
    assert client.get("/doc", params={"path": "利用者/evil.html"}).status_code == 403
    assert client.get("/doc", params={"path": "利用者/e.svg"}).status_code == 403


def test_doc_text_is_not_inline_html(env):
    s, c = env
    r = TestClient(create_app(s)).get("/doc", params={"path": REL})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_post_from_other_origin_is_refused(env):
    s, c = env
    client = TestClient(create_app(s))
    data = dict(id="", name="改ざん", kana="", cm="", care_level="", cert_start="", cert_end="", type="")
    assert client.post("/ledger", data=data, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/ledger", data=data, headers={"Origin": "http://testserver"}, follow_redirects=False).status_code == 303


def test_unknown_host_is_refused(env):
    s, c = env
    client = TestClient(create_app(s))
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    assert client.get("/", headers={"Host": "192.168.1.20:8000"}).status_code == 200


# 14. CSV の再取り込みが手直しを上書きしない


def test_reimport_does_not_overwrite_human_edit(env):
    s, c = env
    path = s.drive_root / "利用者" / "U01_杉森ハル" / "経過.csv"
    path.write_text("日付,本文\n2026-10-05,CSV本文\n", encoding="utf-8")
    upsert_user(c, User(id="U01", name="杉森 ハル"))
    wire = WireCsv(date_column="日付", time_column="", kind_column="", partner_column="", body_column="本文", author_column="", rows=[])

    def go():
        ref = next(r for r in scan(s) if r.ext == "csv")
        return import_csv(c, FakeClient(answer(wire)), s, ref)

    go()
    rec = list_records(c, "U01")[0]
    edit_record(c, rec.id, {"body": "人が直した本文"}, editor="真柴")
    path.write_text("日付,本文\n2026-10-05,CSVが変わった\n", encoding="utf-8")
    result = go()
    assert list_records(c, "U01")[0].body == "人が直した本文"
    assert result.conflicts == [rec.id]


# 15. 医療の線引き、過去の日付


@pytest.mark.parametrize("bad", ["受診をお勧めします。確認してください。", "減薬を検討してください。確認してください。", "受診させてください。確認してください。", "認知症の疑いがあります。確認してください。"])
def test_medical_messages_are_dropped(bad):
    assert validate_finding(f(message=bad), TEXTS) is None


def test_medical_todo_is_dropped():
    assert validate_finding(f(todo="薬を中止するよう伝える"), TEXTS) is None


def test_backdating_by_month_is_dropped():
    assert validate_finding(f(suggestion="10月の日付で訪問した旨を書く。"), TEXTS).suggestion is None

