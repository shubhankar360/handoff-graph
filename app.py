"""HTTP front end: one graph, one in-memory world, conversations keyed by thread_id.

  POST /chat            {"thread_id": "...", "message": "..."}
  POST /handoff/resume  {"thread_id": "...", "agent": "dana"}   a person accepts a paused handoff
  GET  /health

Uses Claude when ANTHROPIC_API_KEY is set, the scripted planner otherwise.
State lives in memory, so it resets on restart; this is a demo, not a bank.
"""
import os

from fastapi import FastAPI
from langgraph.types import Command
from pydantic import BaseModel

from handoff_graph import ClaudeModel, ScriptedModel, build, seed

world = seed()
model = ClaudeModel() if os.environ.get("ANTHROPIC_API_KEY") else ScriptedModel("careful")
graph = build(world, model)
app = FastAPI(title="handoff-graph")


class ChatIn(BaseModel):
    thread_id: str
    message: str


class ResumeIn(BaseModel):
    thread_id: str
    agent: str


def _view(out: dict) -> dict:
    paused = bool(out.get("__interrupt__"))
    return {
        "reply": None if paused else out.get("reply"),
        "handoff_pending": out["__interrupt__"][0].value if paused else None,
        "intent": out.get("intent"),
        "verified": out.get("verified", False),
    }


@app.get("/health")
def health():
    return {"ok": True, "model": model.name}


@app.post("/chat")
def chat(body: ChatIn):
    cfg = {"configurable": {"thread_id": body.thread_id}}
    return _view(graph.invoke({"messages": [{"role": "user", "content": body.message}]}, cfg))


@app.post("/handoff/resume")
def resume(body: ResumeIn):
    cfg = {"configurable": {"thread_id": body.thread_id}}
    return _view(graph.invoke(Command(resume={"agent": body.agent}), cfg))
