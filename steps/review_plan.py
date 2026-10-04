"""devflow step: turn this run's model review assignments into a fan-out plan.

usage: review_plan.py <provider> <login> <model_route> <principal> <profile> <admission-evidence-dir>

evaluate.py decided which reviewer assignments a model may supply (and wrote each one's frozen
prompt); the router or the admission decided which providers may run now. This writes the plan
steps/tasks.py runs: one member per assignment, each a single model step, under the principal the
assignment names — which may write its own review_* files and nothing else (principals.yaml).

An assignment that names its model (reviews.reviewer_models, distinct_models) is bound to it by the
stack's roles.py, against the admission that let the round start: the login and model route are
the stack's answer, and a model the admission did not find eligible is left out of the plan with
the reason (its review stays missing, and the state says so). An assignment that names no model
runs on the provider the router picked.

What a repeat does: "yes" — the same evaluation and admission give the same plan.
"""
REPEATABLE = "yes"
import json, os, subprocess, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import dfcore  # noqa: E402
import step  # noqa: E402 — the stack's step helper: run id, workspace, the answer line

D = os.path.join(dfcore.workspace(), "devflow")
ROLES = "/work/stack/steps/roles.py"


def bind(profile, evidence_dir, named):
    """{label: roles.py's binding} for the named assignments — provider, login, route, principal,
    or a reason it could not be bound. The stack's answer (agent-stack roles.py, #51)."""
    if not named:
        return {}
    specs = [f"{a['reviewer']}={a['provider']}:{a.get('principal') or ''}".rstrip(":") for a in named]
    p = subprocess.run([sys.executable, ROLES, profile, evidence_dir] + specs,
                       capture_output=True, text=True, timeout=120)
    got = json.loads((p.stdout.strip().splitlines() or ["{}"])[-1])
    why = {}
    for part in (got.get("missing") or "").split(",") + (got.get("ineligible") or "").split(","):
        if part.strip():
            why[part.split(":", 1)[0]] = part.strip()
    return {a["reviewer"]: {k: got.get(f"{a['reviewer']}_{k}", "") for k in ("provider", "login", "route", "principal")}
            | {"why": why.get(a["reviewer"], "")} for a in named}


def main(argv):
    provider, login, route, principal, profile, evidence_dir = (argv + [""] * 6)[:6]
    ev = json.load(open(os.path.join(D, "evaluation.json"), encoding="utf-8"))
    wanted = [a for a in ev.get("assignments") or [] if a.get("by") == "model" and a.get("prompt")]
    bound = bind(profile, evidence_dir, [a for a in wanted if a.get("provider")])
    members, unbound = [], []
    for a in wanted:
        if a.get("provider"):
            b = bound.get(a["reviewer"]) or {}
            if not b.get("provider"):
                unbound.append(f"{a['reviewer']}: {b.get('why') or 'not bound'}")
                continue
            call = {"provider": b["provider"], "login": b["login"], "route": b["route"],
                    "principal": b["principal"] or a.get("principal") or principal}
        else:
            call = {"provider": provider, "login": login, "route": route, "principal": a.get("principal") or principal}
        member = {"label": a["reviewer"], "steps": [{"kind": "model", **call,
                                                     "prompt": a["prompt"], "expected": a["expected"]}]}
        # a member that did not produce (a failure or a time-out, never a denial) may be run again,
        # as many times as the project's policy says — the platform counts every attempt
        if int(a.get("retries") or 0) > 0:
            member.update(retries=int(a["retries"]), retry_when=["failed"])
        members.append(member)
    plan = os.path.join(D, "review_plan.json")
    json.dump({"members": members, "unbound": unbound}, open(plan, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    step.out(status="OK" if members else "EMPTY", plan=plan, members=len(members),
             context=ev["frame"]["id"], unbound="; ".join(unbound)[:300], error="")
    return 0


if __name__ == "__main__":
    sys.exit(step.main(lambda: main(sys.argv[1:]), status="FAILED", plan="", members=0, context="",
                       unbound="", error="", failure_key="error"))
