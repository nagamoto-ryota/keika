"""独立レビュアー（.claude/agents/boundary-reviewer.md）が再現した抜けの試験。"""
import pytest
from test_intake import entry, env, run  # noqa: F401

from keika.claude_checks import crosses_line, validate_finding
from keika.findings import Evidence, Finding
from keika.intake import invented_terms, open_reviews
from keika.med_names import candidates, decide_keep, open_checks, register_checks
from keika.records import RecordIn, add_record, list_records, mark_checked


@pytest.mark.parametrize("bad", [
    "メトホルミンを中止することを検討してください。", "メトホルミンは減量した方がよいようです。", "レボドパの量を見直してください。", "休薬を検討してください。",
    "受診した方がよいようです。", "早めに病院へ行くよう伝えてください。", "至急主治医に連絡が必要です。", "重篤な状態のようです。",
    "糖尿病かもしれません。", "メトホルミンから糖尿病と思われます。", "パーキンソンの症状のようです。", "認知症が進んでいるようです。",
    "アムロジンピンはアムロジピンの聞き間違いのようです。", "すぐに受診が必要です", "この薬を減らすべきです",
])
def test_finding_words_beyond_the_line_are_stopped(bad):
    assert crosses_line(bad), bad


@pytest.mark.parametrize("ok", [
    "主治医への情報提供が記録にあるか確認してください。", "血圧の薬を飲み忘れた記録があります。", "デイサービスの回数の変更の記録が見当たりません。",
    "通所介護の中止の記録があるか確認してください。", "糖尿病の悪化が心配と計画書にあります。", "10/22に薬が調整されたと記録にあります。",
])
def test_plain_fact_is_not_stopped(ok):
    assert not crosses_line(ok), ok


@pytest.mark.parametrize("spoken,shaped", [
    ("血圧の薬を飲んでいる", "高血圧のため血圧の薬を内服している。"),
    ("薬を飲んでいる", "うつ病の薬を内服している。"),
    ("朝はアムロジンピンを飲んでいる", "朝はアムロジピンを飲んでいる。"),
    ("朝はアムロジンピンを飲んでいる", "アムロジンピン（アムロジピンか）を飲んでいる。"),
    ("メトホルミンとレボドパを飲んでいる", "メトホルミンは中止した方がよい。"),
    ("11月2日に訪問。薬は1錠", "11月2日に訪問。薬は2錠とのこと。"),
    ("薬は1錠", "薬は半錠とのこと。"),
    ("薬は5ミリグラム", "薬は5グラムとのこと。"),
    ("薬を飲めていない", "薬を飲めている。"),
    ("先生が来た", "受診した。"),
    ("お茶を飲んだ", "内服した。"),
    ("胸が痛いと言う", "胸痛の訴えあり。至急病院へ行く必要がある。"),
    ("胸が痛いと言う", "胸痛の訴えあり。重篤な状態。"),
])
def test_shaped_text_beyond_the_talk_is_caught(spoken, shaped):
    assert invented_terms(spoken, shaped), (spoken, shaped)


@pytest.mark.parametrize("spoken,shaped", [
    ("薬は1錠飲めていない", "薬は1錠で、飲めていないとのこと。"),
    ("先生に診てもらった", "受診した。"),
    ("薬を飲んだ", "内服した。"),
    ("11月2日に訪問。薬は朝2錠", "11月2日に訪問した。薬は朝2錠とのこと。"),
])
def test_faithful_shaping_passes(spoken, shaped):
    assert invented_terms(spoken, shaped) == []


def test_unspoken_visit_and_date_are_sent_to_a_person(env):  # noqa: F811
    s, c = env
    run(s, c, "柚木さんの件、変わりなし。", entry(event_date="2026-10-15", event_time="14:30", kind_read="居宅訪問", partner="長女"))
    assert list_records(c, "U04") == []
    assert open_reviews(c)[0]["warnings"]


def test_spoken_visit_on_todays_date_is_saved(env):  # noqa: F811
    s, c = env
    results, _ = run(s, c, "柚木さん宅を訪問。変わりなし。", entry(event_date="2026-11-04", event_time="", kind_read="居宅訪問"))
    assert results[0].record is not None


def test_spoken_month_and_day_are_accepted(env):  # noqa: F811
    s, c = env
    results, _ = run(s, c, "柚木さん宅を11月2日に訪問。変わりなし。", entry(event_date="2026-11-02", kind_read="居宅訪問"))
    assert results[0].record is not None


