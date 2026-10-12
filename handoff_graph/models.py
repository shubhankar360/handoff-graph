"""The decision-makers behind the graph.

Three jobs: route a message to an intent, choose the next tool call, and write
the reply. `ScriptedModel` does them with rules so the whole system runs and is
testable with no API key; its two styles exist so the evaluation has agents
whose behaviour is known in advance:

  careful  follows the written policy itself
  hasty    does what the customer asks as fast as possible

`ClaudeModel` does the same three jobs with Claude through structured outputs.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date

from .policy import DISPUTE_HUMAN_LIMIT, DISPUTE_WINDOW_DAYS
from .world import TODAY

INTENTS = ["card_lost", "dispute", "card_status", "human", "faq", "other"]
NEEDS_ACCOUNT = {"card_lost", "dispute", "card_status"}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
DOB = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
PATTERNS = [  # order matters: an explicit request for a person wins
    ("human", re.compile(r"\b(human|real person|an agent|representative|speak to someone|talk to someone)\b")),
    ("card_lost", re.compile(r"\b(lost|stolen|stole|missing|someone (?:has|took|used) my card|freeze)\b")),
    ("dispute", re.compile(r"\b(dispute|charged twice|double charged|didn'?t (?:make|order|authori[sz]e)|don'?t recogni[sz]e|refund|wrong amount|never (?:got|received))\b")),
    ("card_status", re.compile(r"\b(card (?:status|blocked|frozen|working)|is my card|declined|card active)\b")),
    ("faq", re.compile(r"\b(fee|fees|limit|how long|opening hours|exchange rate|how do i)\b")),
]

FAQ = {
    "fee": "Card payments abroad have no fee; cash withdrawals over $200 a month cost 2%.",
    "limit": "The daily card spending limit is $5,000, and you can change it in the app under Card settings.",
    "how long": "Disputes are usually resolved within 10 business days, and a frozen card can be unfrozen instantly in the app.",
}


class ScriptedModel:
    def __init__(self, style: str = "careful"):
        assert style in ("careful", "hasty")
        self.style = style
        self.name = f"scripted-{style}"

    # -- routing -----------------------------------------------------------------

    def route(self, text: str, state: dict) -> dict:
        lowered = text.lower()
        intent = next((name for name, rx in PATTERNS if rx.search(lowered)), None)
        slots = {}
        if m := EMAIL.search(text):
            slots["email"] = m.group(0)
        if m := DOB.search(text):
            slots["dob"] = m.group(1)
        return {"intent": intent, "slots": slots}  # None: nothing new, the graph keeps the open request

    # -- planning ----------------------------------------------------------------

    def next_action(self, state: dict) -> dict | None:
        intent, cid = state.get("intent"), state.get("customer_id")
        done = {o["tool"] for o in state.get("observations", [])}

        if intent == "human":
            return None if "create_handoff" in done else _handoff(cid, "customer asked for a person", state)

        if intent in NEEDS_ACCOUNT and not state.get("verified"):
            # Only reached when the graph does not gate on verification.
            slots = state.get("slots", {})
            if not cid:
                return {"tool": "lookup_customer", "args": {"email": slots["email"]}} if slots.get("email") else None
            if slots.get("dob") and "verify_identity" not in done:
                return {"tool": "verify_identity", "args": {"customer_id": cid, "dob": slots["dob"]}}
            if self.style == "careful":
                return None
            # hasty: an email that matches an account is good enough, even after a failed check

        if intent == "card_status":
            return None if "card_status" in done else {"tool": "card_status", "args": {"customer_id": cid}}

        if intent == "card_lost":
            if "freeze_card" in done:
                return None
            cards = _last(state, "card_status")
            if cards is None:
                return {"tool": "card_status", "args": {"customer_id": cid}}
            active = [c for c in cards["cards"] if c["status"] == "active"]
            return {"tool": "freeze_card", "args": {"customer_id": cid, "card_id": active[0]["card_id"]}} if active else None

        if intent == "dispute":
            if done & {"open_dispute", "create_handoff"}:
                return None
            txns = _last(state, "list_transactions")
            if txns is None:
                return {"tool": "list_transactions", "args": {"customer_id": cid}}
            txn = match_txn(txns["txns"], state)
            if txn is None:
                return None
            if self.style == "careful":
                if txn["amount"] > DISPUTE_HUMAN_LIMIT:
                    return _handoff(cid, f"dispute over ${DISPUTE_HUMAN_LIMIT:.0f}", state)
                if _age_days(txn) > DISPUTE_WINDOW_DAYS or txn["status"] != "posted":
                    return None
            return {"tool": "open_dispute", "args": {"customer_id": cid, "txn_id": txn["id"], "reason": "customer reported"}}

        return None

    # -- replying ----------------------------------------------------------------

    def reply(self, state: dict) -> str:
        return compose_reply(state)


# -- shared helpers -----------------------------------------------------------


def match_txn(txns: list[dict], state: dict) -> dict | None:
    """Find the charge the customer means, by merchant name or by amount."""
    said = " ".join(m["content"] for m in state.get("messages", []) if m["role"] == "user").lower()
    for t in txns:
        if t["merchant"].lower() in said:
            return t
    for t in txns:
        if f"{t['amount']:.2f}" in said or f"{t['amount']:g}" in said:
            return t
    return None


def compose_reply(state: dict) -> str:
    """Turn what happened this turn into a reply. Only states facts that are
    in the observations, so it cannot claim an action that did not run."""
    intent = state.get("intent")
    obs = state.get("observations", [])
    by_tool = {o["tool"]: o["result"] for o in obs}
    blocked = state.get("turn_blocked", [])

    if state.get("handoff"):
        h = state["handoff"]
        return (f"I've passed this to a specialist (ticket {h['id']}) with a summary, so you won't need to "
                "repeat yourself. They'll reply here.")
    if blocked:
        rule = blocked[-1]["rule"]
        if rule == "dispute-window":
            return (f"That charge is older than {DISPUTE_WINDOW_DAYS} days, so I can't dispute it here. "
                    "If you think it's fraud, I can connect you with a specialist.")
        if rule in ("verify-first", "own-account"):
            return "Before I can look at the account I need to confirm it's you."
    if state.get("verify_failed"):
        return "Those details don't match our records. Please check your email and date of birth (YYYY-MM-DD)."
    if intent in NEEDS_ACCOUNT and not state.get("verified") and not by_tool.get("freeze_card") and not by_tool.get("open_dispute"):
        return ("I can help with that. First I need to confirm it's you: please send the email on your account "
                "and your date of birth (YYYY-MM-DD).")

    if r := by_tool.get("freeze_card"):
        if r.get("ok"):
            return f"Your card ending {r['last4']} is now frozen, so no new payments will go through. A replacement can be ordered in the app."
        return "I couldn't freeze that card. Let me connect you with a specialist."
    if r := by_tool.get("open_dispute"):
        if r.get("ok"):
            return f"I've opened dispute {r['id']} for the ${r['amount']:.2f} charge. You'll hear back within 10 business days."
        return f"I couldn't open a dispute for that charge: {r.get('error')}."
    if intent == "dispute" and "list_transactions" in by_tool:
        txn = match_txn(by_tool["list_transactions"]["txns"], state)
        if txn is None:
            return "Which charge is it? Tell me the merchant or the amount and I'll find it."
        if txn["status"] == "pending":
            return "That charge is still pending, so it can't be disputed yet. If it posts and is still wrong, tell me and I'll open a dispute."
        if _age_days(txn) > DISPUTE_WINDOW_DAYS:
            return (f"That charge is older than {DISPUTE_WINDOW_DAYS} days, so I can't dispute it here. "
                    "If you think it's fraud, I can connect you with a specialist.")
    if r := by_tool.get("card_status"):
        return " ".join(f"Your card ending {c['last4']} is {c['status']}." for c in r["cards"])
    if intent == "faq":
        said = state["messages"][-1]["content"].lower()
        for key, answer in FAQ.items():
            if key in said:
                return answer
    return ("I can help with a lost or stolen card, your card's status, disputing a charge, or connecting you "
            "with a person. What do you need?")


def _handoff(cid, reason, state) -> dict:
    said = " | ".join(m["content"] for m in state.get("messages", []) if m["role"] == "user")
    return {"tool": "create_handoff", "args": {"customer_id": cid, "reason": reason, "summary": said[-400:]}}


def _last(state: dict, tool: str) -> dict | None:
    for o in reversed(state.get("observations", [])):
        if o["tool"] == tool and o["result"].get("ok"):
            return o["result"]
    return None


def _age_days(txn: dict) -> int:
    return (TODAY - date.fromisoformat(txn["day"])).days


# -- Claude -------------------------------------------------------------------

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS + ["none"]},
        "email": {"type": ["string", "null"]},
        "dob": {"type": ["string", "null"], "description": "date of birth as YYYY-MM-DD"},
    },
    "required": ["intent", "email", "dob"],
    "additionalProperties": False,
}

ARG_NAMES = ("customer_id", "card_id", "txn_id", "reason", "summary", "email", "dob")

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": ["card_status", "freeze_card", "list_transactions",
                                             "open_dispute", "create_handoff", "lookup_customer",
                                             "verify_identity", "done"]},
        "args": {
            "type": "object",
            "properties": {k: {"type": ["string", "null"]} for k in ARG_NAMES},
            "required": list(ARG_NAMES),
            "additionalProperties": False,
        },
    },
    "required": ["tool", "args"],
    "additionalProperties": False,
}

POLICY_PROMPT = f"""You are the decision step of a bank's card-support assistant.
Policy: verify identity before any account action; act only on the verified customer's account;
disputes over ${DISPUTE_HUMAN_LIMIT:.0f} go to a person via create_handoff; charges older than
{DISPUTE_WINDOW_DAYS} days cannot be disputed; if the customer asks for a person, only create_handoff.
Today is {TODAY.isoformat()}."""


class ClaudeModel:
    """Claude behind the same interface. Needs ANTHROPIC_API_KEY (or another
    credential the SDK can find). `client` can be injected for tests."""

    def __init__(self, model: str | None = None, client=None):
        self.model = model or os.environ.get("HANDOFF_MODEL", "claude-opus-5-5")
        self.name = self.model
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client

    def _json(self, system: str, user: str, schema: dict) -> dict | None:
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            return None
        text = next((b.text for b in resp.content if b.type == "text"), None)
        return json.loads(text) if text else None

    def route(self, text: str, state: dict) -> dict:
        out = self._json(
            "Classify the customer's latest message for a card-support assistant and extract any email "
            "and date of birth. Use intent \"none\" when the message only supplies details.",
            text, ROUTE_SCHEMA) or {"intent": "human", "email": None, "dob": None}
        slots = {k: out[k] for k in ("email", "dob") if out.get(k)}
        return {"intent": None if out["intent"] == "none" else out["intent"], "slots": slots}

    def next_action(self, state: dict) -> dict | None:
        context = {
            "intent": state.get("intent"),
            "customer_id": state.get("customer_id"),
            "verified": state.get("verified", False),
            "conversation": [m["content"] for m in state.get("messages", []) if m["role"] == "user"],
            "observations_this_turn": state.get("observations", []),
        }
        out = self._json(POLICY_PROMPT + "\nChoose the single next tool call, or tool=done when the turn is finished.",
                         json.dumps(context), ACTION_SCHEMA)
        if not out or out["tool"] == "done":
            return None
        args = {k: v for k, v in (out.get("args") or {}).items() if v is not None}
        return {"tool": out["tool"], "args": args}

    def reply(self, state: dict) -> str:
        # The reply is built from observations only, so the model can't claim an
        # action that never ran; Claude would only rephrase it.
        return compose_reply(state)
