"""レビューの指摘（11/6）の直し: 二重に動かない、6通目以降も見る、Gmail で実際に送った件名と添付を使う、
許された権限を覚える、空の返事・読めない返事を AI に回さない、文字コード、引用の切り方、記録の元の見分け。"""
import base64
import json

import pytest

from keika import gmail_draft, sent_record
from keika.claude import StructuredError
from keika.gmail_draft import Gmail
from keika.intake import open_reviews
from keika.outbox import candidates
from keika.records import RecordIn, add_record
from keika.settings import now_real
from test_outbox import env, made  # noqa: F401  （env は fixture）
from test_reading import FakeClient, answer
from test_reply_to_record import REPLY_AT, FakeGmail as ReplyGmail, ready, summary
from test_sent_to_record import SENT_AT


class FakeGmail(ReplyGmail):
    def send(self, thread_id, to="sv01@example.com", subject="柚木 茂さんの件", attachments=()):
        super().send(thread_id)
        self.bodies["m1"] = {"from": "hello", "to": to, "subject": subject, "date": SENT_AT, "text": "本文", "attachments": list(attachments)}


def cards(c, source):
    return [it for it in open_reviews(c) if it["source"] == source]


def put_sent_mail(c, mail_id, seen=True, at=None):
    """送った（seen=True）か、下書きのまま（False）のメールを1通、表に直接入れる。"""
    at = at or now_real().isoformat()
    c.execute("INSERT INTO mail_out (mail_id, user_id, record_id, recipient_id, purpose, ground, origin, state, sent_at, sent_to, sent_name, sent_subject, sent_body, created_at)"
              " VALUES (?, 'U04', 1, 'SV01', '照会', '', 'proposal', 'sent', ?, 'sv01@example.com', 'デイサービスこなら坂', ?, '本文', ?)", (mail_id, at, f"件名{mail_id}", at))
    c.execute("INSERT INTO mail_gmail (mail_id, message_id, at) VALUES (?, ?, ?)", (mail_id, f"m{mail_id}", at))
    c.execute("INSERT INTO mail_gmail_thread (mail_id, thread_id) VALUES (?, ?)", (mail_id, f"t{mail_id}"))
    if seen:
        c.execute("INSERT INTO mail_sent_seen (mail_id, result, review_id, at) VALUES (?, 'sent', NULL, ?)", (mail_id, at))
    c.commit()


# --- 1. 二重に動かない ---


def test_second_run_while_one_is_running_does_nothing(env):
    s, c = env
    gm = FakeGmail()
    pressed(s, c, gm)
    gm.send("t1")
    with sent_record.RUNNING:
        assert sent_record.check(c, s, gm, summary()) == 0
    assert cards(c, "mail") == []
    assert sent_record.check(c, s, gm, summary()) == 1


def pressed(s, c, gm):
    from test_sent_to_record import pressed as press

    return press(s, c, gm)


# --- 2, 3. 6通目以降も見る ---


def test_replies_to_older_mails_are_still_read(env):
    s, c = env
    gm = FakeGmail()
    for n in range(1, 8):
        put_sent_mail(c, 100 + n)
        gm.threads[f"t{100 + n}"] = [{"id": f"m{100 + n}", "labels": ["SENT"], "date": SENT_AT}]
    gm.reply("t101", "r101")  # 一番古いメールへの返事
    assert sent_record.check(c, s, gm, summary()) == 1 and gm.read == ["r101"]


def test_old_sent_draft_is_not_stuck_behind_newer_unsent_ones(env):
    s, c = env
    gm = FakeGmail()
    put_sent_mail(c, 200, seen=False, at="2026-11-01T09:00:00")
    gm.threads["t200"] = [{"id": "m200", "labels": ["SENT"], "date": SENT_AT}]
    gm.bodies["m200"] = {"from": "hello", "to": "sv01@example.com", "subject": "件名200", "date": SENT_AT, "text": "本文", "attachments": []}
    for n in range(201, 208):
        put_sent_mail(c, n, seen=False)
        gm.threads[f"t{n}"] = [{"id": f"m{n}", "labels": ["DRAFT"], "date": None}]
    assert sent_record.check(c, s, gm, None) == 1
    assert "件名「件名200」" in cards(c, "mail")[0]["entry"]["body"]


# --- 4. Gmail で実際に送った件名と添付 ---


def test_card_uses_what_gmail_actually_sent(env):
    s, c = env
    gm = FakeGmail()
    pressed(s, c, gm)
    gm.send("t1", to="me@another.example", subject="件名を直して送った", attachments=["計画書.pdf"])
    sent_record.check(c, s, gm, None)
    body = cards(c, "mail")[0]["entry"]["body"]
    assert "件名「件名を直して送った」" in body and "添付: 計画書.pdf" in body and "宛先は Gmail で変えて送りました" in body
    assert "me@another.example" not in body


