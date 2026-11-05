"""Gmail の下書きに、その利用者の書類だけを添付する。本物の API は呼ばない。"""
import base64
import email
import email.policy

from fastapi.testclient import TestClient

from keika import attach
from keika.gmail_draft import Gmail
from keika.outbox import edit_mail, out_view, sent_list
from keika.web.app import create_app
from test_form_shelf import draft as make_draft
from test_outbox import env, made  # noqa: F401  （env は fixture）

MINE = "利用者/U04_柚木茂/アセスメント_2025-11-20.pdf"
OTHERS = "利用者/U05_戸叶フミ/アセスメント_2025-10-01.pdf"
BODY = "デイサービスこなら坂 岩舘様\n柚木 茂さんの件です。"


class FakeGmail:
    def __init__(self):
        self.drafts = []

    def create_draft(self, to, subject, body, attachments=()):
        self.drafts.append((to, subject, body, list(attachments)))
        return f"m{len(self.drafts)}", f"t{len(self.drafts)}"


def put(s, rel, data=b"%PDF-1.4 x"):
    path = s.drive_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def keys(items):
    return [a["key"] for a in items]


def app(s, gm):
    return TestClient(create_app(s, gmail_factory=lambda: gm))


def with_files(s):
    put(s, MINE)
    put(s, OTHERS)


# --- 候補 ---


def test_candidates_are_only_this_persons(env):
    s, c = env
    with_files(s)
    d = make_draft(s, c, "meeting")
    items = attach.candidates(c, s, "U04")
    assert f"f:{MINE}" in keys(items) and f"f:{OTHERS}" not in keys(items)
    assert f"d:{d}" in keys(items)
    assert all("U05" not in k for k in keys(items))
    assert f"d:{d}" not in keys(attach.candidates(c, s, "U05"))


def test_files_of_others_never_come_out_even_if_asked(env):
    s, c = env
    with_files(s)
    got = attach.files_for(c, s, "U04", [f"f:{OTHERS}", f"f:{MINE}", "f:../../x.pdf"])
    assert [g[0] for g in got] == ["アセスメント_2025-11-20.pdf"]
    assert got[0][1] == "application/pdf" and got[0][2] == b"%PDF-1.4 x"


def test_draft_comes_as_word(env):
    s, c = env
    d = make_draft(s, c, "meeting")
    (name, mime, data), = attach.files_for(c, s, "U04", [f"d:{d}"])
    assert name.endswith(".docx") and mime.endswith("wordprocessingml.document") and data[:2] == b"PK"


# --- Gmail の形 ---


def _gm(calls):
    def post(url, form=None, json_body=None, headers=None, raw=None):
        calls.append((url, json_body, headers, raw))
        return {"access_token": "AT"} if "token" in url else {"message": {"id": "m1"}}

    return Gmail("c", "s", "r", post=post)


def test_attachments_go_as_multipart_with_name_type_and_bytes():
    calls = []
    _gm(calls).create_draft("a@example.com", "件名", "本文", attachments=[("アセスメント.pdf", "application/pdf", b"x" * 1234)])
    raw = base64.urlsafe_b64decode(calls[1][1]["message"]["raw"])
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    parts = list(msg.iter_attachments())
    assert msg.is_multipart() and len(parts) == 1
    assert parts[0].get_filename() == "アセスメント.pdf" and parts[0].get_content_type() == "application/pdf" and len(parts[0].get_content()) == 1234


def test_big_message_uses_the_upload_way():
    calls = []
    _gm(calls).create_draft("a@example.com", "件名", "本文", attachments=[("大.pdf", "application/pdf", b"x" * (6 * 1024 * 1024))])
    url, body, headers, raw = calls[1]
    assert url == "https://gmail.googleapis.com/upload/gmail/v1/users/me/drafts?uploadType=media"
    assert body is None and headers["Content-Type"] == "message/rfc822" and len(raw) > 6 * 1024 * 1024


# --- 画面 ---


def page(s, gm, i):
    return app(s, gm).get(f"/desk/U04/mail/{i}").text


def test_attach_box_lists_only_this_persons_documents(env):
    s, c = env
    with_files(s)
    i = made(s, c)
    html = page(s, FakeGmail(), i)
    box = html.split('class="mail-attach')[1].split("</fieldset>")[0]
    assert "アセスメント_2025-11-20.pdf" in box and "2025-10-01" not in box and 'type="checkbox"' in box


