"""根拠の原文: 動かす全行で省令の本文が引け、解釈通知と Q&A は保存した公式文書と一字一句合うこと。"""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from keika.db import connect
from keika.legal_texts import SOURCES_DIR, basis_for, load_notices, parse_refs, section
from keika.rules import active_rules, all_rules, get_rule
from keika.settings import Settings
from keika.web.app import create_app


def squeeze(text: str) -> str:
    return re.sub(r"\s", "", text)


def test_parse_refs_reads_the_written_forms():
    assert parse_refs("居宅基準13条9号・15号、予防基準30条9号・17号") == [("kyotaku", "13条9号"), ("kyotaku", "13条15号"), ("yobo", "30条9号"), ("yobo", "30条17号")]
    assert parse_refs("居宅基準13条14号イ・ロ・ハ、予防基準30条16号") == [("kyotaku", "13条14号"), ("yobo", "30条16号")]
    assert parse_refs("居宅基準19条の2、予防基準18条の2") == [("kyotaku", "19条の2"), ("yobo", "18条の2")]
    assert parse_refs("居宅基準13条11号・19号の2、予防基準30条11号・21号の2")[1] == ("kyotaku", "13条19号の2")
    assert parse_refs("居宅基準13条4号（努力義務）、予防基準30条4号")[0] == ("kyotaku", "13条4号")
    assert parse_refs("居宅基準21条の2、老企第22号 第2の3⒄、予防基準20条の2") == [("kyotaku", "21条の2"), ("yobo", "20条の2")]
    assert parse_refs("居宅基準27条の2、18条6号、老企第22号 第2の3、予防基準26条の2・17条") == [("kyotaku", "27条の2"), ("kyotaku", "18条6号"), ("yobo", "26条の2"), ("yobo", "17条")]
    assert parse_refs("居宅基準22条1〜3項、令和6年厚労省令16号附則2条、予防基準21条") == [("kyotaku", "22条"), ("yobo", "21条")]


def test_known_articles_have_the_official_words():
    assert "サービス担当者会議" in section("kyotaku", "13条9号").text
    assert "三十日前" in section("kyotaku", "8条3項").text
    assert "少なくとも一月に一回、利用者に面接すること" in section("kyotaku", "13条14号").text
    assert "主治の医師等に交付" in section("kyotaku", "13条19号の2").text
    assert "業務継続計画" in section("kyotaku", "19条の2").text


@pytest.mark.parametrize("rule", [r for r in all_rules() if r.active and not r.not_required], ids=lambda r: r.id)
def test_every_active_rule_resolves_to_ordinance_text(rule):
    if not rule.basis_confirmed:
        pytest.skip("根拠確認中の行")
    b = basis_for(rule.id)
    assert b.kyotaku, rule.legal_basis
    assert all(s.text.strip() for s in b.kyotaku + b.yobo)


def test_r07_points_to_delivery_to_the_doctor_not_business_continuity():
    texts = "".join(s.text for s in basis_for("R07").kyotaku)
    assert "主治の医師等に交付" in texts and "業務継続計画" not in texts


def test_rule_titles_match_ordinance_topics():
    """番号の取り違えを防ぐ。行の主題の語が条文に出てくること。"""
    expect = {"R04": "福祉用具", "R06": "モニタリング", "R08": "主治の医師", "R12": "訪問介護計画", "R21": "個人情報", "R23": "業務継続計画",
              "R24": "感染症", "R25": "虐待", "R27": "掲示", "R28": "性的な言動", "R31": "秘密", "R32": "事故", "R44": "更新", "R45": "サービス担当者会議"}
    for rule_id, word in expect.items():
        assert word in "".join(s.text for s in basis_for(rule_id).kyotaku), rule_id


def test_notices_match_saved_official_text_exactly():
    for n in load_notices():
        source = (SOURCES_DIR / f"{n.source}.txt").read_text(encoding="utf-8")
        assert squeeze(n.quote) in squeeze(source), (n.rule, n.where)
        assert n.url.startswith("https://") and n.doc and n.where
        get_rule(n.rule)


def test_unknown_reference_raises_instead_of_inventing():
    with pytest.raises(KeyError):
        section("kyotaku", "99条1号")


@pytest.fixture
def client(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    (root / "事業所").mkdir()
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    connect(s).close()
    return TestClient(create_app(s))


def test_office_row_shows_ordinance_text_and_link(client):
    body = client.get("/office").text
    assert "業務継続計画" in body and "laws.e-gov.go.jp/law/411M50000100038" in body


def test_active_rules_used_today_are_covered():
    assert active_rules(date(2026, 11, 4))
