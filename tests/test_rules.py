from datetime import date

from keika.rules import active_rules, all_rules, basis_text, get_rule


def test_every_row_has_required_fields_and_unique_id():
    rows = all_rules()
    assert len(rows) >= 44
    assert len({r.id for r in rows}) == len(rows)
    assert all(r.title and r.plain and r.legal_basis and r.done_when and r.source for r in rows)


def test_rule_hidden_before_effective_date():
    assert "R27" not in {r.id for r in active_rules(date(2024, 3, 31))}


def test_rule_shown_after_grace_ends():
    # 虐待防止は経過措置 R6.3.31 まで
    assert "R25" not in {r.id for r in active_rules(date(2024, 3, 31))}
    assert "R25" in {r.id for r in active_rules(date(2024, 4, 1))}


def test_web_posting_appears_after_its_grace():
    assert "R27" not in {r.id for r in active_rules(date(2025, 3, 31))}
    assert "R27" in {r.id for r in active_rules(date(2025, 4, 1))}


def test_inactive_row_is_kept_but_not_running():
    assert get_rule("R36").active is False
    assert "R36" not in {r.id for r in active_rules(date(2026, 11, 4))}


def test_unconfirmed_basis_displays_as_pending():
    assert basis_text(get_rule("R15")) == "根拠確認中"
    assert "13条14号" in basis_text(get_rule("R06"))


def test_physical_restraint_committee_not_required_for_kyotaku():
    row = get_rule("R46")
    assert row.not_required is True
    assert row.id not in {r.id for r in active_rules(date(2026, 11, 4))}


def test_active_rows_are_fixed():
    # 動かす行は頻度の基準で先に決めた。増減したら理由を残すこと
    assert {r.id for r in all_rules() if r.active and not r.added_by} == {
        "R01", "R02", "R03", "R04", "R06", "R07", "R08", "R09", "R10", "R11", "R12", "R13", "R14",
        "R15", "R19", "R20", "R21", "R23", "R24", "R25", "R27", "R28", "R31", "R32", "R33", "R44", "R45", "R47",
    }


def test_record_content_row_runs_for_claude():
    row = get_rule("R47")
    assert row.active is True and row.judge == "claude" and "13条14号" in basis_text(row)


def test_housework_reason_row_runs_for_claude():
    row = get_rule("R15")
    assert row.active is True and row.judge == "claude"
