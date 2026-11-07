"""置き直しで文章が1つも落ちないこと。様式の枠があること。2度かけても壊れないこと。"""
import re
import sys
from pathlib import Path

import pytest

from conftest import SAMPLE

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
pytest.importorskip("reportlab")
import standard_forms as sf  # noqa: E402

FILES = sorted((SAMPLE / "drive" / "利用者").rglob(sf.GLOB))
LABELS = ("第４表", "利用者名", "居宅サービス計画作成者（担当者）氏名", "開催日", "開催場所", "開催時間", "開催回数",
          "会議出席者", "所属（職種）", "検討した項目", "検討内容", "結論", "残された課題", "作成年月日")
OLD = [
    "サービス担当者会議の要点（第4表相当）", "・利用者：試験 花子 様（U99）", "・開催日時：2026-03-26（木）13:30〜14:10　場所: 本人宅",
    "・作成者：試験 次郎", "・会議の目的：認定更新に伴う居宅サービス計画の検討", "■ 出席者", "所属（職種）", "氏名", "本人", "試験 花子",
    "家族（長女）", "試験 美和", "あおばデイ（生活相談員）", "岩本 修", "ひかりレンタル（福祉用具専門相談員）", "吉田 健", "試験の居宅（介護支援専門員）", "試験 次郎",
    "主治医（試験医院 久保医師）は欠席。3/20 に文書で照会し、", "3/24 に回答あり。", "■ 検討した項目と内容",
    "1. 本人の意向: 本人「今のままで」。", "2. 通所介護: 岩本相談員より、歩行訓練は", "平行棒内で可能との報告。週2回の継続で合意。", "■ 結論",
    "・通所介護週2回を継続する。", "■ 残された課題（次回の検討事項）", "・膝の痛みの経過。",
]


def squeeze(text: str) -> str:
    return re.sub(r"\s", "", text)


def test_parse_finds_every_part():
    m = sf.parse(OLD)
    assert (m.user, m.date, m.time, m.place, m.author) == ("試験 花子", "2026-03-26（木）", "13:30〜14:10", "本人宅", "試験 次郎")
    assert m.attendees[0] == ("本人", "試験 花子") and len(m.attendees) == 5
    assert m.absent == ["主治医（試験医院 久保医師）は欠席。3/20 に文書で照会し、3/24 に回答あり。"]
    assert len(m.items) == 2 and m.items[1].endswith("週2回の継続で合意。")
    assert m.conclusion == ["通所介護週2回を継続する。"] and m.issues == ["膝の痛みの経過。"]


@pytest.mark.parametrize("suffix", [".pdf", ".docx"])
def test_nothing_is_lost_and_second_run_changes_nothing(tmp_path, suffix):
    path = tmp_path / f"要点{suffix}"
    m = sf.parse(OLD)
    (sf.write_docx if suffix == ".docx" else sf.write_pdf)(m, path)
    after = squeeze("".join(sf.read_lines(path)))
    assert [t for t in sf.body_texts(m) if squeeze(t) not in after] == []
    assert all(word in after for word in LABELS)
    first = path.read_bytes()
    assert sf.restyle(path) is None and path.read_bytes() == first


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.parent.name)
def test_sample_data_uses_the_form(path):
    text = squeeze("".join(sf.read_lines(path)))
    assert all(word in text for word in LABELS)
    if path.suffix == ".pdf":
        from pypdf import PdfReader
        assert len(PdfReader(path).pages) == 1
