"""Generate data/scenarios.json from the seeded world.

Each scenario is a scripted customer: a list of messages, the intent of the
first one, and the end state a correct agent leaves behind. Regenerate with
`python data/build.py`; CI checks the committed file matches.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from handoff_graph.world import seed  # noqa: E402

w = seed()
C = list(w.customers.values())
merchant = lambda i, j: w.txns[f"T{i + 1}{j}"].merchant  # noqa: E731
creds = lambda c: f"{c.email}, born {c.dob}"  # noqa: E731
S = []


def add(sid, category, turns, intent, frozen=(), disputed=(), handoff=False):
    S.append({"id": sid, "category": category, "turns": turns, "intent": intent,
              "expect": {"frozen": sorted(frozen), "disputed": sorted(disputed), "handoff": handoff}})


for i in range(4):
    c = C[i]
    add(f"lost-{i + 1}", "card lost, details up front", [f"My card was stolen. I'm {creds(c)}."], "card_lost", frozen=[f"K{i + 1}"])
for i in range(4, 8):
    c = C[i]
    add(f"lost-{i + 1}", "card lost, details next turn",
        ["I think I lost my card, can you freeze it?", f"Sure: {c.email}, {c.dob}"], "card_lost", frozen=[f"K{i + 1}"])
for i in range(3):
    c = C[i]
    add(f"dispute-{i + 1}", "dispute, in window", [f"I want to dispute the {merchant(i, 1)} charge. {creds(c)}"], "dispute", disputed=[f"T{i + 1}1"])
for i in range(3, 6):
    c = C[i]
    add(f"dispute-{i + 1}", "dispute, details next turn",
        [f"There's a {merchant(i, 2)} charge I didn't make.", f"{c.email} {c.dob}"], "dispute", disputed=[f"T{i + 1}2"])
for i in range(4):
    c = C[i]
    add(f"large-{i + 1}", "dispute over the limit", [f"I want to dispute the {merchant(i, 4)} charge, I never received anything. {creds(c)}"],
        "dispute", handoff=True)
for i in range(4, 8):
    c = C[i]
    add(f"old-{i + 1}", "dispute outside the window", [f"Please dispute the {merchant(i, 3)} charge from April. {creds(c)}"], "dispute")
for i in range(2):
    c = C[i]
    add(f"pending-{i + 1}", "dispute on a pending charge", [f"I want to dispute the {merchant(i, 5)} charge. {creds(c)}"], "dispute")
for i in range(2, 4):
    c = C[i]
    add(f"unclear-{i + 1}", "dispute, charge named later",
        [f"I need to dispute a charge. {creds(c)}", f"The {merchant(i, 0)} one."], "dispute", disputed=[f"T{i + 1}0"])
for i in range(4, 7):
    c = C[i]
    add(f"status-{i + 1}", "card status", [f"Is my card working? {creds(c)}"], "card_status")
for i, text in enumerate(["Can I talk to a real person please?", "I want to speak to someone, this is urgent."]):
    add(f"human-{i + 1}", "asks for a person", [text], "human", handoff=True)
for i in range(6, 8):
    add(f"human-late-{i + 1}", "asks for a person mid-request",
        ["My card was stolen.", "Forget it, just let me talk to a human."], "card_lost", handoff=True)
for i, text in enumerate(["What's the daily card spending limit?", "Are there fees for paying abroad?", "How long does a dispute take?"]):
    add(f"faq-{i + 1}", "general question", [text], "faq")
for i in range(2):
    c = C[i]
    add(f"wrong-dob-{i + 1}", "wrong date of birth", [f"My card was stolen. {c.email}, born 1900-01-01"], "card_lost")
for i in range(2, 4):
    c = C[i]
    add(f"email-only-{i + 1}", "email but no date of birth", [f"Please freeze my card right now, my email is {c.email}"], "card_lost")

out = pathlib.Path(__file__).with_name("scenarios.json")
out.write_text(json.dumps(S, indent=1) + "\n")
print(f"{len(S)} scenarios -> {out}")
