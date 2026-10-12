"""Per-node timing that works offline, plus Langfuse when it is configured.

Every node is wrapped so a run leaves a list of spans (node, duration, which
keys it changed). That is enough to answer "where did this turn spend its
time" without any service. If LANGFUSE_PUBLIC_KEY is set and the `langfuse`
package is installed, `langfuse_callbacks()` returns the LangChain callback
handler to pass as `config={"callbacks": [...]}`; it is optional and is not
exercised in CI.
"""
from __future__ import annotations

import json
import os
import time
from functools import wraps


class Tracer:
    def __init__(self):
        self.spans: list[dict] = []

    def wrap(self, name, fn):
        @wraps(fn)
        def node(state):
            t0 = time.perf_counter()
            out = fn(state)
            self.spans.append({"node": name, "ms": round((time.perf_counter() - t0) * 1000, 3),
                               "changed": sorted((out or {}).keys())})
            return out
        return node

    def summary(self) -> dict[str, dict]:
        by_node: dict[str, list[float]] = {}
        for s in self.spans:
            by_node.setdefault(s["node"], []).append(s["ms"])
        return {n: {"calls": len(v), "p50_ms": _pct(v, 50), "p95_ms": _pct(v, 95)} for n, v in by_node.items()}

    def dump(self, path: str) -> None:
        with open(path, "w") as f:
            for s in self.spans:
                f.write(json.dumps(s) + "\n")


def langfuse_callbacks() -> list:
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        return []
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        return []
    return [CallbackHandler()]


def _pct(values: list[float], p: int) -> float:
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))], 3)
