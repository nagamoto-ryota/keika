"""計画書の置き直し: 文字が落ちない、様式の枠がある、取り消し線や欠落があれば触らない。"""
import sys
from pathlib import Path

import pytest

from conftest import SAMPLE

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
pytest.importorskip("fitz")
pytest.importorskip("reportlab")
import standard_plans as sp  # noqa: E402

HEADER2 = ["生活全般の解決すべき課題（ニーズ）", "長期目標（期間）", "短期目標（期間）", "サービス内容", "※1", "サービス種別", "※2 事業所", "頻度", "期間"]
KYOTAKU = [
    "居宅サービス計画書", "・利用者：試験 花子 様（U99）　要介護2　認定有効期間 2026-04-01〜2028-03-31", "・作成者：試験 次郎（2026-07-01 から担当 別木 三郎）",
    "・作成日：2026-03-26（更新）　初回作成日: 2024-03-12", "・計画の期間：2026-04-01〜2027-03-31", "・計画の説明・同意：2026-03-26 本人に説明し同意を得た（本人署名）",
    "・交付：2026-03-27 本人へ手渡し", "・変更の記録：なし", "■ 第1表", "・利用者及び家族の生活に対する意向を踏まえた課題分析の結果：", "・本人：家にいたい。", "・長女：続けてほしい。",
    "・課題分析の結果：膝が痛い。", "・介護認定審査会の意見及びサービスの種類の指定：なし", "・総合的な援助の方針：歩く力を保ちます。緊急連絡先: 長女 000-0000-0000。",
    "・生活援助中心型の算定理由：1. 一人暮らし　2. 家族等が障害、疾病等　3. その他（　　　　）", "■ 第2表",
    [HEADER2, ["歩きたい", "転ばず歩ける（2026-04-01〜2027-03-31）", "訓練する（2026-04-01〜2026-09-30 →2026-10-01〜2027-03-31）", "機能訓練", "○", "通所介護", "こなら坂", "週2回", "2026-04-01〜2027-03-31"]],
    [HEADER2, ["", "", "", "手すり", "○", "福祉用具貸与", "レンタル", "毎日", "2026-04-01〜2027-03-31"]],
    "※短期目標の期間延長は軽微な変更として処理", "■ 第3表（週間サービス計画）", [["曜日", "サービス"], ["月", "通所介護 9:00〜16:00"], ["火", "―"], ["毎日", "手すり"], ["月〜金", "配食（昼）"]],
    "週単位以外のサービス: 月1回 診療所受診（タクシー）。",
]
YOBO_HEAD = ["アセスメント領域と課題", "具体策についての意向（本人・家族）", "目標", "本人等のセルフケアや家族の支援、インフォーマルサービス", "介護保険サービス又は地域支援事業", "サービス種別", "事業所", "期間"]
YOBO = [
    "介護予防サービス・支援計画書", "・利用者：試験 花子 様（U99）　要支援2　認定有効期間 2026-05-01〜2027-04-30", "・委託元：包括（担当 小田切保健師）",
    "・作成日：2026-04-28（更新）　計画の期間: 2026-05-01〜2027-04-30", "・評価期間：2026-05-01〜2027-04-30（期間終了月に評価）", "・計画の説明・同意：2026-04-28 本人に説明し同意を得た（本人署名）",
    "■ 目標とする生活", "・1日: 午前中に家事をすませる。", "・1年: 仲間とお茶をする。", "■ 支援計画", [YOBO_HEAD, ["運動・移動: 歩行距離の低下", "本人「歩きたい」", "500m 歩く", "毎日 散歩", "運動器の機能向上", "通所型", "こなら坂", "2026-05-01〜2027-04-30"]],
    "■ 週間の予定", [["曜日", "内容"], ["月", "通所型サービス 9:30〜12:30"], ["毎日", "散歩"]],
]


@pytest.mark.parametrize("events", [KYOTAKU, YOBO], ids=["居宅", "予防"])
@pytest.mark.parametrize("suffix", [".pdf", ".docx"])
def test_every_character_survives(tmp_path, events, suffix):
    src = sp.parse(events)
    path = tmp_path / f"計画{suffix}"
    (sp.write_docx if suffix == ".docx" else sp.write_pdf)(sp.layout(src), path)
    assert sp.lost_chars(src.raw, sp.read_text(path)) == ""


