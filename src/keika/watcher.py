"""見張りフォルダ。Drive の入口/ に置かれた文字起こしを自動で拾う。

元のファイルは消さない。取り込んだ中身を覚えて二重に取り込まない。同期の途中のファイルは落ち着いてから読む。
Drive の同期先ではファイルの変更通知が当てにならないので、一定間隔で見に行く。
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from keika.db import connect
from keika.files import _hash, scan, to_payload
from keika.intake import process_text, to_review
from keika.settings import Settings, now_real

SETTLE_SECONDS = 20
RECHECK_SECONDS = 0.5


def date_hint(name: str) -> str | None:
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", name)
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def handle_file(conn: sqlite3.Connection, client: Any, s: Settings, path: Path, on_saved: Callable[[str], None] | None = None) -> bool:
    """取り込んだら True。同期の途中、または取り込み済みなら False。"""
    before = path.stat()
    if time.time() - before.st_mtime < SETTLE_SECONDS:
        return False
    time.sleep(RECHECK_SECONDS)  # 元の日時を保つコピーでも、大きさが動いていれば待つ
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return False
    sha = _hash(path)
    if conn.execute("SELECT 1 FROM intake_files WHERE sha256 = ?", (sha,)).fetchone():
        return False
    rel = path.resolve().relative_to(s.drive_root.resolve()).as_posix()
    ref = next((r for r in scan(s) if r.rel_path == rel), None)
    if ref is None:
        return False
    payload = to_payload(s, ref)
    if payload.text is None:
        to_review(conn, f"（{path.name}）", path.name, {}, [], f"{path.name} は文字として読めませんでした。文字起こしを置いてください。")
    else:
        process_text(conn, client, s, payload.text, path.name, date_hint=date_hint(path.name), on_saved=on_saved)
    conn.execute("INSERT INTO intake_files (sha256, rel_path, taken_at) VALUES (?, ?, ?)", (sha, rel, now_real().isoformat()))
    conn.commit()
    return True


def scan_inbox(conn: sqlite3.Connection, client: Any, s: Settings, on_saved: Callable[[str], None] | None = None) -> int:
    """取りこぼしも含めて入口を一通り見る。取り込んだ数を返す。"""
    count = 0
    for ref in scan(s):
        if ref.scope == "inbox" and handle_file(conn, client, s, s.drive_root / ref.rel_path, on_saved=on_saved):
            count += 1
    return count


def start_watcher(s: Settings, client_factory: Callable[[], Any], on_saved: Callable[[str], None] | None = None, interval: float = 30.0) -> threading.Event:
    """裏で入口を見張る。返した Event を set すると止まる。"""
    stop = threading.Event()

    def loop() -> None:
        conn = connect(s)
        while not stop.is_set():
            try:
                scan_inbox(conn, client_factory(), s, on_saved=on_saved)
            except Exception:  # キーが無い、通信切れ、同期で開けない等。止まらず次の回にまた見る
                pass
            stop.wait(interval)
        conn.close()

    threading.Thread(target=loop, daemon=True, name="keika-watcher").start()
    return stop
