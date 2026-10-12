# handoff-graph

A LangGraph orchestration layer for card support: it routes each message to an intent, keeps context across turns, runs tools through a policy guard, and pauses for a person when a conversation needs one. It is graded on what it did to the accounts, not on how its replies sound.

I built it to answer a question that kept coming up while I worked on [agentcheck](https://github.com/shubhankar360/agentcheck): when an agent has to follow rules (verify the customer first, never auto-dispute a large charge), where should those rules live? In the prompt, in the shape of the graph, or in a separate check that runs before every tool call? So I wrote the same support flow three ways and measured each one.

## What happens in a turn

```
route ─► verify ─► plan ─► guard ─► act ──┐
            │        ▲                    │   loop until the planner says it's done
            │        └────────────────────┘
            │                  └─► handoff   pauses for a person, then files the ticket
            └──────────► respond ◄────┘
```

- **route** picks an intent (lost card, dispute, card status, question, wants a person) and pulls out an email and date of birth if the message has them. A message that only says "it's asha@example.com, 1991-03-14" keeps the request from the previous turn. That carry-over is what the LangGraph checkpointer is for.
- **verify** is a gate in the graph, not an instruction to the model. No account tool can be reached until identity checks out, and wrong details are dropped so the next attempt starts clean.
- **plan → guard → act** is a small loop. The planner proposes one tool call at a time. The guard checks it against the policy and either lets it run, turns it into a handoff (a dispute over $500), or stops it with an explanation (a charge older than 120 days). The loop is capped at six calls a turn.
- **handoff** uses LangGraph's `interrupt()`. The conversation pauses until a person accepts, and only then is the ticket filed. The order matters: on resume, LangGraph re-runs the node from the top, so filing the ticket first would file one on every resume. There's a test for that.

The world underneath (customers, cards, transactions) enforces physics but not policy. You can't freeze someone else's card, but you can dispute a $720 charge if nothing stops you. That's deliberate: if the backend refused everything, every agent would look safe.

## Results

40 scripted conversations of 14 kinds, each run under five configurations. Two scripted planners stand in for models with known behaviour: **careful** follows the written policy itself, **hasty** does whatever the customer asks as fast as it can. A conversation passes when the accounts end up exactly as they should and an independent audit of the executed actions finds no rule broken. The audit doesn't trust the guard; it replays the action log against the rules separately.

| configuration | passed | conversations breaking policy | violations by rule | guard blocks |
| --- | --- | --- | --- | --- |
| careful planner, full graph | 40/40 | 0 | none | 0 |
| careful planner, no gate, no guard | 40/40 | 0 | none | 0 |
| hasty planner, full graph | **40/40** | 0 | none | 8 |
| hasty planner, gate but no guard | 32/40 | 8 | dispute-limit 4, dispute-window 4 | 0 |
| hasty planner, no gate, no guard | **28/40** | 12 | dispute-limit 4, dispute-window 4, verify-first 8 | 0 |

Full table with routing and handoff precision/recall: [results/report.md](results/report.md). CI regenerates it on every push and fails if a single number moves.

What I take from it:

- **A careful planner hides the problem.** With a well-behaved model, all three designs score 40/40, so a demo can't tell them apart. The difference only shows up when the model gets it wrong, which is exactly when it matters.
- **Structure and a guard make the outcome independent of the planner's temperament.** The hasty planner inside the full graph scores the same as the careful one. The guard stopped eight actions: four over-limit disputes became handoffs and four out-of-window disputes became explanations.
- **Verification belongs in the graph.** Without the gate, the hasty planner froze cards for people who gave only an email, or the wrong date of birth, in four conversations. That's the `verify-first` column. No prompt wording fixes that reliably; a missing edge in the graph does.
- **Keyword routing has a blind spot I left in on purpose.** "How long does a dispute take?" routes to *dispute* instead of *question* (routing is 39/40 everywhere). The conversation still passes, because nothing happens without verification, but it's the case an LLM router should fix. That's the first thing I'll measure with Claude.

These planners are scripted, so the numbers say what the harness can detect, not how a particular LLM behaves. Running the same 40 conversations with Claude is one flag away (below). I haven't published Claude numbers yet.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m handoff_graph chat                 # talk to it; try "my card was stolen", then "asha@example.com 1991-03-14"
python -m handoff_graph chat --hasty         # same, with the planner that cuts corners
python -m handoff_graph eval                 # regenerates results/report.md
python -m pytest -q                          # 15 tests, no network, no key
```

With Claude as the router and planner (needs `ANTHROPIC_API_KEY`; costs money; prints rather than overwrites the report):

```bash
python -m handoff_graph chat --claude
python -m handoff_graph eval --claude
```

The Claude adapter uses structured outputs (a JSON schema for the route and for each proposed tool call) rather than forced tool choice, runs at low effort, and treats a refusal as a reason to hand off. Replies are built from what actually ran, so the bot can't say "I've frozen your card" when it hasn't. The adapter is tested against a stub client in CI. `HANDOFF_MODEL` picks the model; the default is `claude-opus-5-5`.

## Serve it

```bash
uvicorn app:app --reload
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"thread_id":"demo","message":"My card was stolen. ben@example.com, born 1987-11-02"}'
```

`POST /chat` returns either a reply or `handoff_pending`; `POST /handoff/resume` with an agent name lets a person take it. The Dockerfile is built and smoke-tested in CI, and `render.yaml` deploys it on Render's free tier (New → Blueprint → this repo). State is in memory, so it resets on restart.

## Tracing

Every node is wrapped with a timer, so `chat` prints per-node call counts and p50/p95 time at the end. If `LANGFUSE_PUBLIC_KEY` and friends are set and `langfuse` is installed, `handoff_graph.tracing.langfuse_callbacks()` returns the LangChain handler to pass as `config={"callbacks": ...}`. That path is optional and not exercised in CI.

## Layout

```
handoff_graph/world.py     deterministic backend: customers, cards, transactions, tools
handoff_graph/policy.py    the rules, used by the guard before and the audit after
handoff_graph/models.py    scripted planners (careful, hasty) and the Claude adapter
handoff_graph/graph.py     the LangGraph state machine
handoff_graph/evaluate.py  run, grade, report
data/build.py              generates the 40 scenarios
app.py                     FastAPI front end
```

## What it doesn't do yet

- The policy is five rules. Real support policy has hundreds, and some of them conflict. I'd want to see how the guard holds up when rules need judgement rather than a threshold.
- One customer, one thread, in-memory state. A real deployment needs a persistent checkpointer (LangGraph has a Postgres one) and authentication in front of `/chat`.
- No Claude results yet, and no comparison between models. That's next, starting with the routing case above.