def test_without_gmail_the_box_says_attachments_need_it(env):
    s, c = env
    with_files(s)
    i = made(s, c)
    html = page(s, None, i)
    assert "添付は Gmail につないだ時だけ使えます" in html and 'name="attach"' not in html


def test_mentioned_document_starts_checked(env):
    s, c = env
    with_files(s)
    i = made(s, c, body="デイサービスこなら坂 岩舘様\n柚木 茂さんのアセスメントを添付します。")
    html = page(s, FakeGmail(), i)
    box = html.split('class="mail-attach')[1].split("</fieldset>")[0]
    row = next(line for line in box.splitlines() if "2025-11-20" in line)
    assert "checked" in row


def test_says_attached_but_nothing_is_red(env):
    s, c = env
    with_files(s)
    i = made(s, c, body="デイサービスこなら坂 岩舘様\n柚木 茂さんの書類を添付します。")
    view = out_view(c, s, i, gmail_on=True)
    assert any("添付" in b for b in view["blocks"])
    assert [l["check"] for l in view["lines"]] == [False, True]


def test_attached_but_not_mentioned_is_red(env):
    s, c = env
    with_files(s)
    i = made(s, c, body=BODY)
    attach.choose(c, i, [f"f:{MINE}"])
    view = out_view(c, s, i, gmail_on=True)
    assert view["attach_red"] and any("添付" in b for b in view["blocks"])


def test_mismatch_does_not_stop_the_mail_app(env):
    s, c = env
    i = made(s, c, body="デイサービスこなら坂 岩舘様\n柚木 茂さんの書類を添付します。")
    assert out_view(c, s, i, gmail_on=False)["blocks"] == []


def test_over_25mb_stops_before_pressing(env):
    s, c = env
    put(s, MINE, b"x" * (25 * 1024 * 1024 + 1))
    i = made(s, c, body="デイサービスこなら坂 岩舘様\n柚木 茂さんのアセスメントを添付します。")
    attach.choose(c, i, [f"f:{MINE}"])
    view = out_view(c, s, i, gmail_on=True)
    assert any("25MB" in b for b in view["blocks"])
    gm = FakeGmail()
    app(s, gm).post(f"/desk/U04/mail/{i}/send")
    assert gm.drafts == [] and sent_list(c) == []


def test_choice_is_saved_from_the_screen_and_others_are_dropped(env):
    s, c = env
    with_files(s)
    i = made(s, c, body="デイサービスこなら坂 岩舘様\n柚木 茂さんのアセスメントを添付します。")
    m_lines = ["デイサービスこなら坂 岩舘様", "柚木 茂さんのアセスメントを添付します。"]
    app(s, FakeGmail()).post(f"/desk/U04/mail/{i}/edit", data={"subject": "件名", "line": m_lines, "attach_shown": "1", "attach": [f"f:{MINE}", f"f:{OTHERS}"]})
    assert attach.chosen(c, s, i, "U04", "") == [f"f:{MINE}"]
    app(s, FakeGmail()).post(f"/desk/U04/mail/{i}/edit", data={"subject": "件名", "line": m_lines, "attach_shown": "1"})
    assert attach.chosen(c, s, i, "U04", "アセスメントを添付します") == []  # 外したら外したまま


def test_pressing_sends_the_files_to_gmail_and_the_copy_lists_them(env):
    s, c = env
    with_files(s)
    i = made(s, c, body="デイサービスこなら坂 岩舘様\n柚木 茂さんのアセスメントを添付します。")
    gm = FakeGmail()
    app(s, gm).post(f"/desk/U04/mail/{i}/send")
    (_, _, _, files), = gm.drafts
    assert [(n, t, len(b)) for n, t, b in files] == [("アセスメント_2025-11-20.pdf", "application/pdf", 10)]
    assert sent_list(c)[0]["attached"] == ["アセスメント_2025-11-20.pdf"]
    assert "アセスメント_2025-11-20.pdf" in app(s, gm).get("/mail/sent").text


def test_red_attach_never_reaches_gmail(env):
    s, c = env
    with_files(s)
    i = made(s, c, body="デイサービスこなら坂 岩舘様\n柚木 茂さんの書類を添付します。")
    attach.choose(c, i, [])
    gm = FakeGmail()
    app(s, gm).post(f"/desk/U04/mail/{i}/send")
    assert gm.drafts == []
    edit_mail(c, "U04", i, "件名", BODY)
    app(s, gm).post(f"/desk/U04/mail/{i}/send")
    assert len(gm.drafts) == 1 and gm.drafts[0][3] == []
