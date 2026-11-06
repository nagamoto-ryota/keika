"""Gmail の下書きに入れるだけの道具。送る機能は作らない。

範囲は gmail.compose（下書きを作る）と gmail.readonly（ケイカが作ったメールが送信済みかと、その返事を読む。変更・送信・削除はできない）だけ。認証はこのパソコンでの OAuth 同意で、クライアントの情報と token は
DB と同じフォルダ（git に入らない data/）に置く。本文を URL に載せない。
"""
from __future__ import annotations

import base64
import hashlib
import html
import re
import time
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

from keika.settings import Settings

SCOPE = "https://www.googleapis.com/auth/gmail.compose"
READ = "https://www.googleapis.com/auth/gmail.readonly"  # 送ったかと返事を読むため。読むだけで、変えられない範囲
SCOPES = [SCOPE, READ]
THREADS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/threads/"
MESSAGE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/"  # 読むだけ（format=full の取得）
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRAFTS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
UPLOAD_URL = "https://gmail.googleapis.com/upload/gmail/v1/users/me/drafts?uploadType=media"
UPLOAD_OVER = 5 * 1024 * 1024  # これより大きい時は upload の経路で送る


class GmailError(Exception):
    def __init__(self, message: str = "", code: int | None = None):
        super().__init__(message)
        self.code = code


def _fail(e: urllib.error.HTTPError) -> GmailError:
    try:
        reason = json.loads(e.read().decode("utf-8")).get("error", "")
    except ValueError:
        reason = ""
    reason = reason.get("message", "") if isinstance(reason, dict) else reason
    return GmailError(f"{e.code} {reason}".strip(), code=e.code)


