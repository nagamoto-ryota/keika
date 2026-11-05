"""Gmail から実際に送られたメールだけを、支援経過の案（入口の確認カード）にする。本物の API は呼ばない。"""
import json

from fastapi.testclient import TestClient

from keika import gmail_draft, sent_record
from keika.gmail_draft import Gmail, GmailError
from keika.intake import open_reviews
from keika.outbox import candidates, sent_list
from keika.web.app import create_app
from test_outbox import env, made  # noqa: F401  （env は fixture）

SENT_AT = 1762405200000  # 2025-11-06 14:00 JST（ミリ秒）


class FakeGmail:
    """下書きを作り、スレッドの状態を返すだけ。state は draft / sent / gone。"""

    def __init__(self):
        self.drafts, self.state, self.asked = [], {}, []

    def create_draft(self, to, subject, body, attachments=()):
        self.drafts.append((to, subject, body, list(attachments)))
        n = len(self.drafts)
        self.state[f"t{n}"] = ("draft", None)
        return f"m{n}", f"t{n}"

    def sent_state(self, thread_id):
        self.asked.append(thread_id)
        return self.state.get(thread_id, ("gone", None))


def pressed(s, c, gm, **kw):
    i = made(s, c, **kw)
    TestClient(create_app(s, gmail_factory=lambda: gm)).post(f"/desk/U04/mail/{i}/send")
    assert len(sent_list(c)) >= 1
    return i


def mail_cards(c):
    return [it for it in open_reviews(c) if it["source"] == "mail"]


# --- 送ったかの見分け ---


def test_draft_still_in_gmail_makes_nothing(env):
    s, c = env
    gm = FakeGmail()
    pressed(s, c, gm)
    assert sent_record.check(c, s, gm) == 0 and mail_cards(c) == []


def test_deleted_draft_is_not_treated_as_sent_and_is_not_asked_again(env):
    s, c = env
    gm = FakeGmail()
    pressed(s, c, gm)
    gm.state["t1"] = ("gone", None)
    assert sent_record.check(c, s, gm) == 0 and mail_cards(c) == []
    gm.state["t1"] = ("sent", SENT_AT)  # 後から送信済みに見えても、消えた扱いのまま
    assert sent_record.check(c, s, gm) == 0 and gm.asked.count("t1") == 1


def test_sent_mail_becomes_one_card_for_that_person(env):
    s, c = env
    gm = FakeGmail()
    pressed(s, c, gm)
    gm.state["t1"] = ("sent", SENT_AT)
    assert sent_record.check(c, s, gm) == 1
    (card,) = mail_cards(c)
    e = card["entry"]
    assert e["user_id"] == "U04" and e["kind_read"] == "文書・FAX・メール" and e["event_date"] == "2025-11-06" and e["event_time"] == "14:00"
    assert "デイサービスこなら坂" in e["body"] and "メールで照会" in e["body"] and "件名「柚木 茂さんの件」" in e["body"]
    assert e["reason"]  # 理由は最初から入れる（AI を呼ばない）
    assert sent_record.check(c, s, gm) == 0 and len(mail_cards(c)) == 1  # 同じメールで2回作らない


def test_attachment_names_go_into_the_card(env):
    s, c = env
    path = s.drive_root / "利用者/U04_柚木茂/アセスメント_2025-11-20.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 x")
    gm = FakeGmail()
    pressed(s, c, gm, body="デイサービスこなら坂 岩舘様\n柚木 茂さんのアセスメントを添付します。")
    gm.state["t1"] = ("sent", SENT_AT)
    sent_record.check(c, s, gm)
    assert "添付: アセスメント_2025-11-20.pdf" in mail_cards(c)[0]["entry"]["body"]


def test_without_gmail_nothing_happens(env):
    s, c = env
    gm = FakeGmail()
    pressed(s, c, gm)
    gm.state["t1"] = ("sent", SENT_AT)
    assert sent_record.check(c, s, None) == 0 and mail_cards(c) == []


