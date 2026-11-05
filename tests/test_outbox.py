"""先回りのメール案、共有、送信控え。自動では送らず、宛先は登録された相手だけ。"""
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.ledger import seed_from_json
from keika.outbox import (
    Blocked, SENT_DEMO, WireProposal, candidates, edit_mail, home_lines, make_share, mark_all_scanned, med_ok, out_view, prepare_demo, propose,
    recipients, send_mail, sent_list,
)
from keika.records import RecordIn, add_record
from keika.secretary import WireMail, get_mail
from keika.settings import Settings
from keika.web.app import create_app
from test_reading import FakeClient, answer

GROUND = "能登さんに家事の内容を確認する"
BODY = "ヘルパーステーションほたるい 能登様\n柚木 茂さんの週2回の家事の内容を教えてください。\nケアプランセンターはしばみ"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 13))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def record(c, user="U04", body="今日訪問した。能登さんに家事の内容を確認することにした。"):
    return add_record(c, RecordIn(user_id=user, event_date="2026-11-12", kind_read="居宅訪問", body=body)).id


def sv(c, user="U06"):
    return next(r for r in recipients(c, Settings(drive_root=Path("."), db_path=Path("x"), as_of=date(2026, 11, 13)), user) if r["kind"] == "provider")["id"]


def wire(**kw):
    return WireProposal(**(dict(need=True, recipient_id="SV02", purpose="照会", ground=GROUND, subject="柚木 茂さんの件", body=BODY) | kw))


def made(s, c, user="U04", **kw):
    """U04 の担当に SV02 は無いので、試験の相手は登録された SV01 を使う。"""
    body = kw.pop("body", "デイサービスこなら坂 岩舘様\n柚木 茂さんの週2回の内容を教えてください。")
    rid = record(c, user, kw.pop("said", "今日訪問した。能登さんに家事の内容を確認することにした。"))
    return propose(c, FakeClient(answer(wire(recipient_id="SV01", body=body, **kw))), s, rid)


# --- 宛先 ---


def test_recipients_are_only_those_registered_for_this_user(env):
    s, c = env
    rows = recipients(c, s, "U04")
    ids = {r["id"] for r in rows}
    assert ids == {"SV01", "SV05", "F05", "F06", "DR02"}
    assert "F01" not in ids and "SV02" not in ids  # 別の人の相手は出さない
    assert rows[0]["kind"] == "provider"  # 既定は担当の他事業所
    assert {r["kind"] for r in rows} == {"provider", "family", "medical"}


def test_addresses_in_the_demo_are_example_ones(env):
    s, c = env
    assert all(r["address"].endswith("@example.com") for r in recipients(c, s, "U04"))


def test_without_the_demo_no_address_is_invented(env):
    s, c = env
    real = Settings(drive_root=s.drive_root, db_path=s.db_path, as_of=None)
    assert all(r["address"] == "" for r in recipients(c, real, "U04"))


def test_other_users_hospital_is_not_offered(env):
    s, c = env
    assert "HP01" in {r["id"] for r in recipients(c, s, "U02")} and "HP01" not in {r["id"] for r in recipients(c, s, "U04")}


# --- 先回りの案 ---


def test_proposal_is_saved_with_its_ground_and_shown_on_the_home(env):
    s, c = env
    i = made(s, c)
    assert i
    m = out_view(c, s, i)
    assert m["ground"] == GROUND and m["origin"] == "proposal" and m["recipient"]["id"] == "SV01"
    line = home_lines(c)[0]
    assert line["user_name"].startswith("柚木") and "デイサービスこなら坂への照会メールの案を用意しました" in line["text"]
    html = TestClient(create_app(s)).get("/").text
    assert "照会メールの案を用意しました" in html and f'/desk/U04/mail/{i}' in html and GROUND in html


def test_no_need_means_no_proposal_and_no_second_try(env):
    s, c = env
    rid = record(c)
    client = FakeClient(answer(wire(need=False)))
    assert propose(c, client, s, rid) is None
    assert propose(c, client, s, rid) is None
    assert len(client.calls) == 1 and rid not in [r.id for r in candidates(c)]


