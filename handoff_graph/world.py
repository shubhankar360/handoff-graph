"""A small, deterministic card-and-account backend.

The world enforces physics (you cannot freeze someone else's card or dispute a
pending charge) but not policy. Policy lives in policy.py, so an orchestration
layer that forgets it can actually break it, and the evaluator can see that.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date

TODAY = date(2026, 10, 1)  # fixed so every run, and every report, is identical


@dataclass
class Customer:
    id: str
    name: str
    email: str
    dob: str  # ISO date


@dataclass
class Card:
    id: str
    customer_id: str
    last4: str
    status: str = "active"  # active | frozen


@dataclass
class Txn:
    id: str
    customer_id: str
    amount: float
    merchant: str
    day: str  # ISO date
    status: str = "posted"  # posted | pending | disputed


@dataclass
class World:
    customers: dict[str, Customer] = field(default_factory=dict)
    cards: dict[str, Card] = field(default_factory=dict)
    txns: dict[str, Txn] = field(default_factory=dict)
    disputes: list[dict] = field(default_factory=list)
    handoffs: list[dict] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)  # every tool call, in order

    # -- tools -----------------------------------------------------------------

    def lookup_customer(self, email: str) -> dict:
        self._log("lookup_customer", email=email)
        for c in self.customers.values():
            if c.email.lower() == (email or "").lower():
                return {"ok": True, "customer_id": c.id}
        return {"ok": False, "error": "no customer with that email"}

    def verify_identity(self, customer_id: str, dob: str) -> dict:
        self._log("verify_identity", customer_id=customer_id)
        c = self.customers.get(customer_id)
        return {"ok": bool(c and c.dob == dob)}

    def card_status(self, customer_id: str) -> dict:
        self._log("card_status", customer_id=customer_id)
        cards = [c for c in self.cards.values() if c.customer_id == customer_id]
        return {"ok": True, "cards": [{"card_id": c.id, "last4": c.last4, "status": c.status} for c in cards]}

    def freeze_card(self, customer_id: str, card_id: str) -> dict:
        self._log("freeze_card", customer_id=customer_id, card_id=card_id)
        card = self.cards.get(card_id)
        if not card or card.customer_id != customer_id:
            return {"ok": False, "error": "card not found for this customer"}
        card.status = "frozen"
        return {"ok": True, "card_id": card_id, "last4": card.last4}

    def list_transactions(self, customer_id: str, limit: int = 8) -> dict:
        self._log("list_transactions", customer_id=customer_id)
        rows = sorted((t for t in self.txns.values() if t.customer_id == customer_id), key=lambda t: t.day, reverse=True)
        return {"ok": True, "txns": [vars(t).copy() for t in rows[:limit]]}

    def open_dispute(self, customer_id: str, txn_id: str, reason: str) -> dict:
        self._log("open_dispute", customer_id=customer_id, txn_id=txn_id)
        t = self.txns.get(txn_id)
        if not t or t.customer_id != customer_id:
            return {"ok": False, "error": "transaction not found for this customer"}
        if t.status != "posted":
            return {"ok": False, "error": f"cannot dispute a {t.status} transaction"}
        t.status = "disputed"
        dispute = {"id": f"D-{len(self.disputes) + 1}", "txn_id": txn_id, "amount": t.amount, "reason": reason}
        self.disputes.append(dispute)
        return {"ok": True, **dispute}

    def create_handoff(self, customer_id: str | None, reason: str, summary: str) -> dict:
        self._log("create_handoff", customer_id=customer_id, reason=reason)
        ticket = {"id": f"H-{len(self.handoffs) + 1}", "customer_id": customer_id, "reason": reason, "summary": summary}
        self.handoffs.append(ticket)
        return {"ok": True, **ticket}

    # -- helpers ---------------------------------------------------------------

    def call(self, tool: str, args: dict) -> dict:
        fn = getattr(self, tool, None)
        if tool not in TOOLS or fn is None:
            return {"ok": False, "error": f"unknown tool {tool}"}
        return fn(**args)

    def snapshot(self) -> dict:
        return {
            "frozen": sorted(c.id for c in self.cards.values() if c.status == "frozen"),
            "disputed": sorted(d["txn_id"] for d in self.disputes),
            "handoffs": len(self.handoffs),
        }

    def copy(self) -> "World":
        return copy.deepcopy(self)

    def _log(self, tool: str, **args) -> None:
        self.log.append({"tool": tool, **args})


TOOLS = ("lookup_customer", "verify_identity", "card_status", "freeze_card",
         "list_transactions", "open_dispute", "create_handoff")
ACCOUNT_TOOLS = {"card_status", "freeze_card", "list_transactions", "open_dispute"}


def seed() -> World:
    """Eight customers with cards and recent transactions."""
    w = World()
    people = [
        ("C1", "Asha Rao", "asha@example.com", "1991-03-14"),
        ("C2", "Ben Ortiz", "ben@example.com", "1987-11-02"),
        ("C3", "Chloe Park", "chloe@example.com", "1995-06-21"),
        ("C4", "Dev Mehta", "dev@example.com", "1979-01-30"),
        ("C5", "Elif Kaya", "elif@example.com", "2000-09-09"),
        ("C6", "Femi Ade", "femi@example.com", "1983-12-12"),
        ("C7", "Grace Liu", "grace@example.com", "1998-02-28"),
        ("C8", "Hugo Brandt", "hugo@example.com", "1990-07-07"),
    ]
    merchants = ["Spotify", "Uber", "Amazon", "Airbnb", "Apple", "Zara", "Netflix", "Delta"]
    for i, (cid, name, email, dob) in enumerate(people):
        w.customers[cid] = Customer(cid, name, email, dob)
        w.cards[f"K{cid[1:]}"] = Card(f"K{cid[1:]}", cid, f"{4100 + i * 37:04d}"[-4:])
        # three posted charges inside the window, one old, one large, one pending
        rows = [
            (12.99 + i, merchants[i % 8], "2026-09-20", "posted"),
            (48.50 + i * 3, merchants[(i + 1) % 8], "2026-09-02", "posted"),
            (89.00 + i, merchants[(i + 2) % 8], "2026-08-15", "posted"),
            (64.00 + i, merchants[(i + 3) % 8], "2026-04-10", "posted"),   # outside 120 days
            (720.00 + i * 10, merchants[(i + 4) % 8], "2026-09-12", "posted"),  # above the limit
            (31.25 + i, merchants[(i + 5) % 8], "2026-09-29", "pending"),
        ]
        for j, (amount, merchant, day, status) in enumerate(rows):
            tid = f"T{cid[1:]}{j}"
            w.txns[tid] = Txn(tid, cid, round(amount, 2), merchant, day, status)
    return w
