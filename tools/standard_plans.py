"""架空データの計画書（PDF / Word）を、標準様式の配置に置き直す。

居宅サービス計画書は第1〜3表（老企第29号）、介護予防は介護予防サービス・支援計画書の形にする。文章は変えず配置だけを変える。
元の文字が1つでも落ちるファイルと、取り消し線のあるファイルは置き直さず、そのままにする。Excel 版は対象外。

  python tools/standard_plans.py sample_data/drive/利用者
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

GLOBS = ("居宅サービス計画書_*", "介護予防サービス・支援計画書_*")
MARKS = ("第１表", "計画に関する同意")
_FD = r"C:\Windows\Fonts"
OFFICE = "ケアプランセンターはしばみ"
_NOISE = re.compile(r"[\s　・、。：:，,（）()■｜|]|初回作成日|認定有効期間|週単位以外のサービス")


@dataclass
class Source:
    head: dict[str, str] = field(default_factory=dict)
    sections: dict[str, list[str]] = field(default_factory=dict)
    tables: dict[str, list[list[str]]] = field(default_factory=dict)
    is_yobo: bool = False
    raw: str = ""


def read_events(path: Path) -> list:
    """文は str、表は行の list として、上から順に返す。"""
    return _docx_events(path) if path.suffix == ".docx" else _pdf_events(path)


def _docx_events(path: Path) -> list:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(path)
    out: list = []
    for el in d.element.body.iterchildren():
        if el.tag.endswith("}p"):
            text = Paragraph(el, d).text.strip()
            if text:
                out.append(text)
        elif el.tag.endswith("}tbl"):
            out.append([[c.text.strip() for c in r.cells] for r in Table(el, d).rows])
    return out


def _pdf_events(path: Path) -> list:
    import fitz

    out: list = []
    for page in fitz.open(path):
        tables = page.find_tables().tables
        boxes = [fitz.Rect(t.bbox) for t in tables]
        items = [(t.bbox[1], [[_cell_text(page, c) for c in row.cells] for row in t.rows]) for t in tables]
        for b in page.get_text("blocks"):
            if b[6] != 0 or any(box.contains(fitz.Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)) for box in boxes):
                continue
            items.append((b[1], "".join(ln.strip() for ln in b[4].splitlines())))
        for _, item in sorted(items, key=lambda x: x[0]):
            if isinstance(item, str) and (not item or item == OFFICE or re.fullmatch(r"- \d+ -", item)):
                continue
            out.append(item)
    return out


def _cell_text(page, box) -> str:
    """欄の中を書かれた順に読む。表の抽出機能に任せると句読点の位置がずれる。"""
    if box is None:
        return ""
    return "".join(ln.strip() for ln in page.get_text("text", clip=box).splitlines())


def has_strike(path: Path) -> bool:
    if path.suffix == ".docx":
        import docx

        d = docx.Document(path)
        cells = [p for t in d.tables for r in t.rows for c in r.cells for p in c.paragraphs]
        return any(run.font.strike for p in [*d.paragraphs, *cells] for run in p.runs)
    import fitz

    for page in fitz.open(path):
        spans = [s["bbox"] for b in page.get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"]]
        for drawing in page.get_drawings():
            r = drawing["rect"]
            if r.height < 1.5 and any(sp[1] + (sp[3] - sp[1]) * .3 < r.y0 < sp[3] - (sp[3] - sp[1]) * .3 and sp[0] <= r.x0 and r.x1 <= sp[2] + 1 for sp in spans):
                return True
    return False


def parse(events: list) -> Source:
    src = Source()
    section = "head"
    last_key = None
    for ev in events:
        if isinstance(ev, list):
            rows = ev
            bucket = src.tables.setdefault(section, [])
            bucket += rows[1:] if bucket and rows[0] == bucket[0] else rows
        elif ev.startswith("■"):
            section = ev.lstrip("■ ").strip()
        elif ev.startswith(("居宅サービス計画書", "介護予防サービス・支援計画書")) and section == "head":
            src.is_yobo = ev.startswith("介護予防")
        elif section == "head":
            if ev.startswith("・"):
                key, _, value = ev.lstrip("・").partition("：")
                last_key = key.strip()
                src.head[last_key] = value.strip()
            elif last_key:  # PDF の折り返しの続き
                src.head[last_key] += ev
        else:
            lines = src.sections.setdefault(section, [])
            if lines and not ev.startswith(("・", "※", "週単位")):
                lines[-1] += ev
            else:
                lines.append(ev.lstrip("・ "))
    values = [*src.head.values(), *[x for lines in src.sections.values() for x in lines]]
    src.raw = "".join(values) + "".join("".join(r) for rows in src.tables.values() for r in rows[1:])  # 見出しは様式の見出しに置き換わる
    return src


def user_parts(src: Source) -> dict[str, str]:
    text = src.head.get("利用者", "")
    m = re.match(r"(.+?)\s*様(?:（U\d+）)?\s*(\S*)\s*認定有効期間\s*(.+)", text)
    return {"name": m[1], "level": m[2], "period": m[3]} if m else {"name": text, "level": "", "period": ""}


def split_made(text: str) -> tuple[str, str]:
    m = re.match(r"(.+?)\s*初回作成日[:：]\s*(.+)", text)
    return (m[1], m[2]) if m else (text, "")


def _split_period(cell: str) -> tuple[str, str]:
    m = re.match(r"(.*?)（([^（）]*\d{4}-[^（）]*)）$", cell.strip())
    return (m[1], m[2]) if m else (cell, "")


def _c(text: str = "", span: int = 1, label: bool = False) -> dict:
    return {"text": text, "span": span, "label": label}


def _section_value(lines: list[str], prefix: str) -> str:
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):].lstrip("：: ")
    return ""


def _kyotaku_layout(src: Source) -> list[dict]:
    u = user_parts(src)
    made, first = split_made(src.head.get("作成日", ""))
    made_day = made.split("（")[0].strip()
    first1 = src.sections.get("第1表", [])
    known = ("利用者及び家族の生活に対する意向を踏まえた課題分析の結果", "介護認定審査会の意見及びサービスの種類の指定", "総合的な援助の方針", "生活援助中心型の算定理由")
    wishes = [ln for ln in first1 if not ln.startswith(known)]
    table1 = [
        [_c("利用者名", label=True), _c(f"{u['name']}　殿"), _c("生年月日", label=True), _c(""), _c("住所", label=True), _c("")],
        [_c("居宅サービス計画作成者氏名", label=True), _c(src.head.get("作成者", ""), 5)],
        [_c("居宅介護支援事業者・事業所名及び所在地", label=True), _c(OFFICE, 5)],
        [_c("居宅サービス計画作成（変更）日", label=True), _c(made), _c("初回居宅サービス計画作成日", label=True), _c(first, 3)],
        [_c("認定日", label=True), _c(""), _c("認定の有効期間", label=True), _c(u["period"], 3)],
        [_c("要介護状態区分", label=True), _c(u["level"], 5)],
        [_c(known[0], label=True), _c("\n".join(wishes), 5)],
        [_c(known[1], label=True), _c(_section_value(first1, known[1]), 5)],
        [_c(known[2], label=True), _c(_section_value(first1, known[2]), 5)],
        [_c(known[3], label=True), _c(_section_value(first1, known[3]), 5)],
        [_c("計画の説明・同意", label=True), _c(src.head.get("計画の説明・同意", ""), 5)],
    ]
    rest = [f"{k}：{v}" for k, v in src.head.items() if k not in ("利用者", "作成者", "作成日", "計画の説明・同意")]
    blocks = [
        {"kind": "title", "left": "第１表", "center": "居宅サービス計画書（１）", "right": f"作成年月日　{made_day}"},
        {"kind": "table", "rows": table1, "widths": [20, 18, 16, 18, 8, 20]},
    ]
    if rest:
        blocks += [{"kind": "table", "rows": [[_c("事業所の控え", label=True), _c("\n".join(rest))]], "widths": [20, 80]}]
    rows2 = [[_c("生活全般の解決すべき課題（ニーズ）", label=True), _c("長期目標", label=True), _c("（期間）", label=True), _c("短期目標", label=True), _c("（期間）", label=True),
              _c("サービス内容", label=True), _c("※1", label=True), _c("サービス種別", label=True), _c("※2", label=True), _c("頻度", label=True), _c("期間", label=True)]]
    for row in src.tables.get("第2表", [])[1:]:
        long_goal, long_term = _split_period(row[1])
        short_goal, short_term = _split_period(row[2])
        rows2.append([_c(row[0]), _c(long_goal), _c(long_term), _c(short_goal), _c(short_term), *[_c(x) for x in row[3:]]])
    blocks += [
        {"kind": "break"},
        {"kind": "title", "left": "第２表", "center": "居宅サービス計画書（２）", "right": f"作成年月日　{made_day}"},
        {"kind": "text", "text": f"利用者名　{u['name']}　殿"},
        {"kind": "table", "rows": rows2, "widths": [12, 10, 9, 10, 9, 14, 3, 8, 9, 5, 9]},
        {"kind": "text", "text": "※1 保険給付の対象となるかどうかの区分　※2 当該サービス提供を行う事業所"},
    ]
    blocks += [{"kind": "text", "text": ln} for ln in src.sections.get("第2表", [])]
    week_key = next((k for k in src.tables if k.startswith("第3表")), "")
    days = {"月": "", "火": "", "水": "", "木": "", "金": "", "土": "", "日": ""}
    others = []
    for row in src.tables.get(week_key, [])[1:]:
        if row[0] in days:
            days[row[0]] = row[1]
        else:
            others.append(f"{row[0]}：{row[1]}")
    blocks += [
        {"kind": "break"},
        {"kind": "title", "left": "第３表", "center": "週間サービス計画表", "right": f"作成年月日　{made_day}"},
        {"kind": "text", "text": f"利用者名　{u['name']}　殿"},
        {"kind": "table", "rows": [[_c(d, label=True) for d in days], [_c(v) for v in days.values()]], "widths": [14, 14, 14, 15, 15, 14, 14]},
        {"kind": "table", "rows": [[_c("曜日によらないサービス", label=True), _c("\n".join(others))],
                                   [_c("週単位以外のサービス", label=True), _c("\n".join(x.split("週単位以外のサービス:")[-1].strip() for x in src.sections.get(week_key, [])))]], "widths": [20, 80]},
    ]
    return blocks


def _yobo_layout(src: Source) -> list[dict]:
    u = user_parts(src)
    lines = src.sections.get("目標とする生活", [])
    table = src.tables.get("支援計画", [])
    head = [[_c(h, label=True) for h in table[0]]] if table else []
    consumed = ("利用者", "計画の説明・同意")
    rest = [f"{k}：{v}" for k, v in src.head.items() if k not in consumed]
    week = src.tables.get("週間の予定", [])
    return [
        {"kind": "title", "left": "", "center": "介護予防サービス・支援計画書", "right": ""},
        {"kind": "table", "rows": [[_c("利用者名", label=True), _c(f"{u['name']}　様"), _c("要介護度", label=True), _c(u["level"]), _c("認定の有効期間", label=True), _c(u["period"])]], "widths": [12, 22, 10, 14, 14, 28]},
        {"kind": "table", "rows": [[_c("計画の作成", label=True), _c("\n".join(rest))]], "widths": [12, 88]},
        {"kind": "table", "rows": [[_c("目標とする生活", label=True), _c("\n".join(lines))]], "widths": [12, 88]},
        {"kind": "table", "rows": head + [[_c(x) for x in row] for row in table[1:]], "widths": [16, 14, 12, 16, 16, 8, 10, 8]},
        {"kind": "table", "rows": [[_c("週間の予定", label=True), _c("\n".join(f"{a}：{b}" for a, b in week[1:]))]], "widths": [12, 88]},
        {"kind": "table", "rows": [[_c("計画に関する同意", label=True), _c(src.head.get("計画の説明・同意", ""))]], "widths": [12, 88]},
    ]


def layout(src: Source) -> list[dict]:
    return _yobo_layout(src) if src.is_yobo else _kyotaku_layout(src)


def write_pdf(blocks: list[dict], path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(TTFont("MSMincho", _FD + r"\msmincho.ttc", subfontIndex=0))
    base = ParagraphStyle("b", fontName="MSMincho", fontSize=9, leading=12, wordWrap="CJK")
    label = ParagraphStyle("l", parent=base, fontSize=8, leading=10.5)
    head = ParagraphStyle("h", parent=base, fontSize=14, leading=18, alignment=1)
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br/>")
    width = landscape(A4)[0] - 24 * mm
    story: list = []
    for b in blocks:
        if b["kind"] == "break":
            story.append(PageBreak())
        elif b["kind"] == "title":
            t = Table([[Paragraph(esc(b["left"]), base), Paragraph(esc(b["center"]), head), Paragraph(esc(b["right"]), base)]], colWidths=[width * .2, width * .55, width * .25])
            story.append(t)
        elif b["kind"] == "text":
            story.append(Paragraph(esc(b["text"]), base))
        else:
            cols = len(b["widths"])
            data, style = [], [("GRID", (0, 0), (-1, -1), .5, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
            for r, row in enumerate(b["rows"]):
                line, c = [], 0
                for cell in row:
                    line.append(Paragraph(esc(cell["text"]), label if cell["label"] else base))
                    if cell["span"] > 1:
                        style.append(("SPAN", (c, r), (c + cell["span"] - 1, r)))
                    line += [""] * (cell["span"] - 1)
                    c += cell["span"]
                data.append(line + [""] * (cols - len(line)))
            total = sum(b["widths"])
            table = Table(data, colWidths=[width * w / total for w in b["widths"]], repeatRows=0)
            table.setStyle(TableStyle(style))
            story += [table, Spacer(1, 4)]
    SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm, bottomMargin=10 * mm, title="計画書").build(story)


def write_docx(blocks: list[dict], path: Path) -> None:
    import docx
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_BREAK
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    d = docx.Document()
    sec = d.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = Mm(297), Mm(210)
    sec.left_margin = sec.right_margin = Mm(12)
    sec.top_margin = sec.bottom_margin = Mm(10)
    style = d.styles["Normal"]
    style.font.name = "Yu Gothic"
    style.font.size = Pt(9)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "Yu Gothic")
    style.paragraph_format.space_after = Pt(0)
    for b in blocks:
        if b["kind"] == "break":
            d.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        elif b["kind"] == "title":
            p = d.add_paragraph()
            p.add_run(f"{b['left']}　　　　　")
            p.add_run(b["center"]).bold = True
            p.add_run(f"　　　　　{b['right']}")
        elif b["kind"] == "text":
            d.add_paragraph(b["text"])
        else:
            cols = len(b["widths"])
            t = d.add_table(rows=len(b["rows"]), cols=cols)
            t.style = "Table Grid"
            for r, row in enumerate(b["rows"]):
                c = 0
                for cell in row:
                    target = t.cell(r, c)
                    if cell["span"] > 1:
                        target = target.merge(t.cell(r, c + cell["span"] - 1))
                    target.text = cell["text"]
                    c += cell["span"]
            d.add_paragraph()
    d.save(path)


def read_text(path: Path) -> str:
    return "".join(e if isinstance(e, str) else "".join("".join(r) for r in e) for e in read_events(path))


def lost_chars(before: str, after: str) -> str:
    """元にあって、置き直した後に足りない文字。"""
    before = re.sub(r"様（U\d+）", "", before)
    missing = Counter(_NOISE.sub("", before)) - Counter(_NOISE.sub("", after))
    return "".join(missing.elements())


def order_broken(src: Source, path: Path) -> list[str]:
    """読み取った文が、元の PDF の文字の並びのとおりか。句読点のずれや、別の欄への混入を見つける。"""
    if path.suffix != ".pdf":
        return []
    from pypdf import PdfReader

    original = re.sub(r"\s", "", "".join(pg.extract_text() for pg in PdfReader(path).pages))
    pieces = [*src.head.values(), *[x for lines in src.sections.values() for x in lines], *[c for rows in src.tables.values() for r in rows[1:] for c in r]]
    return [x for x in pieces if re.sub(r"\s", "", x) not in original]


def restyle(path: Path) -> str:
    """結果を done / already / strike / lost / garbled のどれかで返す。"""
    events = read_events(path)
    text = "".join(e for e in events if isinstance(e, str)) + "".join("".join("".join(r) for r in e) for e in events if isinstance(e, list))
    if any(m in text for m in MARKS):
        return "already"
    if has_strike(path):
        return "strike"
    src = parse(events)
    broken = order_broken(src, path)
    if broken:
        print(path.name, "並びが元と違う:", broken[:2], file=sys.stderr)
        return "garbled"
    blocks = layout(src)
    tmp = path.with_name("~" + path.name)
    (write_docx if path.suffix == ".docx" else write_pdf)(blocks, tmp)
    after = read_text(tmp)
    if lost_chars(src.raw, after):
        print(path.name, "落ちた文字:", lost_chars(src.raw, after)[:60], file=sys.stderr)
        tmp.unlink()  # 自分で作った一時ファイルだけを消す
        return "lost"
    tmp.replace(path)
    return "done"


if __name__ == "__main__":
    files = [p for g in GLOBS for p in sorted(Path(sys.argv[1]).rglob(g)) if p.suffix in (".pdf", ".docx")]
    print(Counter(restyle(p) for p in files))