def test_same_record_gets_one_proposal(env):
    s, c = env
    rid = record(c)
    client = FakeClient(answer(wire(recipient_id="SV01", body="デイサービスこなら坂 岩舘様\n柚木 茂さんの内容を教えてください。")))
    assert propose(c, client, s, rid)
    assert propose(c, client, s, rid) is None
    assert len(client.calls) == 1 and len(home_lines(c)) == 1


def test_same_person_and_same_partner_is_not_proposed_twice(env):
    s, c = env
    assert made(s, c)
    assert made(s, c) is None
    assert len(home_lines(c)) == 1


def test_recipient_not_registered_is_refused(env):
    s, c = env
    assert propose(c, FakeClient(answer(wire(recipient_id="SV02"))), s, record(c)) is None
    assert not home_lines(c)


def test_ground_not_in_the_record_is_refused(env):
    s, c = env
    assert made(s, c, ground="記録のどこにも無い文のはずです") is None


def test_line_crossing_body_is_refused(env):
    s, c = env
    body = "デイサービスこなら坂 岩舘様\n柚木 茂さんは薬を中止したほうがよいです。"
    assert made(s, c, body=body) is None


def test_candidates_need_a_contact_word_and_skip_csv(env):
    s, c = env
    a = record(c)
    record(c, body="今日は天気の話をした。変わりなし。")
    assert [r.id for r in candidates(c)] == [a]
    mark_all_scanned(c)
    assert candidates(c) == []


def test_home_with_no_key_still_opens(env):
    s, c = env
    record(c)
    assert TestClient(create_app(s)).get("/").status_code == 200


# --- 送信 ---


def test_nothing_is_sent_until_the_person_presses(env):
    s, c = env
    i = made(s, c)
    assert sent_list(c) == []
    r = TestClient(create_app(s)).post(f"/desk/U04/mail/{i}/send", follow_redirects=False)
    assert r.status_code == 303
    rows = sent_list(c)
    assert len(rows) == 1 and rows[0]["sent_to"].endswith("@example.com") and rows[0]["sent_body"]
    html = TestClient(create_app(s)).get(f"/desk/U04/mail/{i}").text
    assert "控えに残しました" in html and 'class="send-button"' not in html
    assert home_lines(c) == []


def test_sent_list_page_shows_the_copy(env):
    s, c = env
    i = made(s, c)
    send_mail(c, s, "U04", i)
    html = TestClient(create_app(s)).get("/mail/sent").text
    assert "デイサービスこなら坂" in html and "柚木 茂さんの週2回" in html and SENT_DEMO in html


def page(s, i, user="U04", **kw):
    return TestClient(create_app(s, **kw)).get(f"/desk/{user}/mail/{i}").text


def main_buttons(html):
    return html.count('class="send-button"')


def test_the_screen_has_one_main_button_and_two_small_ones(env):
    s, c = env
    i = made(s, c)
    html = page(s, i)
    assert '<h1 class="do-line">確かめて、メールアプリで開く</h1>' in html
    assert main_buttons(html) == 1 and ">メールアプリで開く</button>" in html
    assert "<summary>AIに直してもらう</summary>" in html and ">要らない</button>" in html
    for word in (">送信</button>", "<summary>直す</summary>", "送った控えを残す", ">いいよ</button>", "この文にする"):
        assert word not in html, word
    assert GROUND in html


def body_lines(html):
    part = html.split('id="mail-body"')[1].split("</div>")[0]
    return [seg.split(">", 1) for seg in part.split("<textarea")[1:]]


def test_lines_to_check_are_red_in_the_body_and_the_button_says_why(env):
    s, c = env
    i = made(s, c)
    edit_mail(c, "U04", i, "柚木 茂さんの件", "デイサービスこなら坂 岩舘様\n柚木 茂さんの件、〇〇日までに。")
    html = page(s, i)
    lines = body_lines(html)
    assert len(lines) == 2
    assert "is-blank" not in lines[0][0] and "is-blank" in lines[1][0] and lines[1][1].startswith("柚木 茂さんの件、〇〇日までに。")
    bar = html.split('class="mail-go"')[1].split("</form>")[0]
    assert 'class="send-button" disabled' in bar and "〇〇 が残っています" in bar


