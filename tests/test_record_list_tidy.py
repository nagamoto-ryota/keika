"""利用者画面の記録の一覧: 履歴の画面は案内しない、未読の印は自動で入った未読の記録だけ、日時の無い取り込みに「不明」を出さない。"""
from keika.records import RecordIn, _insert, add_record, edit_record, mark_checked
from test_web_records import env  # noqa: F401  （env は fixture）


def row_of(html, record_id):
    return html.split(f'/records/{record_id}/edit')[0].rsplit('<article class="record">', 1)[1] + html.split(f'/records/{record_id}/edit')[1].split("</article>")[0]


def test_history_is_not_linked_from_the_list_or_the_edit_page(env):
    s, c, r, client = env
    edit_record(c, r.id, {"body": "直した文。"}, editor="井波")
    assert f"/records/{r.id}/history" not in client.get("/users/U04").text
    assert f"/records/{r.id}/history" not in client.get(f"/records/{r.id}/edit").text
    assert client.get(f"/records/{r.id}/history").status_code == 200  # データと画面は残す（案内しないだけ）


def test_unread_mark_only_on_records_keika_put_in_and_nobody_read(env):
    s, c, r, client = env
    read = add_record(c, RecordIn(user_id="U04", event_date="2026-11-03", kind_read="電話", body="読んだ記録。"))
    mark_checked(c, read.id, editor="井波")
    html = client.get("/users/U04").text
    assert "未読" in row_of(html, r.id) and ">読んだ</button>" in row_of(html, r.id)
    assert "未読" not in row_of(html, read.id) and ">読んだ</button>" not in row_of(html, read.id)


def test_imported_record_without_a_time_says_nothing_about_it(env):
    s, c, r, client = env
    with c:
        old = _insert(c, RecordIn(user_id="U04", event_date="2026-08-01", kind_read="電話", body="取り込んだ古い記録。"), None, "csv-1", False)
    html = client.get("/users/U04").text
    assert "不明（取り込み）" not in html and "記入:" not in row_of(html, old)
    assert "記入:" in row_of(html, r.id)
