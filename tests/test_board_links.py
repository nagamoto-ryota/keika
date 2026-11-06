"""一覧の進み具合の表から、その人の書類やその項目を開く。他の人の書類は開かない。"""
from urllib.parse import quote

from fastapi.testclient import TestClient

from keika.ledger import get_user
from keika.machine_checks import user_refs
from keika.progress import Cell, user_progress
from keika.web.app import create_app, progress_link
from test_care_plan import ASSESS, env  # noqa: F401  （env は fixture）

OTHERS = "利用者/U02_予防花子/アセスメント_2026-09-01.txt"


def board_cells(html, user_id):
    row = html.split(f'<a href="/users/{user_id}">')[1].split("</tr>")[0]
    return row


def test_mark_with_a_document_opens_that_persons_document_on_the_left(env):
    s, c = env
    cells = user_progress(c, s, get_user(c, "U01"))
    with_doc = [x for x in cells if x.file and x.status in ("done", "check", "unknown")]
    assert with_doc  # 計画書を根拠にした印がある
    row = board_cells(TestClient(create_app(s)).get("/board").text, "U01")
    for x in with_doc:
        assert f'href="/desk?t=U01&amp;doc={quote(x.file)}"' in row
    assert TestClient(create_app(s)).get(f"/desk?t=U01&doc={quote(with_doc[0].file)}").text.count(f'data-open-file="{with_doc[0].file}"') == 1


def test_done_points_at_its_own_document():
    assert progress_link("U01", Cell("assessment", "アセス", "done", file=ASSESS), {ASSESS}) == f"/desk?t=U01&doc={quote(ASSESS)}"


def test_link_never_points_at_someone_elses_document(env):
    s, c = env
    docs = {r.rel_path for r in user_refs(c, s, get_user(c, "U01"))}
    link = progress_link("U01", Cell("assessment", "アセス", "done", file=OTHERS), docs)
    assert OTHERS not in link and quote(OTHERS) not in link and link == "/users/U01#step-assessment"


def test_check_with_a_document_opens_it_and_without_goes_to_the_item(env):
    s, c = env
    docs = {ASSESS}
    assert progress_link("U01", Cell("assessment", "アセス", "check", file=ASSESS), docs) == f"/desk?t=U01&doc={quote(ASSESS)}"
    assert progress_link("U01", Cell("minutes", "会議録", "unknown"), docs) == "/users/U01#step-minutes"


def test_not_yet_goes_to_the_item_on_the_persons_page_and_says_it_is_missing(env):
    s, c = env
    assert progress_link("U01", Cell("minutes", "会議録", "todo", note="会議録が見つかりません"), set()) == "/users/U01#step-minutes"
    html = TestClient(create_app(s)).get("/users/U01").text
    assert 'id="step-minutes"' in html and 'class="step-why"' in html


def test_dash_is_not_a_link(env):
    assert progress_link("U01", Cell("consent", "同意", "na"), set()) == ""
    s, c = env
    row = board_cells(TestClient(create_app(s)).get("/board").text, "U01")
    for td in row.split("<td")[1:]:
        if "—" in td:
            assert "<a " not in td
