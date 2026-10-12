from handoff_graph import ScriptedModel, build, seed, send


def test_context_carries_across_turns():
    w = seed()
    app = build(w, ScriptedModel("careful"))
    first = send(app, "t", "I think I lost my card, can you freeze it?")
    assert first["intent"] == "card_lost" and not first.get("verified")
    assert w.snapshot()["frozen"] == []
    second = send(app, "t", "Sure: chloe@example.com, 1995-06-21")
    assert second["verified"] and w.snapshot()["frozen"] == ["K3"]
    assert "frozen" in second["reply"]


def test_wrong_details_are_forgotten_and_nothing_runs():
    w = seed()
    app = build(w, ScriptedModel("careful"))
    out = send(app, "t", "My card was stolen. asha@example.com, born 1900-01-01")
    assert out["verify_failed"] and "email" not in out["slots"] and w.snapshot()["frozen"] == []


def test_handoff_pauses_for_a_person_and_files_one_ticket():
    w = seed()
    app = build(w, ScriptedModel("careful"))
    cfg = {"configurable": {"thread_id": "h"}}
    paused = app.invoke({"messages": [{"role": "user", "content": "Can I talk to a real person please?"}]}, cfg)
    assert paused["__interrupt__"][0].value["reason"] == "customer asked for a person"
    assert w.handoffs == []  # nothing filed until a person accepts
    seen = []
    out = send(app, "h2", "I want to speak to someone, this is urgent.", on_handoff=lambda p: seen.append(p) or {"agent": "dana"})
    assert len(seen) == 1 and len(w.handoffs) == 1 and out["handoff"]["accepted_by"] == "dana"


def test_guard_turns_an_over_limit_dispute_into_a_handoff():
    w = seed()
    app = build(w, ScriptedModel("hasty"))
    out = send(app, "t", "I want to dispute the Apple charge, I never received anything. asha@example.com, born 1991-03-14")
    assert out["blocked"] == [{"tool": "open_dispute", "rule": "dispute-limit"}]
    assert w.snapshot() == {"frozen": [], "disputed": [], "handoffs": 1}


def test_without_the_guard_the_same_planner_breaks_policy():
    w = seed()
    app = build(w, ScriptedModel("hasty"), guarded=False)
    send(app, "t", "I want to dispute the Apple charge, I never received anything. asha@example.com, born 1991-03-14")
    assert w.snapshot()["disputed"] == ["T14"]


class Looper(ScriptedModel):
    def next_action(self, state):
        return {"tool": "card_status", "args": {"customer_id": state.get("customer_id")}}


def test_a_planner_that_never_stops_is_cut_off():
    w = seed()
    app = build(w, Looper())
    out = send(app, "t", "Is my card working? asha@example.com, born 1991-03-14")
    assert len(out["executed"]) == 6 and out["reply"]
