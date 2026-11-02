"""設定、基準日、読み取りの根。

アプリが読むのは drive_root の中だけ。答え合わせ表など外のファイルには触れない。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


class OutsideRootError(ValueError):
    """設定したフォルダの外を読もうとした。"""


@dataclass(frozen=True)
class Settings:
    drive_root: Path
    db_path: Path
    as_of: date | None = None  # デモ専用の基準日。記入日時には影響しない


def load_settings() -> Settings:
    as_of = os.environ.get("KEIKA_AS_OF")
    return Settings(
        drive_root=Path(os.environ.get("KEIKA_DRIVE_ROOT", "sample_data/drive")),
        db_path=Path(os.environ.get("KEIKA_DB_PATH", "data/keika.db")),
        as_of=date.fromisoformat(as_of) if as_of else None,
    )


def today(s: Settings) -> date:
    return s.as_of or date.today()


def now_real() -> datetime:
    """記入日時に使う実際の時計。基準日の影響を受けない。"""
    return datetime.now().astimezone()


def safe_path(s: Settings, rel: str) -> Path:
    root = s.drive_root.resolve()
    path = (root / rel).resolve()  # シンボリックリンクも実体に解決される
    if not path.is_relative_to(root):
        raise OutsideRootError(f"読み取りの根の外です: {rel}")
    return path
