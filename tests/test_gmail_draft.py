"""Gmail の下書きに入れる。範囲は gmail.compose だけで、送る機能は無い。本物の API は呼ばない。"""
import base64
import email
import email.policy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from keika import gmail_draft
from keika.gmail_draft import SCOPE, Gmail, GmailError, auth_url, connected, open_url, save_token
from keika.outbox import edit_mail, sent_list
from keika.web.app import create_app
from test_outbox import env, made  # noqa: F401  （env は fixture）


class FakeGmail:
    def __init__(self, fail: str = ""):
        self.drafts, self.fail = [], fail

    def create_draft(self, to, subject, body):
        if self.fail:
            raise GmailError(self.fail)
        self.drafts.append((to, subject, body))
        return f"m{len(self.drafts)}"


def app(s, gm):
    return TestClient(create_app(s, gmail_factory=lambda: gm))


# --- 道具そのもの ---


def test_scope_is_compose_only_and_there_is_no_send():
    assert SCOPE == "https://www.googleapis.com/auth/gmail.compose"
    src = Path(gmail_draft.__file__).read_text(encoding="utf-8")
    assert "/send" not in src and "users/me/messages" not in src and "smtplib" not in src


def test_create_draft_posts_a_mime_message_to_drafts():
    calls = []

    def post(url, form=None, json_body=None, headers=None):
        calls.append((url, form, json_body, headers))
        return {"access_token": "AT"} if "token" in url else {"id": "d1", "message": {"id": "m9"}}

    gm = Gmail("cid", "secret", "refresh", post=post)
    assert gm.create_draft("sv01@example.com", "柚木 茂さんの件", "岩舘様\n本文です。") == "m9"
    assert calls[0][0] == gmail_draft.TOKEN_URL and calls[0][1]["grant_type"] == "refresh_token" and calls[0][1]["refresh_token"] == "refresh"
    url, _, body, headers = calls[1]
    assert url == "https://gmail.googleapis.com/gmail/v1/users/me/drafts" and headers["Authorization"] == "Bearer AT"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]), policy=email.policy.default)
    assert msg["To"] == "sv01@example.com" and msg["Subject"] == "柚木 茂さんの件" and "本文です。" in msg.get_content()


def test_draft_without_an_address_leaves_to_empty():
    def post(url, form=None, json_body=None, headers=None):
        return {"access_token": "AT"} if "token" in url else {"message": {"id": "m1"}}

    seen = {}

    def keep(url, form=None, json_body=None, headers=None):
        seen.update(json_body or {})
        return post(url, form, json_body, headers)

    Gmail("c", "s", "r", post=keep).create_draft("", "件名", "本文")
    msg = email.message_from_bytes(base64.urlsafe_b64decode(seen["message"]["raw"]), policy=email.policy.default)
    assert msg["To"] is None


def test_token_failure_is_a_gmail_error():
    def post(url, form=None, json_body=None, headers=None):
        raise GmailError("invalid_grant")

    with pytest.raises(GmailError):
        Gmail("c", "s", "r", post=post).create_draft("a@example.com", "件名", "本文")


def test_connected_needs_both_the_client_and_the_token(tmp_path):
    from datetime import date

    from keika.settings import Settings

    s = Settings(drive_root=tmp_path, db_path=tmp_path / "data" / "x.db", as_of=date(2026, 11, 13))
    (tmp_path / "data").mkdir()
    assert connected(s) is None
    (tmp_path / "data" / "gmail_client.json").write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "sec"}}), encoding="utf-8")
    assert connected(s) is None
    save_token(s, "refresh")
    gm = connected(s)
    assert isinstance(gm, Gmail) and gm.client_id == "cid" and gm.refresh_token == "refresh"


def test_auth_url_asks_only_for_compose_and_carries_no_mail():
    url = auth_url("cid", "http://127.0.0.1:5000/", "challenge", "state1")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "gmail.compose" in url and "code_challenge=challenge" in url and "code_challenge_method=S256" in url and "access_type=offline" in url
    assert "gmail.send" not in url and "mail.google.com%2F" not in url


def test_open_url_points_at_the_draft_without_the_body():
    assert open_url("m9") == "https://mail.google.com/mail/#drafts?compose=m9"


# --- 画面 ---