def test_card_falls_back_to_keikas_copy_when_the_sent_mail_cannot_be_read(env):
    s, c = env
    gm = FakeGmail()
    pressed(s, c, gm)
    gm.send("t1")
    del gm.bodies["m1"]
    gm.read_message = lambda mid: (_ for _ in ()).throw(gmail_draft.GmailError("403", code=403))
    sent_record.check(c, s, gm, None)
    assert "件名「柚木 茂さんの件」" in cards(c, "mail")[0]["entry"]["body"]


# --- 5. 許された権限を覚える ---


def test_token_keeps_the_scopes_google_actually_granted(tmp_path):
    from test_gmail_draft import _browser, _settings

    s = _settings(tmp_path)
    gmail_draft.connect(s, open_browser=_browser(), post=lambda *a, **k: {"refresh_token": "RT", "scope": gmail_draft.SCOPE})
    assert gmail_draft.needs_reconnect(s)
    gmail_draft.connect(s, open_browser=_browser(), post=lambda *a, **k: {"refresh_token": "RT", "scope": " ".join(gmail_draft.SCOPES)})
    assert not gmail_draft.needs_reconnect(s)


# --- 6, 7. 空の返事・AI が読めない返事 ---


def test_empty_reply_is_not_sent_to_the_ai(env):
    s, c = env
    gm = ready(s, c)
    gm.reply("t1", "r1", text="> 前のメールだけ")
    client = summary()
    assert sent_record.check(c, s, gm, client) == 1 and client.calls == []
    assert "元のメールで確かめてください" in cards(c, "mail_reply")[0]["entry"]["body"]


def test_reply_the_ai_cannot_read_becomes_a_plain_card_once(env, monkeypatch):
    s, c = env
    gm = ready(s, c)
    gm.reply("t1", "r1")

    def broken(*a, **k):
        raise StructuredError("断った")

    monkeypatch.setattr(sent_record, "ask_structured", broken)
    assert sent_record.check(c, s, gm, summary()) == 1
    assert "元のメールで確かめてください" in cards(c, "mail_reply")[0]["entry"]["body"]
    assert sent_record.check(c, s, gm, summary()) == 0


# --- 8. 文字コード と HTML だけの返事 ---


def _gm_with(payload):
    return Gmail("c", "s", "r", post=lambda url, **k: {"access_token": "AT"}, get=lambda url, headers=None: {"internalDate": str(REPLY_AT), "payload": payload})


def test_reply_in_iso_2022_jp_is_read_correctly():
    data = base64.urlsafe_b64encode("火曜の午前なら伺えます。".encode("iso-2022-jp")).decode()
    got = _gm_with({"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": 'text/plain; charset="ISO-2022-JP"'}], "body": {"data": data}}).read_message("r1")
    assert got["text"] == "火曜の午前なら伺えます。"


def test_html_only_reply_is_read_as_text():
    data = base64.urlsafe_b64encode("<div>火曜の<b>午前</b>なら<br>伺えます。</div>".encode()).decode()
    got = _gm_with({"mimeType": "text/html", "headers": [], "body": {"data": data}}).read_message("r1")
    assert "火曜の午前なら" in got["text"] and "伺えます。" in got["text"] and "<" not in got["text"]


# --- 9. Outlook の形の引用 ---


def test_outlook_style_quote_is_cut():
    text = "火曜の午前なら伺えます。\n\n差出人: 井波 拓真\n送信日時: 2026年11月6日 14:00\n件名: 柚木 茂さんの件\n柚木 茂さんの週2回の内容を教えてください。"
    assert sent_record.own_part(text) == "火曜の午前なら伺えます。"
    assert sent_record.own_part("了解です。\n-----Original Message-----\nFrom: x") == "了解です。"


# --- 11. 記録の元の見分け ---


def test_only_mail_records_are_left_out_of_mail_proposals(env):
    s, c = env
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-06", kind_read="電話", body="能登さんに確認することにした。", source="intake:mail_memo.txt"))
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-06", kind_read="文書・FAX・メール", body="能登さんに確認する。", source="intake:mail"))
    sources = {r.source for r in candidates(c, limit=100)}
    assert "intake:mail_memo.txt" in sources and "intake:mail" not in sources


# --- 呼ぶ回数 ---


def test_access_token_is_reused_within_a_run():
    tokens = []

    def post(url, **k):
        tokens.append(url)
        return {"access_token": "AT", "expires_in": 3599}

    gm = Gmail("c", "s", "r", post=post, get=lambda url, headers=None: {"messages": []})
    gm.thread_messages("t1")
    gm.thread_messages("t2")
    assert len(tokens) == 1
