"""Claude 呼び出しの共通部。決まった形で受け取り、費用を数える。"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# 1M トークンあたりの米ドル（入力、出力、キャッシュ読み、キャッシュ書き）
PRICE = (4.00, 20.00, 0.20, 5.00)


class StructuredError(Exception):
    """決まった形の答えが得られなかった（形の崩れ、断り、通信の失敗）。"""


@dataclass
class UsageMeter:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __post_init__(self):
        self._lock = threading.Lock()

    def add(self, usage: Any) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += usage.input_tokens or 0
            self.output_tokens += usage.output_tokens or 0
            self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0

    @property
    def cost_usd(self) -> float:
        i, o, r, w = PRICE
        return (self.input_tokens * i + self.output_tokens * o + self.cache_read * r + self.cache_write * w) / 1_000_000


METER = UsageMeter()


def load_api_key(base: Path | None = None) -> str | None:
    """アプリのフォルダの .env を先に見る。作品専用のキーを他の設定より優先するため。"""
    env_file = (base or Path.cwd()) / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == "ANTHROPIC_API_KEY":
                return value.strip().strip("\"'")
    return os.environ.get("ANTHROPIC_API_KEY")


def make_client(base: Path | None = None) -> anthropic.Anthropic:
    key = load_api_key(base)
    if not key:
        raise RuntimeError("API キーがありません。アプリのフォルダの .env に ANTHROPIC_API_KEY=... を書いてください。")
    return anthropic.Anthropic(api_key=key)


def ask_structured(
    client: Any,
    system: str | list[dict],
    content: list[dict],
    schema: type[BaseModel],
    *,
    effort: str = "medium",
    max_tokens: int = 16000,
) -> BaseModel:
    """決まった形で答えを受け取る。形が崩れたら1回だけやり直し、だめなら StructuredError。"""
    last: Exception | None = None
    for _ in range(2):
        try:
            with client.beta.messages.stream(
                model=MODEL,
                max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                output_format=schema,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=system,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                message = stream.get_final_message()
        except anthropic.APIError as e:  # 通信切れや 429 は SDK が既に再試行している
            raise StructuredError(f"API の失敗: {type(e).__name__}") from e
        except ValueError as e:  # JSON や型の崩れ
            last = e
            continue
        METER.add(message.usage)
        if message.stop_reason == "refusal":
            raise StructuredError("Claude が読み取りを断った")
        if message.parsed_output is None:
            last = ValueError(f"答えが得られなかった: {message.stop_reason}")
            continue
        return message.parsed_output
    raise StructuredError(str(last))
