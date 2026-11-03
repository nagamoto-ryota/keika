import importlib.util
import sys
from pathlib import Path

import yaml

from keika import rules as rules_module
from test_reading import FakeClient, answer

PATH = Path(__file__).parent.parent / "tools" / "rule_update" / "update_rules.py"
spec = importlib.util.spec_from_file_location("update_rules", PATH)
ur = importlib.util.module_from_spec(spec)
sys.modules["update_rules"] = ur
spec.loader.exec_module(ur)

NOTICE = "指定居宅介護支援事業者は、利用者の身体的拘束等を行う場合には、その態様及び時間を記録しなければならない。"
URL = "https://www.mhlw.go.jp/content/example.pdf"


def proposal(**kw):
    data = dict(title="身体的拘束の記録", plain="拘束をした時は態様と時間を記録します。", legal_basis="居宅基準13条", done_when="記録があること",
                applies_to="both", effective_from="2024-04-01", quote="その態様及び時間を記録しなければならない")
    return ur.WireProposal(**(data | kw))


def test_add_rule_needs_source_url():
    assert "URL" in ur.validate(proposal(), "", NOTICE)
    assert ur.validate(proposal(), "通知のどこか", NOTICE)
    assert ur.validate(proposal(), URL, NOTICE) is None


def test_quote_must_be_in_notice():
    assert "原文" in ur.validate(proposal(quote="書いていない義務"), URL, NOTICE)


def test_medical_judgement_is_not_a_rule():
    assert ur.validate(proposal(plain="すぐ受診が必要か判断します。"), URL, NOTICE)


def test_second_reader_agreement_is_accepted():
    client = FakeClient(answer(ur.WireVerdict(supported=True, quote="態様及び時間を記録", reason="原文のとおり")))
    routed = ur.route(client, NOTICE, URL, [proposal()])
    assert len(routed.accepted) == 1 and routed.to_human == []


def test_second_reader_disagreement_goes_to_human():
    client = FakeClient(answer(ur.WireVerdict(supported=False, quote="", reason="努力義務です")))
    routed = ur.route(client, NOTICE, URL, [proposal()])
    assert routed.accepted == [] and "努力義務" in routed.to_human[0][1]


def test_second_reader_with_invented_quote_goes_to_human():
    client = FakeClient(answer(ur.WireVerdict(supported=True, quote="原文に無い語句です", reason="あります")))
    assert ur.route(client, NOTICE, URL, [proposal()]).accepted == []


def test_second_reader_does_not_see_first_readers_quote():
    client = FakeClient(answer(ur.WireVerdict(supported=True, quote="態様及び時間を記録", reason="")))
    p = proposal(quote="その態様及び時間を記録しなければならない", plain="拘束を記録します。")
    ur.second_read(client, "別の文。" + NOTICE, p)
    claim = client.calls[0]["messages"][0]["content"][0]["text"].split("## 通知の原文")[0]
    assert "しなければならない" not in claim


def test_bad_proposal_never_calls_second_reader():
    client = FakeClient(RuntimeError("呼ばれないはず"))
    routed = ur.route(client, NOTICE, "", [proposal()])
    assert len(routed.to_human) == 1 and client.calls == []


def test_rows_get_next_id_source_and_mark():
    rows = ur.to_rows([proposal()], [{"id": "R47"}, {"id": "R09"}], "R6の通知", URL)
    assert rows[0]["id"] == "R48" and rows[0]["source_url"] == URL and rows[0]["added_by"] == "R6の通知"
    assert rows[0]["judge"] == "claude" and rows[0]["basis_confirmed"] is False and rows[0]["effective_from"] == "2024-04-01"


def test_appended_rows_load_as_rules(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("# 注釈\n- id: R01\n  title: t\n", encoding="utf-8")
    ur.append_rows(path, ur.to_rows([proposal()], [{"id": "R01"}], "通知", URL))
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# 注釈")
    rows = yaml.safe_load(text)
    assert rules_module.Rule(**rows[1]).added_by == "通知"


def test_report_states_replay():
    routed = ur.Routed([proposal()], [(proposal(title="別"), "確かめられません")])
    text = ur.report("通知", URL, [{"id": "R48"}], routed, 1.0, ["R16"])
    assert "過去の通知を流した実行" in text and "R16" in text and "人に回す" in text and URL in text


def test_view_says_which_notice_added_the_rule(monkeypatch):
    from keika.findings import Finding
    from keika.responses import Item
    from keika.settings import Settings
    from keika.web import app as web
    row = rules_module.Rule(id="R48", title="t", plain="p", layer="l", legal_basis="b", basis_confirmed=False, source="s", judge="claude",
                            done_when="d", active=True, added_by="R6の通知")
    monkeypatch.setattr(web, "get_rule", lambda _: row)
    f = Finding(rule_id="R48", user_id="U01", kind="finding", message="m", searched_scope="x")
    item = Item(finding=f, fingerprint="fp", evidence_changed=False, record_id=None)
    assert web._view(Settings(drive_root=Path("."), db_path=Path("x.db")), item, [], 2026)["added_by"] == "R6の通知"