@pytest.mark.parametrize("suffix", [".pdf", ".docx"])
def test_kyotaku_has_form_labels_and_keeps_blank_reason_blank(tmp_path, suffix):
    src = sp.parse(KYOTAKU)
    path = tmp_path / f"計画{suffix}"
    (sp.write_docx if suffix == ".docx" else sp.write_pdf)(sp.layout(src), path)
    text = sp.read_text(path).replace(" ", "").replace("　", "")
    for word in ("第１表", "第２表", "第３表", "居宅サービス計画書（１）", "居宅サービス計画書（２）", "週間サービス計画表", "利用者名", "居宅サービス計画作成者氏名", "初回居宅サービス計画作成日",
                 "認定の有効期間", "要介護状態区分", "総合的な援助の方針", "生活援助中心型の算定理由", "長期目標", "短期目標", "サービス内容", "週単位以外のサービス"):
        assert word in text, word


def test_form_does_not_print_a_consent_sentence_as_fact():
    """様式の定型文「同意し交付を受けました」を足すと、書いていない事実になる。"""
    text = str(sp.layout(sp.parse(KYOTAKU)))
    assert "交付を受けました" not in text and "同意し" not in text.replace("同意を得た", "")


def test_goal_and_period_are_separated():
    assert sp._split_period("訓練する（2026-04-01〜2026-09-30 →2026-10-01〜2027-03-31）") == ("訓練する", "2026-04-01〜2026-09-30 →2026-10-01〜2027-03-31")
    assert sp._split_period("歩きたい") == ("歩きたい", "")


def test_wrapped_pdf_line_stays_with_its_item():
    src = sp.parse(["・交付：本人へ手渡しし、関係者へ", "送付した", "■ 第1表", "・総合的な援助の方針：見守ります。緊急連絡先: 長女", "000-0000-0000。", "・生活援助中心型の算定理由：該当なし"])
    assert src.head["交付"] == "本人へ手渡しし、関係者へ送付した"
    assert src.sections["第1表"] == ["総合的な援助の方針：見守ります。緊急連絡先: 長女000-0000-0000。", "生活援助中心型の算定理由：該当なし"]


def test_pdf_cell_keeps_punctuation_in_place(tmp_path):
    src = sp.parse(KYOTAKU)
    path = tmp_path / "計画.pdf"
    sp.write_pdf(sp.layout(src), path)
    tables = [e for e in sp.read_events(path) if isinstance(e, list)]
    cells = "".join(c for rows in tables for r in rows for c in r)
    assert "1.一人暮らし2.家族等が障害、疾病等3.その他（）" in cells.replace(" ", "").replace("　", "")


def test_garbled_reading_is_refused(tmp_path):
    path = tmp_path / "計画.pdf"
    sp.write_pdf(sp.layout(sp.parse(KYOTAKU)), path)
    src = sp.Source(head={"x": "存在しない文、です"})
    assert sp.order_broken(src, path) == ["存在しない文、です"]


def test_lost_text_is_detected():
    assert sp.lost_chars("週2回を継続", "週回を継続") == "2"


def test_file_with_strike_is_left_alone(tmp_path):
    import docx

    d = docx.Document()
    d.add_paragraph("居宅サービス計画書")
    d.add_paragraph().add_run("週2回").font.strike = True
    path = tmp_path / "居宅サービス計画書_x.docx"
    d.save(path)
    before = path.read_bytes()
    assert sp.restyle(path) == "strike" and path.read_bytes() == before


def test_pdf_strike_is_detected(tmp_path):
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    pdfmetrics.registerFont(TTFont("MSMincho", sp._FD + r"\msmincho.ttc", subfontIndex=0))
    style = ParagraphStyle("b", fontName="MSMincho", fontSize=10)
    path = tmp_path / "x.pdf"
    SimpleDocTemplate(str(path)).build([Paragraph("週<strike>2回</strike>を継続", style)])
    assert sp.has_strike(path) is True


def test_second_run_changes_nothing(tmp_path):
    path = tmp_path / "居宅サービス計画書_x.pdf"
    sp.write_pdf(sp.layout(sp.parse(KYOTAKU)), path)
    before = path.read_bytes()
    assert sp.restyle(path) == "already" and path.read_bytes() == before


@pytest.mark.parametrize("path", sorted(p for g in sp.GLOBS for p in (SAMPLE / "drive" / "利用者").rglob(g) if p.suffix in (".pdf", ".docx")), ids=lambda p: p.parent.name)
def test_sample_plans_use_the_form(path):
    text = sp.read_text(path)
    assert any(m in text for m in sp.MARKS)
