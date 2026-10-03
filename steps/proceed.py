"""devflow step: whether preparation settled enough for the implementation to start.

usage: proceed.py <prepare_produced>

Preparation's own answer (`proceed` in devflow/prepare.json), read only when the model step says
this attempt produced the file. No file, or a file that is not JSON, is not a yes: deliver then
reports that preparation produced nothing, and nothing is implemented on a guess.

What a repeat does: "yes".
"""
REPEATABLE = "yes"
import json, os, sys

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
import dfcore  # noqa: E402
import step  # noqa: E402 — the stack's step helper: run id, workspace, the answer line

D = os.path.join(dfcore.workspace(), "devflow")


def main(argv):
    if not argv or argv[0] != "True":
        step.out(**{"proceed": "no", "why": "preparation did not produce its file in this attempt"})
        return 0
    try:
        doc = json.load(open(os.path.join(D, "prepare.json"), encoding="utf-8"))
    except (OSError, ValueError) as e:
        step.out(**{"proceed": "no", "why": f"prepare.json is not readable: {type(e).__name__}"})
        return 0
    open_items = len(doc.get("decisions") or []) + len(doc.get("gaps") or []) + len(doc.get("research") or [])
    ok = doc.get("proceed") is True and not open_items
    step.out(**{"proceed": "yes" if ok else "no",
                      "why": "preparation settled the task" if ok else
                      f"preparation leaves {open_items} question(s) open (decisions, gaps or research)"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
