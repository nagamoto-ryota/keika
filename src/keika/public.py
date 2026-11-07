"""審査用の公開版の守り。合言葉の門、AI の1日の上限、起動のたびに元の DB から始めること。

合言葉と API キーは環境変数（置き場は Secret Manager）で渡し、リポジトリには書かない。
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

COOKIE = "keika_pass"
LIMIT_MESSAGE = "今日の AI の利用の上限に達しました。明日になると使えます"
JST = timezone(timedelta(hours=9))
MAX_MISSES = 30  # 1人が1時間に間違えてよい回数。総当たりを止める
MAX_MISSES_ALL = 1000  # 全体で1時間に間違えてよい回数。1人の間違いで審査員を締め出さないよう、1人分とは別に大きく取る


def pass_value(code: str) -> str:
    """クッキーに入れる値。合言葉そのものは入れない。"""
    return hmac.new(code.encode(), b"keika-public-pass", hashlib.sha256).hexdigest()


def code_ok(given: str, code: str) -> bool:
    return hmac.compare_digest(given.strip().encode(), code.encode())


class Misses:
    """合言葉の間違いを、送ってきた相手ごとに数える。多すぎる相手はしばらく受け付けない。"""

    def __init__(self) -> None:
        self._at: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, who: str, now: float) -> list[float]:
        self._at[who] = [t for t in self._at.get(who, []) if now - t < 3600]
        return self._at[who]

    def too_many(self, who: str = "") -> bool:
        with self._lock:
            now = time.monotonic()
            everyone = sum(len(self._recent(w, now)) for w in list(self._at))
            return len(self._recent(who, now)) >= MAX_MISSES or everyone >= MAX_MISSES_ALL

    def add(self, who: str = "") -> None:
        with self._lock:
            self._at.setdefault(who, []).append(time.monotonic())


def _today_jst() -> str:
    return datetime.now(JST).date().isoformat()


class DailyLimit:
    """client_factory を包み、その日の利用額が上限を超えたら新しく AI を呼ばない。"""

    def __init__(self, factory: Callable[[], Any], *, limit_usd: float, spent: Callable[[], float] | None = None, day: Callable[[], str] = _today_jst):
        from keika.claude import METER

        self.factory, self.limit_usd, self.day = factory, limit_usd, day
        self.spent = spent or (lambda: METER.cost_usd)
        self._lock = threading.Lock()
        self._start: tuple[str, float] | None = None

    def used_today(self) -> float:
        with self._lock:
            today, now = self.day(), self.spent()
            if self._start is None or self._start[0] != today:
                self._start = (today, now if self._start else 0.0)
            return now - self._start[1]

    def check(self) -> None:
        if self.used_today() >= self.limit_usd:
            raise RuntimeError(LIMIT_MESSAGE)

    def __call__(self) -> Any:
        self.check()
        client = self.factory()
        return _Guarded(client, self) if hasattr(client, "beta") else client


class _Guarded:
    """AI を呼ぶたびに上限を確かめる client。1回作った client で何回も呼ぶ処理も止まる。"""

    def __init__(self, client: Any, limit: DailyLimit):
        self._client, self._limit = client, limit
        stream = client.beta.messages.stream

        def guarded_stream(**kw: Any) -> Any:
            limit.check()
            return stream(**kw)

        self.beta = type("Beta", (), {"messages": type("Messages", (), {"stream": staticmethod(guarded_stream)})()})()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def fresh_db(base: Path, target: Path) -> None:
    """審査員が触って汚れても、起動のたびに元の状態から始める。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    for extra in ("-wal", "-shm"):
        Path(str(target) + extra).unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{base}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


GATE_PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ケイカ（審査用）</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main style="max-width:520px;margin:48px auto;padding:0 16px">
<h1>ケイカ</h1>
<p>審査用です。架空データで動いています。</p>
<p>合言葉は、提出フォームの審査員用の欄にあります。</p>
{notice}
<form method="post" action="/enter"><p><label>合言葉 <input name="code" type="password" autocomplete="off" required></label> <button>入る</button></p></form>
</main></body></html>"""


def gate_page(notice: str = "") -> str:
    return GATE_PAGE.format(notice=f'<p class="notice">{notice}</p>' if notice else "")


def app_from_env():
    """Cloud Run で動かす時の入口。合言葉が無ければ起動しない。起動のたびに元の DB から始める。"""
    import os
    from pathlib import Path

    from keika.claude import make_client
    from keika.settings import load_settings
    from keika.web.app import create_app

    code = os.environ.get("KEIKA_ACCESS_CODE", "").strip()
    if not code:
        raise SystemExit("KEIKA_ACCESS_CODE がありません。公開版は合言葉なしでは起動しません。")
    s = load_settings()
    base = os.environ.get("KEIKA_BASE_DB")
    if base:
        fresh_db(Path(base), s.db_path)
    limit = DailyLimit(make_client, limit_usd=float(os.environ.get("KEIKA_DAILY_LIMIT_USD", "10")))
    return create_app(s, client_factory=limit, access_code=code)