def _get(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _fail(e) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GmailError(f"接続できません（{type(e).__name__}）") from None


def _post(url: str, form: dict | None = None, json_body: dict | None = None, headers: dict | None = None, raw: bytes | None = None) -> dict:
    """標準ライブラリだけで POST する。失敗は GmailError にする（中身に鍵を入れない）。"""
    if raw is not None:
        data, kind = raw, (headers or {}).get("Content-Type", "application/octet-stream")
    elif json_body is not None:
        data, kind = json.dumps(json_body).encode("utf-8"), "application/json"
    else:
        data, kind = urllib.parse.urlencode(form or {}).encode("utf-8"), "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": kind, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _fail(e) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GmailError(f"接続できません（{type(e).__name__}）") from None


@dataclass
class Gmail:
    client_id: str
    client_secret: str
    refresh_token: str
    post: Callable[..., dict] = field(default=_post, repr=False)
    get: Callable[..., dict] = field(default=_get, repr=False)
    _token: str = field(default="", repr=False)
    _token_until: float = field(default=0.0, repr=False)

    def _access_token(self) -> str:
        """有効な間は使い回す（呼ぶたびに取り直さない）。"""
        if self._token and time.monotonic() < self._token_until:
            return self._token
        got = self.post(TOKEN_URL, form={"client_id": self.client_id, "client_secret": self.client_secret,
                                         "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
        token = got.get("access_token")
        if not token:
            raise GmailError("つながりが切れています。もう一度つないでください。")
        self._token, self._token_until = token, time.monotonic() + max(0, int(got.get("expires_in") or 0) - 60)
        return token

    def create_draft(self, to: str, subject: str, body: str, attachments: list[tuple[str, str, bytes]] | tuple = ()) -> tuple[str, str]:
        """下書きを1通作り、(メッセージの番号, スレッドの番号) を返す。送らない。添付は (名前, 種類, 中身) の並び。"""
        msg = EmailMessage()
        if to:
            msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        for name, mime, data in attachments:
            main, _, minor = (mime or "application/octet-stream").partition("/")
            msg.add_attachment(data, maintype=main, subtype=minor or "octet-stream", filename=name)
        whole = msg.as_bytes()
        auth = {"Authorization": f"Bearer {self._access_token()}"}
        if len(whole) > UPLOAD_OVER:
            got = self.post(UPLOAD_URL, headers={**auth, "Content-Type": "message/rfc822"}, raw=whole)
        else:
            got = self.post(DRAFTS_URL, json_body={"message": {"raw": base64.urlsafe_b64encode(whole).decode("ascii")}}, headers=auth)
        message = got.get("message") or {}
        if not message.get("id"):
            raise GmailError("下書きの番号が返りませんでした。")
        return message["id"], message.get("threadId", "")

    def thread_messages(self, thread_id: str) -> list[dict]:
        """ケイカが作ったスレッドの各メールの番号と目印と日時だけ（本文は頼まない）。無ければ GmailError(code=404)。"""
        url = THREADS_URL + urllib.parse.quote(thread_id, safe="") + "?" + urllib.parse.urlencode({"format": "minimal", "fields": "messages(id,labelIds,internalDate)"})
        got = self.get(url, headers={"Authorization": f"Bearer {self._access_token()}"})
        return [{"id": m.get("id", ""), "labels": m.get("labelIds") or [], "date": int(m.get("internalDate") or 0) or None} for m in got.get("messages") or []]

    def sent_state(self, thread_id: str) -> tuple[str, int | None]:
        """送信済みかを見る。("sent", 送った時刻のミリ秒) / ("draft", None) / ("gone", None)。"""
        try:
            msgs = self.thread_messages(thread_id)
        except GmailError as e:
            if e.code == 404:
                return "gone", None
            raise
        sent = [m for m in msgs if "SENT" in m["labels"]]
        if sent:
            return "sent", sent[0]["date"]
        return ("draft", None) if msgs else ("gone", None)

    def read_message(self, message_id: str) -> dict:
        """ケイカのスレッドに届いた返事を1通読む。添付は名前だけ（中身は取らない）。"""
        url = MESSAGE_URL + urllib.parse.quote(message_id, safe="") + "?" + urllib.parse.urlencode({"format": "full"})
        got = self.get(url, headers={"Authorization": f"Bearer {self._access_token()}"})
        payload = got.get("payload") or {}
        heads = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers") or []}
        texts, htmls, names = [], [], []

        def walk(part: dict) -> None:
            if part.get("filename"):
                names.append(part["filename"])
            elif part.get("mimeType") in ("text/plain", "text/html") and (part.get("body") or {}).get("data"):
                (texts if part["mimeType"] == "text/plain" else htmls).append(_decode(part))
            for child in part.get("parts") or []:
                walk(child)

        walk(payload)
        text = "\n".join(texts).strip() or "\n".join(_plain(h) for h in htmls).strip()  # 本文が HTML だけの返事も読む
        return {"from": heads.get("from", ""), "to": heads.get("to", ""), "subject": heads.get("subject", ""), "date": int(got.get("internalDate") or 0) or None,
                "text": text, "attachments": names}


def _decode(part: dict) -> str:
    """その部分の文字コードで読む（ISO-2022-JP や Shift_JIS の返事が化けないように）。"""
    data = base64.urlsafe_b64decode(part["body"]["data"] + "=" * (-len(part["body"]["data"]) % 4))
    kind = next((h.get("value", "") for h in part.get("headers") or [] if h.get("name", "").lower() == "content-type"), "")
    found = re.search(r'charset="?([\w\-]+)"?', kind, re.I)
    try:
        return data.decode(found[1] if found else "utf-8", "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


def _plain(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|tr)>", "\n", markup)
    return html.unescape(re.sub(r"<[^>]+>", "", markup)).strip()


def client_path(s: Settings) -> Path:
    return s.db_path.parent / "gmail_client.json"


def token_path(s: Settings) -> Path:
    return s.db_path.parent / "gmail_token.json"


def _client(s: Settings) -> tuple[str, str] | None:
    try:
        data = json.loads(client_path(s).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    info = data.get("installed") or data.get("web") or {}
    if not info.get("client_id") or not info.get("client_secret"):
        return None
    return info["client_id"], info["client_secret"]


def save_token(s: Settings, refresh_token: str, granted: str | None = None) -> None:
    """Google が実際に許した権限を覚える。同意の画面で読む権限が外された時は、つなぎ直しが要ると分かるように。"""
    scopes = granted.split() if granted else SCOPES
    token_path(s).write_text(json.dumps({"refresh_token": refresh_token, "scopes": scopes}), encoding="utf-8")


def needs_reconnect(s: Settings) -> bool:
    """読む権限が無い古い token の時。つなぎ直すまで、送ったかも返事も見ない。"""
    try:
        data = json.loads(token_path(s).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return READ not in (data.get("scopes") or [])


def connected(s: Settings) -> Gmail | None:
    """つないである時だけ道具を返す。無ければ None（画面はメールアプリに戻る）。"""
    client = _client(s)
    try:
        refresh = json.loads(token_path(s).read_text(encoding="utf-8")).get("refresh_token", "")
    except (OSError, ValueError):
        refresh = ""
    if client is None or not refresh:
        return None
    return Gmail(client[0], client[1], refresh)


def open_url(message_id: str) -> str:
    return f"https://mail.google.com/mail/#drafts?compose={urllib.parse.quote(message_id)}"


def auth_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(SCOPES),
        "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent", "state": state,
    })


def connect(s: Settings, open_browser: Callable[[str], Any] = webbrowser.open, post: Callable[..., dict] = _post) -> str:
    """このパソコンのブラウザで同意してもらい、token を data/ に置く。パスワードは扱わない。"""
    client = _client(s)
    if client is None:
        return f"{client_path(s)} がありません。OAuth クライアントの JSON をこの名前で置いてください。"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(16)
    got: dict[str, str] = {}

    class Back(BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("ケイカに戻ってください。この画面は閉じてかまいません。".encode("utf-8"))

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Back)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    open_browser(auth_url(client[0], redirect, challenge, state))
    server.timeout = 300
    server.handle_request()
    server.server_close()
    if not got.get("code"):
        return "同意が確かめられませんでした。もう一度試してください。"
    try:
        tokens = post(TOKEN_URL, form={"client_id": client[0], "client_secret": client[1], "code": got["code"], "code_verifier": verifier,
                                       "grant_type": "authorization_code", "redirect_uri": redirect})
    except GmailError as e:
        return f"つなげませんでした（{e}）"
    if not tokens.get("refresh_token"):
        return "つなげませんでした（token が返りませんでした）"
    save_token(s, tokens["refresh_token"], tokens.get("scope"))
    return "Gmail とつながりました。"
