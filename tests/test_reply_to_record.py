"""ケイカで作って送ったメールへの返事を、支援経過の案（入口の確認カード）にする。読むのはそのスレッドの返事だけ。"""
import base64

from keika import sent_record
from keika.gmail_draft import Gmail, GmailError
from keika.intake import open_reviews
from keika.outbox import candidates
from keika.records import RecordIn, add_record
from test_outbox import env, made  # noqa: F401  （env は fixture）
from test_reading import FakeClient, answer
from test_sent_to_record import SENT_AT, pressed

REPLY_AT = SENT_AT + 3 * 3600 * 1000  # 17:00
QUOTED = "> 柚木 茂さんの週2回の内容を教えてください。"
REPLY = f"井波様\n訪問は火・金の午前なら可能です。会議は11日14時なら伺えます。\n\n2025年11月6日(木) 14:00 井波 <a@example.com>:\n{QUOTED}"


class FakeGmail:
    """スレッドの中身を持つ偽の Gmail。読んだメールの番号を覚える。"""

    def __init__(self):
        self.drafts, self.threads, self.bodies, self.read = [], {}, {}, []

    def create_draft(self, to, subject, body, attachments=()):
        self.drafts.append((to, subject, body))
        n = len(self.drafts)
        self.threads[f"t{n}"] = [{"id": f"m{n}", "labels": ["DRAFT"], "date": None}]
        return f"m{n}", f"t{n}"

    def thread_messages(self, thread_id):
        if thread_id not in self.threads:
            raise GmailError("404", code=404)
        return self.threads[thread_id]

    def sent_state(self, thread_id):
        msgs = self.threads.get(thread_id)
        if not msgs:
            return "gone", None
        sent = [m for m in msgs if "SENT" in m["labels"]]
        return ("sent", sent[0]["date"]) if sent else ("draft", None)

    def read_message(self, message_id):
        self.read.append(message_id)
        return self.bodies[message_id]

    def send(self, thread_id):
        self.threads[thread_id] = [{"id": "m1", "labels": ["SENT"], "date": SENT_AT}]
        self.bodies["m1"] = {"from": "hello", "to": "sv01@example.com", "subject": "柚木 茂さんの件", "date": SENT_AT, "text": "本文", "attachments": []}

    def reply(self, thread_id, message_id, text=REPLY, attachments=()):
        self.threads[thread_id].append({"id": message_id, "labels": ["INBOX", "UNREAD"], "date": REPLY_AT})
        self.bodies[message_id] = {"from": "デイサービスこなら坂 岩舘 <sv01@example.com>", "subject": "Re: 柚木 茂さんの件", "date": REPLY_AT, "text": text, "attachments": list(attachments)}


def summary(text="訪問は火・金の午前なら可能。会議は11日14時なら出席可。"):
    return FakeClient(answer(sent_record.WireReply(summary=text)))


def reply_cards(c):
    return [it for it in open_reviews(c) if it["source"] == "mail_reply"]


def ready(s, c):
    gm = FakeGmail()
    pressed(s, c, gm)
    gm.send("t1")
    sent_record.check(c, s, gm, summary())
    return gm


def test_reply_in_the_thread_becomes_one_card_for_that_person(env):
    s, c = env
    gm = ready(s, c)
    gm.reply("t1", "r1")
    client = summary()
    assert sent_record.check(c, s, gm, client) == 1
    (card,) = reply_cards(c)
    e = card["entry"]
    assert e["user_id"] == "U04" and e["kind_read"] == "文書・FAX・メール" and e["event_time"] == "17:00"
    assert e["body"].startswith("11/6 デイサービスこなら坂") and "から返信。" in e["body"] and "火・金の午前" in e["body"]
    assert "AI の要約" in e["reason"]
    assert sent_record.check(c, s, gm, summary()) == 0 and len(reply_cards(c)) == 1  # 同じ返事で2回作らない


def test_quoted_old_mail_is_not_sent_to_the_ai(env):
    s, c = env
    gm = ready(s, c)
    gm.reply("t1", "r1")
    client = summary()
    sent_record.check(c, s, gm, client)
    said = str(client.calls[0]["messages"])
    assert "火・金の午前" in said and "週2回の内容を教えて" not in said


