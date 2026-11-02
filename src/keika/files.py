"""フォルダの走査と文字化。読めない書類があっても全体を止めず、その書類を「読めない」にする。"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import docx
import openpyxl
import pypdf
from docx.table import Table

from keika.settings import OutsideRootError, Settings, safe_path

SCOPES = {"利用者": "user", "事業所": "office", "入口": "inbox"}
TEXT_EXTS = {"txt", "csv"}
OFFICE_EXTS = {"docx", "xlsx"}
BINARY_EXTS = {"pdf", "png", "jpg", "jpeg"}
OLD_EXTS = {"doc", "xls"}  # 文字にできないが「読めない」と知らせるために拾う
ALL_EXTS = TEXT_EXTS | OFFICE_EXTS | BINARY_EXTS | OLD_EXTS
MEDIA_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


@dataclass(frozen=True)
class DocRef:
    rel_path: str  # 読み取りの根からの相対パス（区切りは /）
    sha256: str
    ext: str
    scope: str  # user / office / inbox
    user_folder: str | None


@dataclass(frozen=True)
class Payload:
    text: str | None = None  # docx・xlsx・csv・txt
    data: bytes | None = None  # PDF・画像はそのまま Claude に渡す
    media_type: str | None = None
    unreadable: bool = False
    reason: str | None = None


def scan(s: Settings) -> list[DocRef]:
    refs = []
    root = s.drive_root.resolve()
    for folder, scope in SCOPES.items():
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            ext = path.suffix.lower().lstrip(".")
            if not path.is_file() or ext not in ALL_EXTS or path.name.startswith(("~$", ".")):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                safe_path(s, rel)
            except OutsideRootError:
                continue
            parts = Path(rel).parts
            user_folder = parts[1] if scope == "user" and len(parts) > 2 else None
            refs.append(DocRef(rel, hashlib.sha256(path.read_bytes()).hexdigest(), ext, scope, user_folder))
    return refs


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("文字コードが分からない")


def _docx_text(path: Path) -> str:
    lines = []
    for block in docx.Document(path).iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                cells: list[str] = []
                for cell in row.cells:
                    text = cell.text.strip().replace("\n", " / ")
                    if not cells or cells[-1] != text:  # 結合セルは同じ文字が並ぶ
                        cells.append(text)
                lines.append("| " + " | ".join(cells) + " |")
        elif block.text.strip():
            lines.append(block.text)
    return "\n".join(lines)


def _xlsx_text(path: Path) -> str:
    lines = []
    for ws in openpyxl.load_workbook(path, data_only=True):
        lines.append(f"## シート: {ws.title}")
        for row in ws.iter_rows():
            cells = [f"{c.coordinate}={c.value}" for c in row if c.value not in (None, "")]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _check_pdf(raw: bytes) -> None:
    reader = pypdf.PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError("パスワードが掛かっている")
    if len(reader.pages) == 0:
        raise ValueError("ページが無い")


def to_payload(s: Settings, ref: DocRef) -> Payload:
    path = safe_path(s, ref.rel_path)
    try:
        if ref.ext in OLD_EXTS:
            raise ValueError("古い形式（.doc / .xls）は読めない")
        if ref.ext == "docx":
            return Payload(text=_docx_text(path))
        if ref.ext == "xlsx":
            return Payload(text=_xlsx_text(path))
        raw = path.read_bytes()
        if ref.ext in TEXT_EXTS:
            return Payload(text=decode_text(raw))
        if ref.ext == "pdf":
            _check_pdf(raw)
        return Payload(data=raw, media_type=MEDIA_TYPES[ref.ext])
    except Exception as e:  # 壊れた書類の失敗の形は様々。1件で全体を止めない
        return Payload(unreadable=True, reason=f"{type(e).__name__}: {e}")


def parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))
