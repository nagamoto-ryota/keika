"""提出前の確認: 公開リポに会話ログ、実データ、API キーが入っていないかを追跡中のファイルで調べる。

  python tools/check_public.py [禁止語のファイル]

禁止語のファイルは1行1語（実在の人名や事業所名など）。リポの外に置く。見つかったら終了コード 1。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
KEY = re.compile(rb"sk-ant-[A-Za-z0-9_-]{10,}|ANTHROPIC_API_KEY=[A-Za-z0-9_-]{20,}")
LOG_NAMES = re.compile(r"\.jsonl$|transcript|(^|/)\.env$|\.credentials|(^|/)data/")
PHONE = re.compile(rb"(?<![0-9-])0(?!00)\d{1,4}-\d{1,4}-\d{4}(?!\d)")  # 架空の電話番号は 000- で始める


def problems(files: list[str], banned: list[str], root: Path = ROOT) -> list[str]:
    found = []
    for name in files:
        if LOG_NAMES.search(name):
            found.append(f"{name}: 入れてはいけない種類のファイル名")
        path = root / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if KEY.search(data):
            found.append(f"{name}: API キーの形の文字列")
        if name.endswith((".md", ".txt", ".csv", ".py", ".yaml", ".html", ".json", ".srt", ".vtt")) and "legal/" not in name and PHONE.search(data):
            found.append(f"{name}: 000- 以外の電話番号の形")
        found += [f"{name}: 禁止語「{w}」" for w in banned if w.encode("utf-8") in data]
    return found


def main() -> int:
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout.splitlines()
    banned = Path(sys.argv[1]).read_text(encoding="utf-8").split() if len(sys.argv) > 1 else []
    found = problems(files, banned)
    print("\n".join(found) if found else f"問題なし（{len(files)} ファイル、禁止語 {len(banned)} 語）")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
