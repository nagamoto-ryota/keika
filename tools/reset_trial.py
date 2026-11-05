"""試用の間に触った分を元に戻す。名前が _try.db のデータベースだけを、用意した元の複製で上書きする。

サーバーは止めなくてよい。デモのデータベースと snapshot は触らない。
使い方: python tools/reset_trial.py data/trial_base.db data/desk_try.db
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def reset(base: Path, target: Path) -> None:
    if not target.name.endswith("_try.db"):
        raise ValueError(f"試用の複製ではないので上書きしません: {target.name}")
    source = sqlite3.connect(f"file:{base.as_posix()}?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()


if __name__ == "__main__":
    reset(Path(sys.argv[1]), Path(sys.argv[2]))
    print("元に戻しました:", sys.argv[2])