def test_body_text_is_escaped_in_the_lines(env):
    s, c = env
    i = made(s, c)
    edit_mail(c, "U04", i, "柚木 茂さんの件", "デイサービスこなら坂 岩舘様\n柚木 茂さん<b>の件")
    html = page(s, i)
    assert "&lt;b&gt;" in html and "<b>の件" not in html


def test_lines_are_saved_in_place(env):
    s, c = env
    i = made(s, c)
    r = TestClient(create_app(s)).post(f"/desk/U04/mail/{i}/edit", data={"subject": "件名を直した", "line": ["デイサービスこなら坂 岩舘様", "", "柚木 茂さんの件です。"]})
    assert r.status_code == 200
    m = get_mail(c, "U04", i)
    assert m["subject"] == "件名を直した" and m["body"] == "デイサービスこなら坂 岩舘様\n\n柚木 茂さんの件です。"


def test_pressing_keeps_the_latest_lines_in_the_copy(env):
    s, c = env
    i = made(s, c)
    edit_mail(c, "U04", i, "柚木 茂さんの件", "デイサービスこなら坂 岩舘様\n柚木 茂さんの件、〇〇日までに。")
    r = TestClient(create_app(s)).post(f"/desk/U04/mail/{i}/send", data={"subject": "柚木 茂さんの件", "line": ["デイサービスこなら坂 岩舘様", "柚木 茂さんの件、金曜までに。"]}, follow_redirects=False)
    assert r.status_code == 303
    rows = sent_list(c)
    assert len(rows) == 1 and rows[0]["sent_body"].endswith("金曜までに。")


def test_pressing_while_red_keeps_nothing(env):
    s, c = env
    i = made(s, c)
    edit_mail(c, "U04", i, "柚木 茂さんの件", "デイサービスこなら坂 岩舘様\n柚木 茂さんの件、〇〇日までに。")
    TestClient(create_app(s)).post(f"/desk/U04/mail/{i}/send")
    assert sent_list(c) == []


def test_after_pressing_one_line_and_the_mail_app(env):
    s, c = env
    i = made(s, c)
    client = TestClient(create_app(s))
    r = client.post(f"/desk/U04/mail/{i}/send", follow_redirects=False)
    assert r.headers["location"].endswith("?just=1")
    html = client.get(r.headers["location"]).text
    assert "控えに残しました。送るのはメールアプリからです" in html
    assert 'href="mailto:sv01@example.com?subject=' in html and ">メールアプリで開く</a>" in html
    assert "location.href" in html  # 押した直後だけメールアプリを開く
    assert "location.href" not in client.get(f"/desk/U04/mail/{i}").text


def test_copies_are_one_step_from_the_menu(env):
    s, c = env
    nav = TestClient(create_app(s)).get("/").text.split("<nav>")[1].split("</nav>")[0]
    assert '<a href="/mail/sent"' in nav and "メールの控え" in nav
    assert '<a href="/mail/sent" aria-current="page">' in TestClient(create_app(s)).get("/mail/sent").text


def test_drop_removes_it_from_the_home_without_a_copy(env):
    s, c = env
    i = made(s, c)
    TestClient(create_app(s)).post(f"/desk/U04/mail/{i}/drop")
    assert home_lines(c) == [] and sent_list(c) == []


def test_only_one_function_records_a_send_and_nothing_leaves():
    src = Path(__file__).parent.parent / "src" / "keika"
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in ("smtplib", "sendmail", "send_message", "gmail"):
            assert word not in text, path.name
    assert (src / "outbox.py").read_text(encoding="utf-8").count("SET state = 'sent'") == 1
    assert (src / "web" / "app.py").read_text(encoding="utf-8").count("send_mail(") == 1


def test_other_users_name_in_the_body_blocks_sending(env):
    s, c = env
    i = made(s, c)
    edit_mail(c, "U04", i, "柚木 茂さんの件", "デイサービスこなら坂 岩舘様\n柚木 茂さんと杉森 ハルさんの件です。")
    assert any("杉森" in b for b in out_view(c, s, i)["blocks"])
    with pytest.raises(Blocked):
        send_mail(c, s, "U04", i)
    assert sent_list(c) == []


