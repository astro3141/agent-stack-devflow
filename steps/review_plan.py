"""devflow step: turn this run's model review assignments into a fan-out plan.

usage: review_plan.py <provider> <login> <model_route> <principal>

evaluate.py decided which reviewer assignments a model may supply (and wrote each one's frozen
prompt); the router decided which provider may run now. This writes the plan steps/tasks.py runs:
one member per assignment, each a single model step, all under the reviewer principal the policy
names — which may write its own review_* files and nothing else (principals.yaml).

No retry is asked for: a review that did not come back is a missing review, and the state says so.

What a repeat does: "yes" — the same evaluation gives the same plan.
"""
REPEATABLE = "yes"
import json, os, sys

sys.path.insert(0, "/work/p281")
try:
    import settings
    WS_ROOT = settings.runtime()["paths"]["workspace_root"]
except Exception:
    WS_ROOT = os.environ.get("AGENTSTACK_WORKSPACE_ROOT", "/ws")
RUN = os.environ.get("CONDUCTOR_SELF_RUN_ID", "manual")
D = os.path.join(WS_ROOT, RUN, "devflow")


def main(argv):
    provider, login, route, principal = (argv + ["", "", "", ""])[:4]
    ev = json.load(open(os.path.join(D, "evaluation.json"), encoding="utf-8"))
    members = []
    for a in ev.get("assignments") or []:
        if a.get("by") != "model" or not a.get("prompt"):
            continue
        members.append({"label": a["reviewer"], "steps": [{
            "kind": "model", "provider": provider, "login": login, "route": route,
            "prompt": a["prompt"], "expected": a["expected"], "principal": principal}]})
    plan = os.path.join(D, "review_plan.json")
    json.dump({"members": members}, open(plan, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({"status": "OK" if members else "EMPTY", "plan": plan, "members": len(members),
                      "context": ev["frame"]["id"]}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        print(json.dumps({"status": "FAILED", "plan": "", "members": 0, "context": "",
                          "error": f"{type(e).__name__}: {e}"[:300]}))