def test_name_fix_cannot_launder_other_words(env):  # noqa: F811
    from keika.intake import WireFix
    s, c = env
    run(s, c, "柚木さん、薬はアムロジンピン", entry(body="薬はアムロジピン。", name_fixes=[WireFix(heard="アムロジンピン", fixed="アムロジピン")]))
    assert list_records(c, "U04") == []


def test_name_not_in_the_talk_goes_to_a_person(env):  # noqa: F811
    s, c = env
    run(s, c, "杉森さん宅を訪問。腰が痛いとのこと。", entry(spoken_name="柚木さん", body="訪問した。腰が痛いとのこと。"))
    assert list_records(c, "U04") == []


def test_finding_with_guess_in_scope_is_refused():
    from keika.claude_checks import validate_finding
    f = Finding(rule_id="R06", user_id="U04", kind="finding", period="2026-10", message="記録が見当たりません。確認してください。",
                evidence=[], searched_scope="認知症の疑いがあり受診が必要")
    assert validate_finding(f, {}) is None


def _rec(c, body):
    return add_record(c, RecordIn(user_id="U04", event_date="2026-11-02", kind_written="訪問", kind_read="居宅訪問", body=body))


def _drive(env):
    s, c = env
    folder = s.drive_root / "利用者" / "U04_柚木茂"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "お薬情報_2026-10-01.txt").write_text("レボドパ 1錠 デイサービス", encoding="utf-8")
    return s, c


def test_drug_sentence_with_only_nomu_is_checked(env):  # noqa: F811
    s, c = _drive(env)
    register_checks(c, s, _rec(c, "朝昼夕にレボドバを飲んでいる。"), [])
    assert [m.heard for m in open_checks(c, s)] == ["レボドバ"]


def test_partial_match_does_not_silence_a_flagged_word(env):  # noqa: F811
    s, c = _drive(env)
    register_checks(c, s, _rec(c, "レボドを飲んでいる。"), ["レボド"])
    assert [m.heard for m in open_checks(c, s)] == ["レボド"]


def test_kept_word_does_not_become_a_candidate(env):  # noqa: F811
    s, c = env
    r = _rec(c, "薬はアムロジンピン。")
    register_checks(c, s, r, ["アムロジンピン"])
    decide_keep(c, open_checks(c, s)[0].id, editor="")
    mark_checked(c, r.id, editor="")
    assert candidates(c, s, "U04", "アムロジンピソ") == []


def test_non_medicine_words_are_not_candidates(env):  # noqa: F811
    s, c = _drive(env)
    assert candidates(c, s, "U04", "デイサービズ") == []



TALK = "本人より、薬を飲むとふらつくため、もうやめたいとの話があった。"


def _talk_finding(message):
    return Finding(rule_id="R11", user_id="U04", kind="finding", period="2026-11", message=message,
                   evidence=[Evidence(file="支援経過", page=0, quote="もうやめたいとの話があった", record_date="2026-11-02")])


@pytest.mark.parametrize("message", [
    "11/2の記録に、本人から薬を「もうやめたい」との話があります。主治医等への情報提供を検討済みか確認してください。",
    "11/2の記録に、本人から服薬についての話（ふらつく、やめたい）があります。関係者への確認を検討済みか確認してください。",
])
def test_person_wish_in_the_quote_can_be_passed_on(message):
    assert validate_finding(_talk_finding(message), {"支援経過": TALK}) is not None


@pytest.mark.parametrize("message", [
    "本人が薬をやめたいとの話です。薬を中止することを検討してください。",
    "本人は薬をやめたいとのことなので、やめた方がよいようです。",
    "本人が薬を減らしたいとの話です。減らすべきです。",
])
def test_advice_next_to_the_wish_is_still_stopped(message):
    assert validate_finding(_talk_finding(message), {"支援経過": TALK}) is None


def test_wish_not_in_the_quote_is_still_stopped():
    other = _talk_finding("本人が薬をやめたいとの話です。確認してください。").model_copy(
        update={"evidence": [Evidence(file="支援経過", page=0, quote="ふらつくため", record_date="2026-11-02")]})
    assert validate_finding(other, {"支援経過": TALK}) is None


def test_wish_added_by_shaping_is_still_caught():
    assert invented_terms("薬を飲んでいる", "本人は薬をやめたいとのこと。")
