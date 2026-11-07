"""導入の手引きから保存する同意書のたたき台。docs/consent-template.md と同じ文面を同梱する（試験で揃える）。"""
from __future__ import annotations

import io
import re
from pathlib import Path

import docx
from docx.shared import Mm, Pt

CONSENT = Path(__file__).with_name("consent_template.md")


def consent_docx() -> bytes:
    """Markdown の見出し、箇条書き、表、段落を Word に置き直す。利用者に渡す書類なので、事業所向けの前書きは除く。"""
    doc = docx.Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.top_margin = section.bottom_margin = Mm(15)
    section.left_margin = section.right_margin = Mm(20)
    doc.styles["Normal"].font.size = Pt(10)
    doc.styles["Normal"].paragraph_format.space_after = Pt(3)
    doc.core_properties.author = ""
    table = None
    body = CONSENT.read_text(encoding="utf-8").split("\n---\n", 1)[1]
    for line in body.splitlines():
        line = line.strip().replace("`", "")
        if not line.startswith("|"):
            table = None
        if not line or line == "---":
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(":?-+:?", c) for c in cells):
                continue
            if table is None:
                table = doc.add_table(rows=0, cols=len(cells))
                table.style = "Table Grid"
            for cell, value in zip(table.add_row().cells, cells):
                cell.text = value
        elif m := re.match(r"(#+)\s+(.*)", line):
            doc.add_heading(m[2], level=min(len(m[1]) - 1, 3))
        elif line.startswith("- "):
            doc.add_paragraph(line[2:], style="List Bullet")
        else:
            doc.add_paragraph(line)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