def test_connected_screen_has_one_gmail_button(env):
    s, c = env
    i = made(s, c)
    html = app(s, FakeGmail()).get(f"/desk/U04/mail/{i}").text
    assert '<h1 class="do-line">確かめて、Gmailに入れる</h1>' in html
    assert html.count('class="send-button"') == 1 and ">Gmailの下書きに入れる</button>" in html
    assert "Gmail はまだつながっていません" not in html


def test_pressing_puts_it_in_gmail_and_keeps_a_copy(env):
    s, c = env
    i = made(s, c)
    gm = FakeGmail()
    client = app(s, gm)
    r = client.post(f"/desk/U04/mail/{i}/send", follow_redirects=False)
    assert r.status_code == 303
    assert len(gm.drafts) == 1 and gm.drafts[0][0] == "sv01@example.com" and "柚木 茂さん" in gm.drafts[0][2]
    assert len(sent_list(c)) == 1
    html = client.get(r.headers["location"]).text
    assert "Gmailの下書きに入れました。送るのは Gmail からです" in html
    assert 'href="https://mail.google.com/mail/#drafts?compose=m1"' in html and ">Gmailで開く</a>" in html
    assert "location.href" not in html and "mailto:" not in html


def test_gmail_trouble_keeps_nothing_and_says_so(env):
    s, c = env
    i = made(s, c)
    client = app(s, FakeGmail(fail="接続できません"))
    r = client.post(f"/desk/U04/mail/{i}/send")
    assert sent_list(c) == []
    assert "Gmail に入れられませんでした（接続できません）" in r.text and ">Gmailの下書きに入れる</button>" in r.text


def test_red_lines_never_reach_gmail(env):
    s, c = env
    i = made(s, c)
    edit_mail(c, "U04", i, "柚木 茂さんの件", "デイサービスこなら坂 岩舘様\n柚木 茂さんの件、〇〇日までに。")
    gm = FakeGmail()
    app(s, gm).post(f"/desk/U04/mail/{i}/send")
    assert gm.drafts == [] and sent_list(c) == []


def test_not_connected_says_so_and_falls_back_to_the_mail_app(env):
    s, c = env
    i = made(s, c)
    html = app(s, None).get(f"/desk/U04/mail/{i}").text
    assert "Gmail はまだつながっていません" in html and ">メールアプリで開く</button>" in html


def test_default_app_is_not_connected_without_files(env):
    s, c = env
    i = made(s, c)
    assert "Gmail はまだつながっていません" in TestClient(create_app(s)).get(f"/desk/U04/mail/{i}").text


def _settings(tmp_path):
    from datetime import date

    from keika.settings import Settings

    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "gmail_client.json").write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "sec"}}), encoding="utf-8")
    return Settings(drive_root=tmp_path, db_path=tmp_path / "data" / "x.db", as_of=date(2026, 11, 13))


def _browser(state_override=None):
    import threading
    import urllib.parse
    import urllib.request

    def open_browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        back = f"{q['redirect_uri']}?code=c1&state={state_override or q['state']}"
        threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=10).read(), daemon=True).start()

    return open_browser


def test_connect_saves_only_a_refresh_token_in_the_data_folder(tmp_path):
    s = _settings(tmp_path)
    sent = []

    def post(url, form=None, json_body=None, headers=None):
        sent.append(form)
        return {"access_token": "AT", "refresh_token": "RT"}

    assert gmail_draft.connect(s, open_browser=_browser(), post=post) == "Gmail とつながりました。"
    assert sent[0]["code"] == "c1" and sent[0]["code_verifier"] and sent[0]["grant_type"] == "authorization_code"
    assert json.loads((tmp_path / "data" / "gmail_token.json").read_text(encoding="utf-8")) == {"refresh_token": "RT"}


def test_connect_ignores_a_reply_with_the_wrong_state(tmp_path):
    s = _settings(tmp_path)
    msg = gmail_draft.connect(s, open_browser=_browser("other"), post=lambda *a, **k: {"refresh_token": "RT"})
    assert "確かめられません" in msg and not (tmp_path / "data" / "gmail_token.json").exists()


def test_connect_without_the_client_file_says_where_to_put_it(tmp_path):
    from datetime import date

    from keika.settings import Settings

    s = Settings(drive_root=tmp_path, db_path=tmp_path / "x.db", as_of=date(2026, 11, 13))
    assert "gmail_client.json" in gmail_draft.connect(s, open_browser=lambda u: None)
