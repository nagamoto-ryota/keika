import sys
from datetime import date
from pathlib import Path

import yaml

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "eval"))

from scoring import load_judge_table, score  # noqa: E402

from keika.findings import Evidence, Finding, fingerprint  # noqa: E402


def item(id="X1", user="U01", kubun="finding", file="利用者/a.pdf", page=2, must=None, **kw):
    return {
        "id": id, "利用者ID": user, "区分": kubun, "judge_kind": kw.pop("judge_kind", "claude"),
        "evidence": [{"file": file, "role": "primary", "page": page}],
        "must_mention": must if must is not None else [{"what": "理由", "any_of": ["週3回", "週三回"]}],
        "needs_inbox": False, **kw,
    }


def finding(user="U01", file="利用者/a.pdf", page=2, message="週3回と書かれています。確認してください。", kind="finding", rule="R03", **kw):
    return Finding(rule_id=rule, user_id=user, kind=kind, period="2026-10", message=message,
                   evidence=[Evidence(file=file, page=page, quote="週3回")], **kw)


def test_hit_when_user_location_and_reason_match():
    s = score([finding()], [item()])
    assert s.hits == ["X1"] and s.misses == []


def test_score_requires_user_and_location_and_reason():
    table = [item()]
    assert score([finding(user="U02")], table).hits == []
    assert score([finding(file="利用者/b.pdf")], table).hits == []
    assert score([finding(file="利用者/別.pdf")], table).hits == []
    f = Finding(rule_id="R03", user_id="U01", kind="finding", period="2026-10", message="確認してください。",
                evidence=[Evidence(file="利用者/a.pdf", page=2, quote="別の語")])
    s = score([f], table)
    assert s.hits == [] and s.reason_review == [("X1", f.message)]


def test_rule_number_alone_is_not_a_hit():
    # O02 と O05 はどちらも同じ項目番号。番号が合うだけでは当たりにしない
    a = item("A", user=None, file="事業所/a.xlsx", page=None, must=[{"what": "x", "any_of": ["委員会"]}])
    b = item("B", user=None, file="事業所/b.xlsx", page=None, must=[{"what": "x", "any_of": ["新規採用"]}])
    f = Finding(rule_id="R25", user_id=None, kind="finding", period="", message="新規採用時の研修を確認してください。",
                evidence=[Evidence(file="事業所/b.xlsx", quote="新規採用")])
    assert score([f], [a, b]).hits == ["B"]


def test_date_evidence_must_match_record_date():
    table = [{**item(), "evidence": [{"file": "利用者/a.csv", "role": "primary", "date": date(2026, 10, 20)}]}]
    good = Finding(rule_id="R06", user_id="U01", kind="finding", period="2026-10", message="週3回",
                   evidence=[Evidence(file="利用者/a.csv", record_date="2026-10-20", quote="電話")])
    bad = good.model_copy(update={"evidence": [Evidence(file="利用者/a.csv", record_date="2026-10-08", quote="電話")]})
    assert score([good], table).hits == ["X1"]
    assert score([bad], table).hits == []


def test_supporting_evidence_alone_is_not_a_hit():
    table = [{**item(), "evidence": [{"file": "利用者/a.pdf", "role": "supporting", "page": 2}]}]
    assert score([finding()], table).hits == []


def test_searched_scope_counts_as_location_for_missing_documents():
    table = [item(file="利用者/U01_x/同意書.pdf", page=None)]
    f = Finding(rule_id="R21", user_id="U01", kind="finding", period="", message="週3回",
                searched_scope="利用者/U01_x/ を探しました")
    assert score([f], table).hits == ["X1"]


def test_unknown_is_counted_separately():
    s = score([finding(kind="unknown")], [item()])
    assert s.hits == [] and s.false_positives == [] and len(s.unknowns) == 1 and s.misses == ["X1"]


def test_assertion_on_o04_is_listed_as_error():
    table = [item("O04", user=None, kubun="unknown", file="事業所/掲示.pdf", page=None)]
    f = Finding(rule_id="R27", user_id=None, kind="finding", period="", message="週3回", evidence=[Evidence(file="事業所/掲示.pdf", quote="掲示")])
    s = score([f], table)
    assert s.unknown_item_errors == ["O04"] and s.hits == []
    ok = f.model_copy(update={"kind": "unknown"})
    assert score([ok], table).unknown_item_correct == ["O04"]


def test_silent_item_flagged_is_false_positive():
    table = [item("T1", kubun="silent")]
    s = score([finding()], table)
    assert s.false_positives == ["T1"] and s.hits == []


def test_silent_item_not_flagged_is_quiet():
    assert score([], [item("T1", kubun="silent")]).silent_ok == ["T1"]


def test_silent_ok_if_goes_to_review_not_error():
    table = [item("T1", kubun="silent", ok_if=["別の理由なら可"])]
    s = score([finding()], table)
    assert s.false_positives == [] and s.silent_review == ["T1"]


def test_unlisted_findings_are_listed_for_review():
    f = finding(user="U05", file="利用者/z.pdf")
    s = score([f], [item()])
    assert s.unlisted == [f]


def test_nudge_and_finding_scored_separately():
    table = [item("N1", kubun="nudge"), item("X1", file="利用者/b.pdf")]
    n = finding(kind="nudge")
    s = score([n], table)
    assert s.nudge_hits == ["N1"] and s.hits == [] and s.misses == ["X1"]


def test_finding_does_not_hit_a_nudge_item():
    s = score([finding()], [item("N1", kubun="nudge")])
    assert s.nudge_hits == []


def test_intake_items_are_not_scored_here():
    s = score([], [item("T4", kubun="intake")])
    assert s.misses == [] and s.silent_ok == []


def test_inbox_only_items_skipped_before_inbox():
    table = [item("I1", needs_inbox=True)]
    assert score([], table, with_inbox=False).misses == []
    assert score([], table, with_inbox=True).misses == ["I1"]


def test_evidence_after_inbox_only_counts_with_inbox():
    table = [{**item("I1", file="利用者/a.pdf"), "evidence_after_inbox": [{"file": "入口/口述.txt", "quote": "週3回"}]}]
    f = Finding(rule_id="R03", user_id="U01", kind="finding", period="", message="週3回", evidence=[Evidence(file="入口/口述.txt", quote="週3回")])
    assert score([f], table, with_inbox=False).hits == []
    assert score([f], table, with_inbox=True).hits == ["I1"]


def test_heads_split_by_judge_kind():
    table = [item("M1", judge_kind="machine"), item("C1", file="利用者/b.pdf")]
    s = score([finding()], table)
    assert s.by_kind == {"machine": {"hit": 1, "total": 1}, "claude": {"hit": 0, "total": 1}}


def test_findings_per_user_is_reported():
    s = score([finding(), finding(file="利用者/b.pdf")], [item()])
    assert s.per_user == {"U01": 2}


def test_user_list_item_matches_any_user():
    table = [item(user=["U01", "U02"])]
    assert score([finding(user="U02")], table).hits == ["X1"]


def test_real_judge_table_loads():
    table = load_judge_table(REPO / "eval" / "judge_table.yaml")
    assert len(table) == 30
    assert {i["区分"] for i in table} == {"finding", "nudge", "silent", "intake", "unknown"}
    assert yaml is not None


def test_fingerprint_is_stable_and_ignores_wording():
    a = finding(message="言い方A")
    b = finding(message="言い方B")
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(finding(user="U02"))
    assert fingerprint(a) != fingerprint(finding(file="利用者/b.pdf"))