def test_only_replies_in_keika_threads_are_read(env):
    s, c = env
    gm = ready(s, c)
    gm.threads["other"] = [{"id": "x1", "labels": ["INBOX"], "date": REPLY_AT}]  # ケイカと関係ないメール
    gm.reply("t1", "r1")
    gm.read.clear()
    sent_record.check(c, s, gm, summary())
    assert gm.read == ["r1"]


def test_our_own_sent_mail_is_not_a_reply(env):
    s, c = env
    gm = ready(s, c)
    gm.read.clear()
    gm.threads["t1"].append({"id": "m2", "labels": ["SENT"], "date": REPLY_AT})
    assert sent_record.check(c, s, gm, summary()) == 0 and gm.read == []


def test_without_ai_the_reply_waits_for_next_time(env):
    s, c = env
    gm = ready(s, c)
    gm.reply("t1", "r1")
    assert sent_record.check(c, s, gm, None) == 0 and reply_cards(c) == []
    assert sent_record.check(c, s, gm, summary()) == 1


def test_summary_crossing_the_line_is_not_used(env):
    s, c = env
    gm = ready(s, c)
    gm.reply("t1", "r1")
    sent_record.check(c, s, gm, summary("すぐに受診してください。"))
    body = reply_cards(c)[0]["entry"]["body"]
    assert "受診" not in body and "元のメールで確かめてください" in body


def test_attachment_names_of_the_reply_go_into_the_card(env):
    s, c = env
    gm = ready(s, c)
    gm.reply("t1", "r1", attachments=["訪問看護計画書.pdf"])
    sent_record.check(c, s, gm, summary())
    assert "添付: 訪問看護計画書.pdf" in reply_cards(c)[0]["entry"]["body"]


def test_reply_records_never_propose_another_mail(env):
    s, c = env
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-06", kind_read="文書・FAX・メール", body="岩舘さんから返信。確認して連絡する。", source="intake:mail_reply"))
    assert all(r.source != "intake:mail_reply" for r in candidates(c, limit=100))


def _b64(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def test_read_message_takes_plain_text_and_attachment_names():
    seen = []
    full = {"internalDate": str(REPLY_AT), "payload": {"mimeType": "multipart/mixed", "headers": [{"name": "From", "value": "岩舘 <sv01@example.com>"}, {"name": "Subject", "value": "Re: 件名"}],
            "parts": [{"mimeType": "multipart/alternative", "parts": [{"mimeType": "text/plain", "body": {"data": _b64("本文です。")}}, {"mimeType": "text/html", "body": {"data": _b64("<p>本文です。</p>")}}]},
                      {"mimeType": "application/pdf", "filename": "計画書.pdf", "body": {"attachmentId": "a1"}}]}}

    def get(url, headers=None):
        seen.append(url)
        return full

    gm = Gmail("c", "s", "r", post=lambda url, **k: {"access_token": "AT"}, get=get)
    got = gm.read_message("r1")
    assert got == {"from": "岩舘 <sv01@example.com>", "to": "", "subject": "Re: 件名", "date": REPLY_AT, "text": "本文です。", "attachments": ["計画書.pdf"]}
    assert seen[0].startswith("https://gmail.googleapis.com/gmail/v1/users/me/messages/r1?") and "format=full" in seen[0]


def test_thread_messages_lists_ids_labels_and_dates():
    def get(url, headers=None):
        return {"messages": [{"id": "m1", "labelIds": ["SENT"], "internalDate": str(SENT_AT)}, {"id": "r1", "labelIds": ["INBOX"], "internalDate": str(REPLY_AT)}]}

    gm = Gmail("c", "s", "r", post=lambda url, **k: {"access_token": "AT"}, get=get)
    assert gm.thread_messages("t1") == [{"id": "m1", "labels": ["SENT"], "date": SENT_AT}, {"id": "r1", "labels": ["INBOX"], "date": REPLY_AT}]
