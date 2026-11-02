"""薬の名前と量は話のとおり。病名の推測、緊急度、受診の要否の判定をしない。

Claude の指示に書くだけでなく、線を越えた文章を機械でも止めて人に回す。実物の Claude での確認は eval/run_boundary.py。
"""
from test_intake import entry, env, run  # noqa: F401

from keika.claude_checks import COMMON, _MEDICAL
from keika.intake import SYSTEM, invented_terms, open_reviews
from keika.records import list_records


def test_prompt_states_the_line():
    for word in ("薬の名前と量は話のとおり", "聞き間違い", "unsure_words", "病名を推測しない", "緊急度", "受診の要否"):
        assert word in SYSTEM, word
    assert "病名を推測しない" in COMMON and "緊急度" in COMMON


def test_corrected_drug_name_is_caught():
    spoken = "朝の薬はアムロジンピンを1錠飲んでいるとのこと"
    assert "アムロジピン" in " ".join(invented_terms(spoken, "朝はアムロジピンを1錠内服しているとのこと。"))


def test_drug_name_kept_as_spoken_passes():
    spoken = "朝の薬はアムロジンピンを1錠飲んでいるとのこと"
    assert invented_terms(spoken, "朝はアムロジンピンを1錠内服しているとのこと。") == []


def test_kana_spoken_drug_name_written_in_katakana_passes():
    assert invented_terms("薬はあむろじんぴんです", "薬はアムロジンピンとのこと。") == []


def test_katakana_outside_medicine_sentence_is_not_flagged():
    assert invented_terms("デイに行った", "デイサービスに通所した。") == []


def test_changed_dose_is_caught():
    assert "2" in invented_terms("薬は1錠", "薬は2錠とのこと。")


def test_disease_guessed_from_drug_is_caught():
    found = invented_terms("血圧の薬を飲んでいる", "高血圧症のため血圧の薬を内服している。")
    assert "高血圧症" in found


def test_disease_that_was_spoken_passes():
    assert invented_terms("糖尿病の薬を飲んでいる", "糖尿病の薬を内服している。") == []


def test_hospital_is_not_a_disease_word():
    assert invented_terms("総合病院へ行った", "総合病院を受診した。") == []


def test_urgency_judgement_is_caught():
    found = invented_terms("息が苦しいと言っていた", "息苦しさの訴えあり。すぐに受診が必要。")
    assert any("受診" in w or "すぐ" in w for w in found)


def test_spoken_plan_to_go_to_hospital_passes():
    assert invented_terms("息が苦しいと言うので家族が病院へ連れて行くとのこと", "息苦しさの訴えあり。家族が病院へ連れて行くとのこと。") == []


def test_unsure_word_is_saved_as_spoken_not_stopped(env):  # noqa: F811
    s, c = env
    body = "朝はアムロジンピンを1錠内服しているとのこと。"
    run(s, c, "柚木さん宅を訪問、朝の薬はアムロジンピンを1錠", entry(body=body, unsure_words=["アムロジンピン"]))
    assert list_records(c, "U04")[0].body == body and open_reviews(c) == []


def test_guessing_finding_is_blocked():
    for bad in ("すぐに受診が必要です", "すぐ病院へ行くべきです", "緊急性が高いです", "糖尿病の可能性があります", "認知症の疑いがあります"):
        assert _MEDICAL.search(bad), bad


def test_plain_fact_finding_is_not_blocked():
    for ok in ("主治医への情報提供が記録にあるか確認してください", "血圧の薬を飲み忘れた記録があります"):
        assert not _MEDICAL.search(ok), ok
