"""devflow step: after the verdict, whether devflow's own implementer acts on the task in this run.

usage: act.py

Reads <ws>/devflow/state.json (adjudicate) and evaluation.json (evaluate). Deterministic; no model;
changes nothing on the remote. Prints `do`:

  prepare    no implementation exists yet: preparation (criteria mapping, research, trace), then
             the implementation, on a new branch devflow/<task id>
  implement  the verdict sent the task back to implementation (REQUEST_CHANGES, or a failure on the
             head's checks) and the pull request is devflow's own: fix what the blockers name
  rebase     the change is not on the latest canonical head of a shared generated artifact: bring
             it there and regenerate — no model
  research   a task type the project routes to a lane of its own (`implement.modes`, e.g. research
             and survey): one model step as devflow-researcher writes a research record, first time
             and on rework alike
  none       anything else: a person, CI or a reviewer has the next move, or the project does not
             let devflow implement (policy `implement.by`), or the task says a person implements it

Where the rules come from: the verdict and its phase are adjudicate's, and this step only reads
them. The rework limit (`loop.max_rework`) and the no-progress limit are already in that verdict: a
task past them is NEEDS_DECISION, not REQUEST_CHANGES. `implement.max_rounds` bounds every
implementation round devflow starts for one task, whatever sent it back.

What a repeat does: "yes" — the same state gives the same answer.
"""
REPEATABLE = "yes"
import json, os, sys

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
import dfcore  # noqa: E402
import stackenv  # noqa: E402

WS_ROOT, _ = stackenv.load(need_proxy=False)
RUN = os.environ.get("CONDUCTOR_SELF_RUN_ID", "manual")
D = os.path.join(WS_ROOT, RUN, "devflow")


def decide(st, ev):
    """(do, why). Pure: the controls call it with states they build."""
    pol = ev.get("policy_doc") or {}
    impl = pol.get("implement") or {}
    task = ev.get("task") or {}
    if (impl.get("by") or "human") != "agent":
        return "none", "the project's policy leaves implementation to a person (implement.by)"
    if str(task.get("implement") or "agent") != "agent":
        return "none", "the task names a person as its implementer (implement: human)"
    if st.get("execution", {}).get("status") != "OK":
        return "none", "this run's machinery failed; nothing is acted on"
    if (ev.get("flow") or {}).get("name", "pull_request") != "pull_request":
        return "none", f"this change class is published through the {ev['flow']['name']} flow, by a person"
    w = st.get("wait") or {}
    hist = st.get("history") or {}
    modes = impl.get("modes") or {}
    pr = st.get("pr")
    # a person's grant of one more rework round (`/devflow approve rework <head>`) is one more
    # round here too — the grant would be empty if the round cap still refused it
    rounds = int(hist.get("implement_rounds") or 0)
    cap = int(impl.get("max_rounds") or 3) + int(ev.get("rework_granted") or 0)
    # a research record is written for the task as its block states it: when a person changes the
    # block (the question, the scope), devflow's open record is rewritten for it — once per change
    if (task.get("type") in modes and pr and pr.get("state") == "open" and not pr.get("merged")
            and pr.get("head_ref") == dfcore.task_branch(task)
            and (ev.get("implementers") or {}).get("devflow_only")
            and hist.get("research_task_digest") != ev.get("task_digest")):
        if rounds >= cap:
            return "none", f"the task changed, but devflow has started {rounds} round(s) for it (implement.max_rounds {cap})"
        return "research", "the task block changed since the record was written: rewrite it for the task as it is now"
    if st.get("phase") != "implement" or w.get("reason") != "implementation":
        return "none", "the next move is not an implementation"
    if rounds >= cap:
        return "none", f"devflow has started {rounds} implementation round(s) for this task (implement.max_rounds {cap})"
    if task.get("type") in modes:
        if pr and (pr.get("head_ref") != dfcore.task_branch(task)
                   or not (ev.get("implementers") or {}).get("devflow_only")):
            return "none", "the pull request holds commits devflow did not push: its author continues it"
        return "research", f"a {task['type']} task: the research lane"
    if pr and hist.get("manual_until_new_head") == pr.get("head_sha"):
        return "none", "an earlier round found what only a person can resolve on this head"
    if not pr:
        return "prepare", "no implementation exists yet"
    if ev.get("prepare_only"):
        if pr.get("head_ref") != dfcore.task_branch(task):
            return "none", "the pull request is not devflow's"
        return "prepare", "the task answers what preparation asked; prepare again, then implement"
    if pr.get("head_ref") != dfcore.task_branch(task) or not (ev.get("implementers") or {}).get("devflow_only"):
        return "none", "the pull request holds commits devflow did not push: its implementer continues it"
    if any("not on the latest canonical head" in b for b in st.get("blockers") or []) and not st.get("verdict"):
        return "rebase", "the change must be brought onto the canonical head and regenerated"
    return "implement", f"{st.get('verdict') or 'the head'}: fix what the blockers name"


def main():
    try:
        st = json.load(open(os.path.join(D, "state.json"), encoding="utf-8"))
        ev = json.load(open(os.path.join(D, "evaluation.json"), encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(json.dumps({"do": "none", "why": f"no state for this run: {type(e).__name__}", "error": str(e)[:200]}))
        return 0
    do, why = decide(st, ev)
    json.dump({"do": do, "why": why}, open(os.path.join(D, "act.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print(json.dumps({"do": do, "why": why, "error": ""}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(json.dumps({"do": "none", "why": "", "error": f"{type(e).__name__}: {e}"[:300]}))