def test_mail_app_copies_are_never_checked(env):
    s, c = env
    i = made(s, c)
    TestClient(create_app(s)).post(f"/desk/U04/mail/{i}/send")  # つないでいない時の控え
    gm = FakeGmail()
    assert sent_record.check(c, s, gm) == 0 and gm.asked == []


def test_trouble_with_gmail_keeps_it_for_next_time(env):
    s, c = env
    gm = FakeGmail()
    pressed(s, c, gm)

    def broken(thread_id):
        raise GmailError("接続できません")

    gm.sent_state = broken
    assert sent_record.check(c, s, gm) == 0
    gm.sent_state = lambda t: ("sent", SENT_AT)
    assert sent_record.check(c, s, gm) == 1


# --- 入口と記録 ---


def test_home_checks_in_the_background_and_shows_the_card(env):
    s, c = env
    gm = FakeGmail()
    pressed(s, c, gm)
    gm.state["t1"] = ("sent", SENT_AT)
    client = TestClient(create_app(s, gmail_factory=lambda: gm))
    client.get("/")
    html = client.get("/").text
    assert "柚木 茂さんの記録にします。" in html and "件名「柚木 茂さんの件」" in html


def test_ok_puts_it_into_that_persons_record_and_it_never_proposes_a_mail(env):
    s, c = env
    gm = FakeGmail()
    pressed(s, c, gm)
    gm.state["t1"] = ("sent", SENT_AT)
    sent_record.check(c, s, gm)
    card = mail_cards(c)[0]
    TestClient(create_app(s, gmail_factory=lambda: gm)).post(f"/review/{card['id']}", data={"action": "save", "user_id": "U04"})
    row = c.execute("SELECT user_id, body, source FROM records WHERE source = 'intake:mail'").fetchone()
    assert row["user_id"] == "U04" and "メールで照会" in row["body"]
    assert all(r.source != "intake:mail" for r in candidates(c, limit=100))


def test_screen_says_what_gmail_adds(env):
    s, c = env
    i = made(s, c)
    html = TestClient(create_app(s)).get(f"/desk/U04/mail/{i}").text
    assert "Gmail につなぐと、送ったメールが支援経過の案になります" in html


# --- Gmail の聞き方 ---


def _gm(responses, seen):
    def get(url, headers=None):
        seen.append(url)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    return Gmail("c", "s", "r", post=lambda url, **k: {"access_token": "AT"}, get=get)


def test_sent_state_reads_only_ids_and_labels_of_the_thread():
    seen = []
    gm = _gm([{"messages": [{"id": "m1", "labelIds": ["SENT"], "internalDate": str(SENT_AT)}]}], seen)
    assert gm.sent_state("t1") == ("sent", SENT_AT)
    assert seen[0].startswith("https://gmail.googleapis.com/gmail/v1/users/me/threads/t1?")
    assert "format=minimal" in seen[0] and "fields=messages%28id%2ClabelIds%2CinternalDate%29" in seen[0]


def test_sent_state_draft_and_gone():
    assert _gm([{"messages": [{"id": "m1", "labelIds": ["DRAFT"]}]}], []).sent_state("t1") == ("draft", None)
    assert _gm([GmailError("404 Not Found", code=404)], []).sent_state("t1") == ("gone", None)


def test_scope_adds_only_readonly():
    assert gmail_draft.SCOPES == ["https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.readonly"]
    url = gmail_draft.auth_url("cid", "http://127.0.0.1:1/", "ch", "st")
    assert "gmail.readonly" in url and "gmail.modify" not in url and "mail.google.com%2F" not in url and "gmail.send" not in url


def test_token_without_read_is_asked_to_connect_again(tmp_path):
    from datetime import date

    from keika.settings import Settings

    s = Settings(drive_root=tmp_path, db_path=tmp_path / "x.db", as_of=date(2026, 11, 13))
    (tmp_path / "gmail_client.json").write_text(json.dumps({"installed": {"client_id": "c", "client_secret": "s"}}), encoding="utf-8")
    (tmp_path / "gmail_token.json").write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
    assert gmail_draft.connected(s) is not None and gmail_draft.needs_reconnect(s)
    gmail_draft.save_token(s, "r2")
    assert not gmail_draft.needs_reconnect(s)
