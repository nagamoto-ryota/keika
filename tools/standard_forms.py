"""架空データの「サービス担当者会議の要点」を、標準様式（老企第29号 第4表）の形に置き直す。

中身の文章は変えず、配置だけを変える。ファイル名と形式（PDF / Word）はそのまま。

  python tools/standard_forms.py sample_data/drive/利用者
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

GLOB = "サービス担当者会議の要点_*"
_FD = r"C:\Windows\Fonts"


@dataclass
class Minutes:
    user: str = ""
    date: str = ""
    time: str = ""
    place: str = ""
    author: str = ""
    purpose: str = ""
    attendees: list[tuple[str, str]] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    conclusion: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def read_lines(path: Path) -> list[str]:
    if path.suffix == ".docx":
        return _docx_lines(path)
    from pypdf import PdfReader

    text = "\n".join(p.extract_text() for p in PdfReader(path).pages)
    lines = [ln.strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln and ln != "ケアプランセンターはしばみ" and not re.fullmatch(r"- \d+ -", ln)]


def _docx_lines(path: Path) -> list[str]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(path)
    lines = []
    for el in d.element.body.iterchildren():
        if el.tag.endswith("}p"):
            text = Paragraph(el, d).text.strip()
            if text:
                lines.append(text)
        elif el.tag.endswith("}tbl"):
            for row in Table(el, d).rows:
                lines.extend(c.text.strip() for c in row.cells)
    return lines


def parse(lines: list[str]) -> Minutes:
    m = Minutes()
    section = "head"
    for line in lines:
        if line.startswith("■ 出席者"):
            section = "attend"
        elif line.startswith("■ 検討した項目"):
            section = "items"
        elif line.startswith("■ 結論"):
            section = "conclusion"
        elif line.startswith("■ 残された課題"):
            section = "issues"
        elif section == "head":
            _head(m, line)
        elif section == "attend":
            _attend(m, line)
        elif section == "items":
            if re.match(r"\d+\. ", line) or not m.items:
                m.items.append(line)
            else:
                m.items[-1] += line
        else:
            bucket = m.conclusion if section == "conclusion" else m.issues
            if line.startswith("・") or not bucket:
                bucket.append(line.lstrip("・"))
            else:
                bucket[-1] += line
    # 2列の表を PDF から読むと1行ずつに分かれるので、組にする
    flat, m.attendees = m.attendees, []
    for i in range(2, len(flat) - 1, 2):
        m.attendees.append((flat[i], flat[i + 1]))
    return m


def _head(m: Minutes, line: str) -> None:
    if line.startswith("・利用者："):
        m.user = re.sub(r"\s*様.*$", "", line[len("・利用者："):])
    elif line.startswith("・開催日時："):
        found = re.match(r"・開催日時：(\S+?（.）)\s*(\S+)\s+場所:\s*(.+)", line)
        m.date, m.time, m.place = found.groups()
    elif line.startswith("・作成者："):
        m.author = line[len("・作成者："):]
    elif line.startswith("・会議の目的："):
        m.purpose = line[len("・会議の目的："):]


def _attend(m: Minutes, line: str) -> None:
    table_done = len(m.attendees) >= 4 and (m.attendees[-1:] and _is_absence(line))
    if table_done or m.absent:
        if _is_absence(line) or not m.absent:
            m.absent.append(line)
        else:
            m.absent[-1] += line
    else:
        m.attendees.append(line)  # 後で組にする


def _is_absence(line: str) -> bool:
    return bool(re.match(r"(欠席:|主治医)", line)) or "欠席" in line


def item_titles(m: Minutes) -> list[str]:
    titles = []
    for it in m.items:
        body = re.sub(r"^\d+\. ", "", it)
        titles.append(re.split(r"[:：]", body, maxsplit=1)[0])
    return titles


def attendee_rows(m: Minutes, per_row: int = 3) -> list[list[tuple[str, str]]]:
    rows = [m.attendees[i:i + per_row] for i in range(0, len(m.attendees), per_row)]
    if rows:
        rows[-1] = rows[-1] + [("", "")] * (per_row - len(rows[-1]))
    return rows


def body_texts(m: Minutes) -> list[str]:
    """様式の枠に入れる中身。置き直しで文が落ちていないかの確認にも使う。"""
    return [m.user, m.author, m.date, m.time, m.place, m.purpose, *[x for p in m.attendees for x in p], *m.absent, *m.items, *m.conclusion, *m.issues]


def write_pdf(m: Minutes, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    pdfmetrics.registerFont(TTFont("MSMincho", _FD + r"\msmincho.ttc", subfontIndex=0))
    width = landscape(A4)[0] - 24 * mm

    def story(size: float) -> list:
        base = ParagraphStyle("b", fontName="MSMincho", fontSize=size, leading=size * 1.35, wordWrap="CJK")
        title = ParagraphStyle("t", parent=base, fontSize=14, leading=18, alignment=1)
        small = ParagraphStyle("s", parent=base, fontSize=size - 1, leading=(size - 1) * 1.3)
        P = lambda text, st=base: Paragraph(text.replace("&", "&amp;").replace("<", "&lt;"), st)
        L = lambda text: Paragraph(text, small)
        box = TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                          ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                          ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)])
        head = Table([[P("第４表"), P("サービス担当者会議の要点", title), P(f"作成年月日　{m.date}")]], colWidths=[width * .15, width * .6, width * .25])
        who = Table([[P(f"利用者名　{m.user}　殿"), P(f"居宅サービス計画作成者（担当者）氏名　{m.author}")],
                     [P(f"開催日　{m.date}　　開催場所　{m.place}"), P(f"開催時間　{m.time}　　開催回数　")]], colWidths=[width * .5, width * .5])
        rows = [[L("会議出席者"), L("所属（職種）"), L("氏名"), L("所属（職種）"), L("氏名"), L("所属（職種）"), L("氏名")]]
        for row in attendee_rows(m):
            rows.append([""] + [P(x) for pair in row for x in pair])
        w = (width - width * .08) / 3
        people = Table(rows, colWidths=[width * .08, w * .68, w * .32, w * .68, w * .32, w * .68, w * .32])
        people.setStyle(box)
        people.setStyle(TableStyle([("SPAN", (0, 0), (0, -1))]))
        label = width * .13
        content = [[L("検討した項目"), [P(f"会議の目的：{m.purpose}"), P("、".join(item_titles(m)))]],
                   [L("検討内容"), [P(x) for x in m.items] + [P(x) for x in m.absent]],
                   [L("結論"), [P(x) for x in m.conclusion]],
                   [L("残された課題<br/>（次回の開催時期）"), [P(x) for x in m.issues]]]
        lower = Table(content, colWidths=[label, width - label])
        lower.setStyle(box)
        who.setStyle(box)
        return [head, who, people, lower]

    for size in (10, 9.5, 9, 8.5, 8):
        doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm, bottomMargin=10 * mm,
                                title="サービス担当者会議の要点")
        doc.build(story(size))
        if doc.page == 1:
            return


def write_docx(m: Minutes, path: Path) -> None:
    import docx
    from docx.enum.section import WD_ORIENT
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    d = docx.Document()
    sec = d.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = Mm(297), Mm(210)
    for side in ("left_margin", "right_margin"):
        setattr(sec, side, Mm(12))
    sec.top_margin = sec.bottom_margin = Mm(10)
    style = d.styles["Normal"]
    style.font.name = "Yu Gothic"
    style.font.size = Pt(9.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "Yu Gothic")
    style.paragraph_format.space_after = Pt(0)

    def fill(cell, text: str) -> None:
        cell.text = text

    p = d.add_paragraph()
    p.add_run("第４表　　　　　　　　　サービス担当者会議の要点").bold = True
    p.add_run(f"　　　　　　　　　作成年月日　{m.date}")
    t = d.add_table(rows=2, cols=2)
    t.style = "Table Grid"
    fill(t.cell(0, 0), f"利用者名　{m.user}　殿")
    fill(t.cell(0, 1), f"居宅サービス計画作成者（担当者）氏名　{m.author}")
    fill(t.cell(1, 0), f"開催日　{m.date}　　開催場所　{m.place}")
    fill(t.cell(1, 1), f"開催時間　{m.time}　　開催回数　")
    rows = attendee_rows(m)
    t = d.add_table(rows=len(rows) + 1, cols=7)
    t.style = "Table Grid"
    for col, word in enumerate(["会議出席者", "所属（職種）", "氏名", "所属（職種）", "氏名", "所属（職種）", "氏名"]):
        fill(t.cell(0, col), word)
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(x for pair in row for x in pair):
            fill(t.cell(r, c + 1), value)
    t.cell(0, 0).merge(t.cell(len(rows), 0))
    t = d.add_table(rows=4, cols=2)
    t.style = "Table Grid"
    blocks = [("検討した項目", [f"会議の目的：{m.purpose}", "、".join(item_titles(m))]),
              ("検討内容", m.items + m.absent), ("結論", m.conclusion), ("残された課題（次回の開催時期）", m.issues)]
    for r, (label, texts) in enumerate(blocks):
        fill(t.cell(r, 0), label)
        fill(t.cell(r, 1), "\n".join(texts))
        t.cell(r, 0).width = Mm(35)
        t.cell(r, 1).width = Mm(238)
    d.save(path)


def restyle(path: Path) -> Minutes | None:
    lines = read_lines(path)
    if any("第４表" in ln for ln in lines):
        return None  # 置き直し済み
    m = parse(lines)
    (write_docx if path.suffix == ".docx" else write_pdf)(m, path)
    return m


if __name__ == "__main__":
    done = [restyle(p) for p in sorted(Path(sys.argv[1]).rglob(GLOB))]
    print(f"{sum(d is not None for d in done)} 件を第4表の形に置き直しました")
