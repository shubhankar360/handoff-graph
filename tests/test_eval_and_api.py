import json
import pathlib
from types import SimpleNamespace

from fastapi.testclient import TestClient

from handoff_graph.evaluate import ROOT, report, run_all
from handoff_graph.models import ClaudeModel


def test_committed_report_matches_a_fresh_run():
    assert report(run_all()) == (ROOT / "results" / "report.md").read_text()


def test_committed_scenarios_match_the_generator(tmp_path):
    import runpy
    before = (ROOT / "data" / "scenarios.json").read_text()
    runpy.run_path(str(ROOT / "data" / "build.py"))
    assert (ROOT / "data" / "scenarios.json").read_text() == before


class FakeMessages:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if r == "REFUSE":
            return SimpleNamespace(stop_reason="refusal", content=[])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(r))])


def fake_client(replies):
    msgs = FakeMessages(replies)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def test_claude_model_requests_structured_output_and_maps_none():
    client, msgs = fake_client([{"intent": "none", "email": "a@b.co", "dob": None}])
    m = ClaudeModel(client=client)
    assert m.route("it's a@b.co", {}) == {"intent": None, "slots": {"email": "a@b.co"}}
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"


def test_claude_model_refusal_routes_to_a_person_and_done_ends_the_turn():
    client, _ = fake_client(["REFUSE", {"tool": "done", "args": {}}])
    m = ClaudeModel(client=client)
    assert m.route("...", {})["intent"] == "human"
    assert m.next_action({"messages": []}) is None


def test_claude_model_drops_null_args():
    client, _ = fake_client([{"tool": "card_status", "args": {"customer_id": "C1", "card_id": None}}])
    assert ClaudeModel(client=client).next_action({"messages": []}) == {"tool": "card_status", "args": {"customer_id": "C1"}}


def test_http_flow_with_a_paused_handoff():
    import app as server
    c = TestClient(server.app)
    assert c.get("/health").json()["ok"]
    r = c.post("/chat", json={"thread_id": "x", "message": "My card was stolen. ben@example.com, born 1987-11-02"}).json()
    assert r["verified"] and "frozen" in r["reply"]
    r = c.post("/chat", json={"thread_id": "y", "message": "Can I talk to a real person please?"}).json()
    assert r["reply"] is None and r["handoff_pending"]
    r = c.post("/handoff/resume", json={"thread_id": "y", "agent": "dana"}).json()
    assert "specialist" in r["reply"]
