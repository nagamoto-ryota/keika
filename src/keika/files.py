"""フォルダの走査と文字化。読めない書類があっても全体を止めず、その書類を「読めない」にする。"""
from __future__ import annotations

import csv
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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
_VIEW_SCANS: ContextVar[dict[str, list["DocRef"]] | None] = ContextVar("view_scans", default=None)
_HASHES: dict[tuple[str, int, int], str] = {}  # 変わっていないファイルは計算し直さない
TEXT_EXTS = {"txt", "csv", "srt", "vtt", "md"}
OFFICE_EXTS = {"docx", "xlsx"}
BINARY_EXTS = {"pdf", "png", "jpg", "jpeg"}
UNREADABLE = "unreadable:"
MAX_BYTES = 30 * 1024 * 1024  # API の上限（1回 32MB）より少し小さく
IMAGE_MAGIC = {"png": b"\x89PNG", "jpg": b"\xff\xd8", "jpeg": b"\xff\xd8"}
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


@contextmanager
def one_view() -> Iterator[None]:
    """画面の1回の表示の間だけ、ファイルの一覧を使い回す。既定は無効で、測定や裏の処理には効かない。
    ContextVar なので、別のスレッドや次の表示には持ち越さない。"""
    token = _VIEW_SCANS.set({})
    try:
        yield
    finally:
        _VIEW_SCANS.reset(token)


def scan(s: Settings) -> list[DocRef]:
    memo = _VIEW_SCANS.get()
    if memo is None:
        return _walk(s)
    key = str(s.drive_root)
    if key not in memo:
        memo[key] = _walk(s)
    return list(memo[key])


def _walk(s: Settings) -> list[DocRef]:
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
            try:
                sha = _hash(path)
            except OSError:  # 使用中や同期中で開けない。次に見た時にまた読む
                sha = f"{UNREADABLE}{rel}"
            refs.append(DocRef(rel, sha, ext, scope, user_folder))
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
        if len(raw) > MAX_BYTES:
            raise ValueError("大きすぎて読めない")
        if ref.ext == "pdf":
            _check_pdf(raw)
        elif not raw.startswith(IMAGE_MAGIC[ref.ext]):
            raise ValueError("画像として開けない")
        return Payload(data=raw, media_type=MEDIA_TYPES[ref.ext])
    except Exception as e:  # 壊れた書類の失敗の形は様々。1件で全体を止めない
        return Payload(unreadable=True, reason=f"{type(e).__name__}: {e}")


def parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _hash(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _HASHES:
        _HASHES[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _HASHES[key]
