"""アセスメントの下書き: 項目は課題分析標準項目（令和5年10月16日 老認発1016第1号 改正後）の名前だけ。
記録と書類に無い事は〇〇、他の人の記録を混ぜない、診断に読める文は入れない、Word に保存できる。"""
import io

import docx
from fastapi.testclient import TestClient

from keika.care_plan import ASSESS_GROUPS, BLANK, NOT_FOUND, SECTIONS, WireItem, WirePlan, WireSection, get_care_plan, list_care_plans, make_care_plan, to_docx
from keika.records import RecordIn, add_record
from keika.secretary import TOOLS
from keika.web.app import create_app
from test_care_plan import ASSESS, env  # noqa: F401  （env は fixture）
from test_reading import FakeClient, answer

NAMES = [
    "基本情報（受付、利用者等基本情報）", "これまでの生活と現在の状況", "利用者の社会保障制度の利用情報", "現在利用している支援や社会資源の状況",
    "日常生活自立度（障害）", "日常生活自立度（認知症）", "主訴・意向", "認定情報", "今回のアセスメントの理由",
    "健康状態", "ADL", "IADL", "認知機能や判断能力", "コミュニケーションにおける理解と表出の状況", "生活リズム", "排泄の状況",
    "清潔の保持に関する状況", "口腔内の状況", "食事摂取の状況", "社会との関わり", "家族等の状況", "居住環境", "その他留意すべき事項・状況",
]
OTHER = "鷺沼さんは港の倉庫で夜勤をしていたと話す。"


def item(text, quote, file=ASSESS, who=""):
    return WireItem(text=text, who=who, file=file, quote=quote)


def assess_wire(*sections):
    return WirePlan(sections=list(sections), rows=[], week=[])


def made(s, c, *sections):
    client = FakeClient(answer(assess_wire(*sections)))
    pid = make_care_plan(c, client, s, "U01", kind="assessment")
    return get_care_plan(c, pid), client


def test_item_names_are_the_23_standard_items_in_order():
    assert list(SECTIONS["assessment"].values()) == NAMES
    groups = [(g, [SECTIONS["assessment"][k] for k in keys]) for g, keys in ASSESS_GROUPS]
    assert groups == [("基本情報に関する項目", NAMES[:9]), ("課題分析（アセスメント）に関する項目", NAMES[9:])]


def test_items_not_in_records_are_blank(env):
    s, c = env
    p, _ = made(s, c, WireSection(key="a11", items=[item("階段は手すりを使う", "階段は手すりを使う")]))
    assert p.kind == "assessment" and p.title.startswith("アセスメント")
    assert p.sections["a11"][0]["text"] == "階段は手すりを使う" and p.sections["a11"][0]["file"] == ASSESS
    assert all(p.sections[k][0]["text"] == BLANK for k in SECTIONS["assessment"] if k != "a11")
    assert p.rows == [] and p.week == []


def test_other_persons_record_is_not_sent_or_used(env):
    s, c = env
    add_record(c, RecordIn(user_id="U02", event_date="2026-11-03", kind_read="居宅訪問", body=OTHER))
    p, client = made(s, c, WireSection(key="a2", items=[item("港の倉庫で夜勤をしていた", "港の倉庫で夜勤をしていた", file="支援経過（アプリの記録）")]))
    assert "港の倉庫" not in str(client.calls[0]["messages"])
    cell = p.sections["a2"][0]
    assert NOT_FOUND in cell["flag"] and cell["file"] == ""


def test_diagnosis_like_text_is_not_put_in(env):
    s, c = env
    p, _ = made(s, c, WireSection(key="a10", items=[item("認知症の疑いがある", "膝が痛く、長く歩くと休む")]))
    assert p.sections["a10"][0]["text"] == BLANK and p.held


def test_word_has_every_item_name(env):
    s, c = env
    p, _ = made(s, c, WireSection(key="a11", items=[item("階段は手すりを使う", "階段は手すりを使う")]))
    data = to_docx(p)
    text = "\n".join(cell.text for t in docx.Document(io.BytesIO(data)).tables for row in t.rows for cell in row.cells)
    assert data[:2] == b"PK" and all(n in text for n in NAMES) and "階段は手すりを使う" in text


def test_page_shows_the_items_and_blank_cells_are_red(env):
    s, c = env
    p, _ = made(s, c, WireSection(key="a11", items=[item("階段は手すりを使う", "階段は手すりを使う")]))
    html = TestClient(create_app(s)).get(f"/careplans/{p.id}").text
    assert "課題分析（アセスメント）に関する項目" in html and all(n in html for n in NAMES)
    assert "第２表" not in html and "アセスメントと読み比べる" not in html
    assert 'cell-box blank-cell' in html and f'action="/users/U01/assessment"' in html


def test_button_on_the_user_page_makes_one(env):
    s, c = env
    client = TestClient(create_app(s, client_factory=lambda: FakeClient(answer(assess_wire(WireSection(key="a11", items=[item("階段は手すりを使う", "階段は手すりを使う")]))))))
    shelf = client.get("/users/U01").text.split('id="forms"')[1].split("</section>")[0]
    row = shelf.split("アセスメント")[1]
    assert 'action="/users/U01/assessment"' in row and "下書きを作る" in row
    r = client.post("/users/U01/assessment", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/careplans/")
    kinds = [p.kind for p in list_care_plans(c, "U01")]
    assert kinds == ["assessment"]
    shelf = client.get("/users/U01").text.split('id="forms"')[1].split("</section>")[0]
    assert shelf.split("アセスメント")[1].count('href="/careplans/') == 1
    assert shelf.split("ケアプラン")[1].split("アセスメント")[0].count('href="/careplans/') == 0  # ケアプランの欄には並べない


def test_desk_can_make_one():
    assert any(t["name"] == "make_assessment" for t in TOOLS)