def test_body_without_this_users_name_blocks_sending(env):
    s, c = env
    i = made(s, c)
    edit_mail(c, "U04", i, "ご連絡", "デイサービスこなら坂 岩舘様\n週2回の内容を教えてください。")
    assert any("柚木" in b for b in out_view(c, s, i)["blocks"])


def test_body_addressed_to_someone_else_blocks_sending(env):
    s, c = env
    i = made(s, c)
    edit_mail(c, "U04", i, "柚木 茂さんの件", "柚木 茂さんの週2回の内容を教えてください。")
    assert any("宛名" in b for b in out_view(c, s, i)["blocks"])


def test_blank_left_in_the_body_blocks_sending(env):
    s, c = env
    i = made(s, c)
    edit_mail(c, "U04", i, "柚木 茂さんの件", "デイサービスこなら坂 岩舘様\n柚木 茂さんの件、〇〇日までに。")
    assert any("〇〇" in b for b in out_view(c, s, i)["blocks"])


def test_crossing_edit_blocks_sending(env):
    s, c = env
    i = made(s, c)
    edit_mail(c, "U04", i, "柚木 茂さんの件", "デイサービスこなら坂 岩舘様\n柚木 茂さんは今日中に受診してください。")
    assert out_view(c, s, i)["blocks"]


def test_medicine_in_the_body_stops_until_the_person_checks(env):
    s, c = env
    i = made(s, c, said=f"{GROUND}。昼の薬が無くなったと話した。朝の薬も。", body="デイサービスこなら坂 岩舘様\n柚木 茂さんは昼の薬が無くなったと話しています。")
    view = out_view(c, s, i)
    assert view["med"] and any("薬" in b for b in view["blocks"])
    with pytest.raises(Blocked):
        send_mail(c, s, "U04", i)
    med_ok(c, "U04", i)
    assert out_view(c, s, i)["blocks"] == []
    edit_mail(c, "U04", i, "柚木 茂さんの件", "デイサービスこなら坂 岩舘様\n柚木 茂さんは朝の薬も無くなったと話しています。")
    assert out_view(c, s, i)["blocks"]  # 直したらもう一度見る


def test_medicine_line_is_red_until_the_person_says_it_is_fine(env):
    s, c = env
    i = made(s, c, said=f"{GROUND}。昼の薬が無くなったと話した。朝の薬も。", body="デイサービスこなら坂 岩舘様\n柚木 茂さんは昼の薬が無くなったと話しています。")
    html = page(s, i)
    lines = body_lines(html)
    assert "is-blank" in lines[1][0] and "mailto:" not in html and ">薬の文はこのままでいい</button>" in html
    assert 'class="send-button" disabled' in html
    med_ok(c, "U04", i)
    html = page(s, i)
    assert all("is-blank" not in a for a, _ in body_lines(html)) and 'class="send-button" disabled' not in html


def test_other_threads_mail_cannot_be_opened_or_sent(env):
    s, c = env
    i = made(s, c)
    client = TestClient(create_app(s))
    assert client.post(f"/desk/U06/mail/{i}/send").status_code == 404
    assert sent_list(c) == []


def test_sample_mode_cannot_send(env):
    s, c = env
    i = made(s, c)
    assert TestClient(create_app(s, sample=True)).post(f"/desk/U04/mail/{i}/send").status_code == 403


def test_address_not_registered_any_more_blocks_sending(env):
    s, c = env
    i = made(s, c)
    c.execute("UPDATE mail_out SET recipient_id = 'SV02' WHERE mail_id = ?", (i,))
    c.commit()
    assert any("登録" in b for b in out_view(c, s, i)["blocks"])


# --- 共有 ---


def share_wire(body):
    return WireMail(to="", subject="柚木 茂さんの記録の共有", body=body)


def test_share_page_offers_registered_people_with_provider_first(env):
    s, c = env
    rid = record(c)
    html = TestClient(create_app(s)).get(f"/records/{rid}/share").text
    assert "デイサービスこなら坂" in html and "さわらぎ診療所" in html and "ヘルパーステーションほたるい" not in html
    assert html.index('value="SV01"') < html.index('value="F05"') < html.index('value="DR02"')
    assert 'value="SV01" selected' in html


