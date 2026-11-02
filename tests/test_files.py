from collections import Counter
from pathlib import Path

import docx
import openpyxl

from keika.files import DocRef, scan, to_payload
from keika.settings import Settings


def s_for(tmp_path: Path) -> Settings:
    root = tmp_path / "drive"
    (root / "利用者" / "田中花子").mkdir(parents=True)
    (root / "事業所").mkdir()
    (root / "入口").mkdir()
    return Settings(drive_root=root, db_path=tmp_path / "x.db")


def by_name(refs, name) -> DocRef:
    return next(r for r in refs if Path(r.rel_path).name == name)


def test_scan_finds_126_files_in_sample(sample_settings):
    refs = scan(sample_settings)
    assert len(refs) == 126
    assert Counter(r.ext for r in refs) == {"pdf": 82, "docx": 24, "csv": 8, "xlsx": 7, "txt": 5}


def test_scan_sets_scope_and_user_folder(sample_settings):
    refs = scan(sample_settings)
    assert Counter(r.scope for r in refs) == {"user": 98, "office": 23, "inbox": 5}
    user_doc = next(r for r in refs if r.scope == "user")
    assert user_doc.user_folder.startswith("U01_")
    assert next(r for r in refs if r.scope == "office").user_folder is None


def test_scan_ignores_files_outside_known_folders(tmp_path):
    s = s_for(tmp_path)
    (s.drive_root / "answer_key.csv").write_text("x", encoding="utf-8")
    assert scan(s) == []


def test_scan_ignores_temp_and_unknown_files(tmp_path):
    s = s_for(tmp_path)
    (s.drive_root / "事業所" / "~$tmp.docx").write_text("x", encoding="utf-8")
    (s.drive_root / "事業所" / "x.exe").write_text("x", encoding="utf-8")
    assert scan(s) == []


def test_sha_changes_with_content(tmp_path):
    s = s_for(tmp_path)
    f = s.drive_root / "事業所" / "a.txt"
    f.write_text("一", encoding="utf-8")
    first = scan(s)[0].sha256
    f.write_text("二", encoding="utf-8")
    assert scan(s)[0].sha256 != first


def test_docx_table_text_is_kept(sample_settings):
    refs = [r for r in scan(sample_settings) if r.ext == "docx" and r.user_folder and r.user_folder.startswith("U03")]
    texts = [to_payload(sample_settings, r).text for r in refs]
    assert any("生活援助中心型の算定理由" in t for t in texts)


def test_docx_keeps_paragraph_and_table_order(tmp_path):
    s = s_for(tmp_path)
    d = docx.Document()
    d.add_paragraph("前の段落")
    d.add_table(1, 1).rows[0].cells[0].text = "表の中"
    d.add_paragraph("後の段落")
    d.save(s.drive_root / "事業所" / "a.docx")
    t = to_payload(s, scan(s)[0]).text
    assert t.index("前の段落") < t.index("表の中") < t.index("後の段落")


def test_xlsx_all_sheets_are_kept(tmp_path):
    s = s_for(tmp_path)
    wb = openpyxl.Workbook()
    wb.active.title = "第1表"
    wb.active["A1"] = "一枚目"
    wb.create_sheet("第2表")["B3"] = "二枚目"
    wb.save(s.drive_root / "事業所" / "a.xlsx")
    t = to_payload(s, scan(s)[0]).text
    assert "第1表" in t and "一枚目" in t and "第2表" in t and "二枚目" in t and "B3" in t


def test_csv_utf8_bom_and_sjis_both_read(tmp_path):
    s = s_for(tmp_path)
    (s.drive_root / "事業所" / "bom.csv").write_bytes("日付,本文\n".encode("utf-8-sig"))
    (s.drive_root / "事業所" / "sjis.csv").write_bytes("日付,本文\n".encode("cp932"))
    payloads = [to_payload(s, r) for r in scan(s)]
    assert [p.text for p in payloads] == ["日付,本文\n", "日付,本文\n"]


def test_pdf_comes_as_bytes(sample_settings):
    ref = next(r for r in scan(sample_settings) if r.ext == "pdf")
    p = to_payload(sample_settings, ref)
    assert p.data.startswith(b"%PDF") and p.text is None and not p.unreadable


def test_broken_file_is_marked_unreadable(tmp_path):
    s = s_for(tmp_path)
    (s.drive_root / "事業所" / "b.docx").write_bytes(b"not a zip")
    (s.drive_root / "事業所" / "b.xlsx").write_bytes(b"not a zip")
    (s.drive_root / "事業所" / "b.pdf").write_bytes(b"not a pdf")
    refs = scan(s)
    assert len(refs) == 3
    assert all(to_payload(s, r).unreadable for r in refs)


def test_encrypted_pdf_is_marked_unreadable(tmp_path):
    from pypdf import PdfWriter

    s = s_for(tmp_path)
    w = PdfWriter()
    w.add_blank_page(100, 100)
    w.encrypt("pass")
    with open(s.drive_root / "事業所" / "lock.pdf", "wb") as f:
        w.write(f)
    assert to_payload(s, scan(s)[0]).unreadable
