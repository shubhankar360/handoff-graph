r"""The orchestration layer, as a LangGraph state machine.

    route -> verify -> plan -> guard -> act ---+
                 \        ^                    |
                  \       +--------------------+   (loop until the planner is done)
                   \           \-> handoff  (pauses for a human, then files the ticket)
                    +-> respond <-+

Each customer message is one invocation on a thread; the checkpointer carries
intent, slots and verification across turns, which is what lets "my card was
stolen" in one message and the email and date of birth in the next end in a
frozen card.

Two switches exist for the evaluation:
  structured=False  removes the verify gate, so verification becomes the model's job
  guarded=False     removes the guard node, so policy becomes the model's job
"""
from __future__ import annotations

import operator
from typing import Annotated, Callable, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import policy
from .models import NEEDS_ACCOUNT
from .tracing import Tracer
from .world import World

MAX_STEPS = 6  # tool calls per turn; a planner that loops gets cut off and the turn ends


class State(TypedDict, total=False):
    messages: Annotated[list, operator.add]
    intent: str | None
    slots: dict
    customer_id: str | None
    verified: bool
    verify_failed: bool
    human_requested: bool
    observations: list                     # tool results, this turn only
    executed: Annotated[list, operator.add]  # every action that ran, with the context it ran under
    blocked: Annotated[list, operator.add]   # every action the guard stopped
    turn_blocked: list
    pending: dict | None
    handoff: dict | None
    steps: int
    reply: str


def build(world: World, model, *, structured: bool = True, guarded: bool = True,
          human_in_loop: bool = True, tracer: Tracer | None = None, checkpointer=None):
    tracer = tracer or Tracer()

    def ctx(state: State) -> dict:
        return {"verified": state.get("verified", False), "customer_id": state.get("customer_id"),
                "human_requested": state.get("human_requested", False)}

    def route(state: State) -> dict:
        r = model.route(state["messages"][-1]["content"], state)
        intent = r["intent"] or state.get("intent") or "other"  # a bare "it's me, born 1991..." keeps the open request
        return {
            "intent": intent,
            "slots": {**state.get("slots", {}), **r["slots"]},
            "human_requested": state.get("human_requested", False) or intent == "human",
            "observations": [], "turn_blocked": [], "pending": None, "steps": 0,
            "verify_failed": False, "handoff": None,
        }

    def verify(state: State) -> dict:
        if state["intent"] not in NEEDS_ACCOUNT or state.get("verified") or state.get("human_requested"):
            return {}
        slots = state["slots"]
        if not (slots.get("email") and slots.get("dob")):
            return {}
        found = world.lookup_customer(slots["email"])
        if found["ok"] and world.verify_identity(found["customer_id"], slots["dob"])["ok"]:
            return {"customer_id": found["customer_id"], "verified": True}
        # forget the wrong details so the next attempt starts clean
        return {"verify_failed": True, "slots": {k: v for k, v in slots.items() if k not in ("email", "dob")}}

    def after_verify(state: State) -> str:
        if not state.get("human_requested") and state["intent"] in NEEDS_ACCOUNT and not state.get("verified"):
            return "respond"
        return "plan"

    def plan(state: State) -> dict:
        if state.get("steps", 0) >= MAX_STEPS:
            return {"pending": None}
        return {"pending": model.next_action(state), "steps": state.get("steps", 0) + 1}

    def dispatch(state: State) -> str:
        a = state.get("pending")
        if not a:
            return "respond"
        return "handoff" if a["tool"] == "create_handoff" else "act"

    def guard(state: State) -> dict:
        a = state["pending"]
        v = policy.check(a, ctx(state), world)
        if v.allowed:
            return {}
        rec = {"tool": a["tool"], "rule": v.rule}
        update = {"blocked": [rec], "turn_blocked": state.get("turn_blocked", []) + [rec], "pending": None}
        if v.fallback == "handoff":
            said = " | ".join(m["content"] for m in state["messages"] if m["role"] == "user")
            update["pending"] = {"tool": "create_handoff", "args": {
                "customer_id": state.get("customer_id"), "reason": policy.RULES[v.rule], "summary": said[-400:]}}
        return update

    def act(state: State) -> dict:
        a = state["pending"]
        result = world.call(a["tool"], a["args"])
        update = {
            "observations": state.get("observations", []) + [{"tool": a["tool"], "args": a["args"], "result": result}],
            "executed": [{**a, "ok": bool(result.get("ok")), "ctx": ctx(state)}],
            "pending": None,
        }
        if a["tool"] == "lookup_customer" and result.get("ok"):
            update["customer_id"] = result["customer_id"]
        if a["tool"] == "verify_identity":
            update["verified"] = bool(result.get("ok"))
            update["verify_failed"] = not result.get("ok")
        return update

    def handoff(state: State) -> dict:
        a = state["pending"]
        # interrupt() re-runs this node from the top on resume, so ask the human
        # first and file the ticket afterwards; otherwise every resume files another one
        decision = interrupt({"reason": a["args"]["reason"], "summary": a["args"]["summary"]}) if human_in_loop else None
        result = world.create_handoff(**a["args"])
        return {
            "handoff": {**result, "accepted_by": (decision or {}).get("agent")},
            "observations": state.get("observations", []) + [{"tool": "create_handoff", "args": a["args"], "result": result}],
            "executed": [{**a, "ok": True, "ctx": ctx(state)}],
            "pending": None,
        }

    def respond(state: State) -> dict:
        text = model.reply(state)
        return {"messages": [{"role": "assistant", "content": text}], "reply": text}

    g = StateGraph(State)
    for name, fn in [("route", route), ("verify", verify), ("plan", plan), ("guard", guard),
                     ("act", act), ("handoff", handoff), ("respond", respond)]:
        g.add_node(name, tracer.wrap(name, fn))

    g.add_edge(START, "route")
    if structured:
        g.add_edge("route", "verify")
        g.add_conditional_edges("verify", after_verify, ["plan", "respond"])
    else:
        g.add_edge("route", "plan")
    if guarded:
        g.add_conditional_edges("plan", lambda s: "guard" if s.get("pending") else "respond", ["guard", "respond"])
        g.add_conditional_edges("guard", dispatch, ["act", "handoff", "respond"])
    else:
        g.add_conditional_edges("plan", dispatch, ["act", "handoff", "respond"])
    g.add_edge("act", "plan")
    g.add_edge("handoff", "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


def send(app, thread_id: str, text: str, on_handoff: Callable[[dict], dict] | None = None) -> dict:
    """One customer message in, final state out. If the graph pauses for a
    handoff, `on_handoff` plays the human; by default an agent called 'queue'
    accepts it."""
    cfg = {"configurable": {"thread_id": thread_id}}
    out = app.invoke({"messages": [{"role": "user", "content": text}]}, cfg)
    while out.get("__interrupt__"):
        payload = out["__interrupt__"][0].value
        decision = (on_handoff or (lambda p: {"agent": "queue"}))(payload)
        out = app.invoke(Command(resume=decision), cfg)
    return out