def test_user_page_has_a_share_link(env):
    s, c = env
    rid = record(c)
    assert f'/records/{rid}/share' in TestClient(create_app(s)).get("/users/U04").text


def test_share_makes_a_mail_for_that_partner_on_the_same_screen(env):
    s, c = env
    rid = record(c)
    client = FakeClient(answer(share_wire("デイサービスこなら坂 岩舘様\n柚木 茂さんの訪問の要点です。")))
    i, problem = make_share(c, client, s, rid, "SV01")
    assert not problem
    m = out_view(c, s, i)
    assert m["origin"] == "share" and m["recipient"]["id"] == "SV01" and get_mail(c, "U04", i)["to_name"].startswith("デイサービスこなら坂")
    assert home_lines(c) == []  # 自分で頼んだ物は入口に出さない
    assert "業務" in client.calls[0]["system"][0]["text"] if isinstance(client.calls[0]["system"], list) else "業務" in client.calls[0]["system"]


def test_share_to_someone_not_registered_is_refused(env):
    s, c = env
    client = FakeClient(answer(share_wire("x")))
    i, problem = make_share(c, client, s, record(c), "SV02")
    assert i is None and problem and not client.calls


def test_share_words_change_with_the_partner(env):
    s, c = env
    rid = record(c)
    seen = {}
    for rec_id, kind_word in (("SV01", "業務"), ("F05", "専門語"), ("DR02", "事実と経過")):
        client = FakeClient(answer(share_wire("柚木 茂さんの要点です。")))
        make_share(c, client, s, rid, rec_id)
        seen[rec_id] = str(client.calls[0]["system"])
        assert kind_word in seen[rec_id]
    assert "受診の必要" in seen["DR02"]


def test_share_to_family_warns_about_consent_without_stopping(env):
    s, c = env
    rid = record(c, body="本人は認知症の診断について話していた。")
    i, _ = make_share(c, FakeClient(answer(share_wire("柚木 様\n柚木 茂さんは認知症の診断について話していました。"))), s, rid, "F05")
    view = out_view(c, s, i)
    assert view["consent"] and "本人の同意" in view["consent"]
    assert not any("同意" in b for b in view["blocks"])


def test_share_to_provider_has_no_consent_note(env):
    s, c = env
    rid = record(c, body="本人は認知症の診断について話していた。")
    i, _ = make_share(c, FakeClient(answer(share_wire("デイサービスこなら坂 岩舘様\n柚木 茂さんは認知症の診断について話していました。"))), s, rid, "SV01")
    assert not out_view(c, s, i)["consent"]


def test_share_body_crossing_the_line_is_refused(env):
    s, c = env
    i, problem = make_share(c, FakeClient(answer(share_wire("さわらぎ診療所 沢良木先生\n柚木 茂さんは受診したほうがよいです。"))), s, record(c), "DR02")
    assert i is None and problem


def test_share_again_returns_the_open_one(env):
    s, c = env
    rid = record(c)
    client = FakeClient(answer(share_wire("デイサービスこなら坂 岩舘様\n柚木 茂さんの要点です。")))
    a, _ = make_share(c, client, s, rid, "SV01")
    b, _ = make_share(c, client, s, rid, "SV01")
    assert a == b and len(client.calls) == 1


def test_share_without_a_key_says_so(env):
    s, c = env
    rid = record(c)
    r = TestClient(create_app(s)).post(f"/records/{rid}/share", data={"recipient": "SV01"})
    assert r.status_code == 200 and "作れませんでした" in r.text


def test_demo_gets_a_few_proposals_and_the_rest_are_marked_done(env):
    s, c = env
    for _ in range(3):
        record(c)
    client = FakeClient(answer(wire(recipient_id="SV01", body="デイサービスこなら坂 岩舘様\n柚木 茂さんの内容を教えてください。")))
    assert len(prepare_demo(c, client, s)) == 1  # 同じ相手には1通
    assert candidates(c) == []
