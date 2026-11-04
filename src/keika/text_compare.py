"""記録の確認で、元の文と整えた文の違いを機械で出す。AI は使わない。

ほとんど同じ文は、並べて違う語に印を付ける。話し言葉を大きく整えた文は、全面に印が付いて
何も見えなくなるので、もう片方に無い言葉だけに印を付ける。語尾、記録の定型の言葉、1字だけの違いには付けない。
"""
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

_LETTERS = r"\u4e00-\u9fff々〆ヶ\u30a1-\u30faー-ヿA-Za-zＡ-Ｚａ-ｚ"
_TOKEN = re.compile(r"[\u4e00-\u9fff々〆ヶ]+|[\u3040-\u309f]+|[\u30a1-\u30faー-ヿ]+|[0-9０-９]+|[A-Za-zＡ-Ｚａ-ｚ]+|[^\0]")
_DIGIT = re.compile(r"[0-9０-９]+")
_WORD = re.compile(f"[{_LETTERS}]+")
_TIME_LABEL = re.compile(r"^\[?\d{1,2}:\d{2}(?::\d{2})?\]?[ \t]*(?:話者[ \t]*\d+[ \t]*[:：])?|^話者[ \t]*\d+[ \t]*[:：]", re.M)
_HEAD_LINE = re.compile(r"[^\s:：]{1,12}[:：].*")
_KANJI_DIGITS = str.maketrans("〇一二三四五六七八九", "0123456789")
_NEARLY_SAME = 0.6  # 語の並びがこれだけ一致していれば、整えたのではなく少し直しただけとみる
_STOCK = frozenset(  # 記録の定型の言葉と単位。話に無くても、言い回しの整えなので印を付けない
    "本人 確認 使用 説明 予定 状況 対応 以前 以降 変更 変化 方法 質問 記載 提出 希望 現状 継続 本日 今日 今月 先日 先週 来週 一度 一回 "
    "m km cm kg g メートル キロ 時半 訪問 居宅 在宅 帰宅 面談".split()
)


@dataclass(frozen=True)
class Comparison:
    mode: str  # aligned（並べて比べた） / words（もう片方に無い言葉を見た）
    left: list[tuple[str, bool]]
    right: list[tuple[str, bool]]

    @property
    def legend(self) -> str:
        if not any(changed for _, changed in self.left + self.right):
            return "印を付ける違いはありません。"
        if self.mode == "aligned":
            return "色と下線の所が、元の文と違う箇所です。"
        return "文を大きく整えたので、色と下線は、もう片方の文に無い言葉だけに付けています。語尾や記録の定型の言葉には付けていません。全文を読んで確かめてください。"


def _tokens(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m[0]) for m in _TOKEN.finditer(text) if m[0].strip()]


def _spans(text: str, marks: list[bool]) -> list[tuple[str, bool]]:
    spans: list[tuple[str, bool]] = []
    start = 0
    for i in range(1, len(text) + 1):
        if i == len(text) or marks[i] != marks[start]:
            spans.append((text[start:i], marks[start]))
            start = i
    return spans


def _normal(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _numbers(text: str) -> set[str]:
    return {m.lstrip("0") or "0" for m in re.findall(r"\d+", _normal(text).translate(_KANJI_DIGITS))}


def _mark_aligned(a_tokens, b_tokens, a_marks, b_marks) -> None:
    a_words, b_words = [t[2] for t in a_tokens], [t[2] for t in b_tokens]
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a_words, b_words, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for tokens, marks, lo, hi in ((a_tokens, a_marks, i1, i2), (b_tokens, b_marks, j1, j2)):
            if lo < hi:
                start, end = tokens[lo][0], tokens[hi - 1][1]
                marks[start:end] = [True] * (end - start)


def _mark_missing(text: str, marks: list[bool], other: str) -> None:
    """text の語のうち、other に無い部分に印を付ける。数字は桁まで同じ数があるかを見る（45 が 450 の一部で通らないように）。"""
    other_text = re.sub(r"[\s\0]+", "", _normal(other))
    numbers = _numbers(other.replace("\0", " "))
    for m in _DIGIT.finditer(text):
        if (_normal(m[0]).lstrip("0") or "0") not in numbers:
            marks[m.start():m.end()] = [True] * len(m[0])
    for m in _WORD.finditer(text):
        word, i = m[0], 0
        while i < len(word):
            found = next((j for j in range(len(word), i + 1, -1) if _normal(word[i:j]) in other_text), 0)
            if found:
                i = found
                continue
            end = i + 1  # 無い部分の終わりまで伸ばす
            while end < len(word) and not any(_normal(word[end:j]) in other_text for j in range(len(word), end + 1, -1)):
                end += 1
            if end - i >= 2 and _normal(word[i:end]) not in _STOCK:
                marks[m.start() + i:m.start() + end] = [True] * (end - i)
            i = end


def compare(original: str, body: str) -> Comparison:
    raw = original.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    right = body.replace("\r\n", "\n").replace("\r", "\n")
    left = re.sub(r"\n{2,}", "\n", raw)  # 録音の文字起こしの空行で縦に伸びないように詰める
    left_known = _TIME_LABEL.sub(lambda m: "\0" * len(m[0]), left)  # 経過時間と話者の印は比べない
    left_seen = left_known
    head_end = raw.find("\n\n")
    if head_end > 0 and all(_HEAD_LINE.fullmatch(line) for line in raw[:head_end].split("\n")):
        left_seen = "\0" * head_end + left_known[head_end:]  # 冒頭の「ファイル名: 〜」の欄に印は付けない。照合には使う
    left_marks, right_marks = [False] * len(left), [False] * len(right)
    a_tokens, b_tokens = _tokens(left_seen), _tokens(right)
    ratio = SequenceMatcher(None, [t[2] for t in a_tokens], [t[2] for t in b_tokens], autojunk=False).ratio()
    if ratio >= _NEARLY_SAME or not a_tokens or not b_tokens:
        _mark_aligned(a_tokens, b_tokens, left_marks, right_marks)
        return Comparison("aligned", _spans(left, left_marks), _spans(right, right_marks))
    _mark_missing(right, right_marks, left_known)
    _mark_missing(left_seen, left_marks, right)
    return Comparison("words", _spans(left, left_marks), _spans(right, right_marks))
