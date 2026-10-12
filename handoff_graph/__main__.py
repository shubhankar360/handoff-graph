"""Command line.

  python -m handoff_graph chat            talk to it (scripted model unless --claude)
  python -m handoff_graph eval            regenerate results/report.md
  python -m handoff_graph eval --claude   run the same scenarios with Claude (costs money; not committed)
"""
import argparse
import pathlib
import sys
import uuid

from .evaluate import CONFIGS, ROOT, report, run_all
from .graph import build, send
from .models import ClaudeModel, ScriptedModel
from .tracing import Tracer
from .world import seed


def main(argv=None):
    p = argparse.ArgumentParser(prog="handoff_graph")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat")
    c.add_argument("--claude", action="store_true")
    c.add_argument("--hasty", action="store_true", help="use the hasty scripted planner")
    e = sub.add_parser("eval")
    e.add_argument("--claude", action="store_true")
    e.add_argument("--out", default=str(ROOT / "results" / "report.md"))
    a = p.parse_args(argv)

    if a.cmd == "chat":
        world, tracer = seed(), Tracer()
        model = ClaudeModel() if a.claude else ScriptedModel("hasty" if a.hasty else "careful")
        app, thread = build(world, model, tracer=tracer), str(uuid.uuid4())
        print(f"model: {model.name}. Try: 'my card was stolen', then 'asha@example.com 1991-03-14'. Ctrl-D to quit.")
        for line in sys.stdin:
            if line.strip():
                out = send(app, thread, line.strip(), on_handoff=lambda p: (print(f"  [handoff to a person: {p['reason']}]"), {"agent": "you"})[1])
                print(">", out["reply"])
        print("\nper-node timing:", tracer.summary())
        return

    if a.claude:
        configs = [("Claude planner, full graph", None, True, True), ("Claude planner, no gate, no guard", None, False, False)]
        results = run_all(configs, model_factory=lambda _style: ClaudeModel())
    else:
        results = run_all(CONFIGS)
    text = report(results)
    if a.claude:
        print(text)
    else:
        pathlib.Path(a.out).write_text(text)
        print(text)


if __name__ == "__main__":
    main()
