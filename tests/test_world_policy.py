from handoff_graph import policy
from handoff_graph.world import seed


def test_world_enforces_physics_not_policy():
    w = seed()
    assert not w.freeze_card("C1", "K2")["ok"]              # someone else's card
    assert not w.open_dispute("C1", "T15", "x")["ok"]       # pending charge
    big = w.open_dispute("C1", "T14", "x")                  # over the limit: policy, not physics
    assert big["ok"] and w.txns["T14"].status == "disputed"
    assert not w.open_dispute("C1", "T14", "x")["ok"]       # already disputed


def ctx(**kw):
    return {"verified": True, "customer_id": "C1", "human_requested": False, **kw}


def test_each_rule():
    w = seed()
    chk = lambda tool, args, **c: policy.check({"tool": tool, "args": args}, ctx(**c), w)  # noqa: E731
    assert chk("freeze_card", {"customer_id": "C1", "card_id": "K1"}, verified=False).rule == "verify-first"
    assert chk("freeze_card", {"customer_id": "C2", "card_id": "K2"}).rule == "own-account"
    over = chk("open_dispute", {"customer_id": "C1", "txn_id": "T14"})
    assert (over.rule, over.fallback) == ("dispute-limit", "handoff")
    assert chk("open_dispute", {"customer_id": "C1", "txn_id": "T13"}).rule == "dispute-window"
    assert chk("freeze_card", {"customer_id": "C1", "card_id": "K1"}, human_requested=True).rule == "human-requested"
    assert chk("create_handoff", {"customer_id": "C1"}, human_requested=True).allowed
    assert chk("open_dispute", {"customer_id": "C1", "txn_id": "T11"}).allowed


def test_audit_replays_the_log_independently():
    w = seed()
    log = [{"tool": "freeze_card", "args": {"customer_id": "C1", "card_id": "K1"}, "ctx": {"verified": False, "customer_id": "C1"}},
           {"tool": "card_status", "args": {"customer_id": "C1"}, "ctx": {"verified": True, "customer_id": "C1"}}]
    assert policy.audit(log, w) == [{"step": 0, "tool": "freeze_card", "rule": "verify-first"}]
