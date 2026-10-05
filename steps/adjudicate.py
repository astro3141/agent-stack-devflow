"""devflow step: reach the task's state from this run's evidence.

usage: adjudicate.py [<receipt>] [<why no model review was started>]

Reads <ws>/devflow/evaluation.json (this run's, from evaluate.py) and, when model reviews ran in
this run, the fan-out receipt steps/tasks.py wrote. Writes <ws>/devflow/state.json.

A model review counts only when the receipt says *this attempt* produced it (produced, not
produced_stale), the file still has the hash the receipt recorded, and it names this frame's head
and policy. Who reviewed is taken from the receipt — the principal and the routed call — never
from what the model wrote about itself. What an earlier attempt left in the workspace is not
evidence of anything.

What a repeat does: "yes" — the same evaluation and receipt give the same state.
"""
REPEATABLE = "yes"
import hashlib, json, os, sys

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
import dfcore  # noqa: E402
import step  # noqa: E402 — the stack's step helper: run id, workspace, the answer line

RUN = step.run_id()
WS = dfcore.workspace()
D = os.path.join(WS, "devflow")


def sha_file(p):
    try:
        return hashlib.sha256(open(p, "rb").read()).hexdigest()
    except OSError:
        return ""


def model_reviews(ev, receipt_path):
    """The reviews this run's models produced, validated like any other review."""
    out, calls = [], 0
    if not receipt_path or not os.path.isfile(receipt_path):
        return out, calls
    rc = json.load(open(receipt_path, encoding="utf-8"))
    frame = ev["frame"]
    if rc.get("context") != frame["id"]:
        return [{"valid": False, "why": ["the receipt belongs to another frame"], "source": "receipt"}], 0
    chosen = ev.get("lens_defs") or {}          # exactly the lenses evaluate selected, common and project
    not_shown = {x["ref"].split(":", 1)[0] for x in ((ev.get("materials") or {}).get("not_shown") or [])}
    by_reviewer = {a["reviewer"]: a for a in ev.get("assignments") or [] if a.get("by") == "model"}
    for label, m in (rc.get("members") or {}).items():
        calls += int(m.get("attempts") or 1)
        res = m.get("result") or {}
        # a plan member is a chain (steps/task_chain.py): the routed call's own record is its step's
        model_steps = [x for x in res.get("steps") or [] if isinstance(x, dict) and x.get("run_id")]
        if model_steps:
            res = {**res, **model_steps[-1]}
        src = f"model:{label}:{res.get('run_id', '')}"
        if not m.get("produced") or res.get("produced_stale"):
            out.append({"valid": False, "why": ["this attempt did not produce the review"], "source": src})
            continue
        path = os.path.join(WS, m.get("artifact") or "")
        if not m.get("sha256") or sha_file(path) != m["sha256"]:
            out.append({"valid": False, "why": ["the review file is not the one the receipt recorded"], "source": src})
            continue
        try:
            doc = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            out.append({"valid": False, "why": [f"the review is not JSON: {type(e).__name__}"], "source": src})
            continue
        a = by_reviewer.get(label) or {}
        doc["lenses"] = [l for l in doc.get("lenses") or [] if l in (a.get("lenses") or [])]
        # identity is the receipt's: the principal the call ran as and the call itself
        doc["reviewer"] = {"kind": "model", "identity": f"devflow-model:{res.get('principal') or 'adapter'}:{res.get('run_id', '')}",
                           "principal": res.get("principal") or "", "session": res.get("run_id", ""),
                           "provider": res.get("provider", ""), "fresh_context": True}
        if a.get("schema"):
            # a query was given its materials in its prompt and had no tools: what was put in is
            # what was read — a fact of the call, not the model's own list of it
            doc["materials"] = [{"ref": k, "read": True} for k in a.get("inlined") or []]
        # material that was not shown cannot have been read, whatever the review says
        doc["materials"] = [dict(x, read=bool(x.get("read")) and x.get("ref") not in not_shown)
                            for x in doc.get("materials") or []]
        v = dfcore.validate_review(doc, frame, chosen, ev.get("implementers") or {})
        v["source"] = src
        v["doc"] = doc
        v["sha256"] = m["sha256"]
        out.append(v)
    return out, calls


def main(argv):
    receipt = argv[0] if argv and argv[0] else ""
    p = os.path.join(D, "evaluation.json")
    if not os.path.isfile(p):
        step.out(**{"status": "TOOL_FAILURE", "verdict": "", "phase": "", "progress": "", "integrate": "no",
                          "state": "", "error": "no evaluation for this run"})
        return 0
    ev = json.load(open(p, encoding="utf-8"))
    if receipt and not os.path.isabs(receipt):
        receipt = os.path.join(WS, receipt)
    mreviews, calls = model_reviews(ev, receipt) if ev.get("frame", {}).get("id") else ([], 0)
    reviews = dict(ev.get("reviews") or {})
    reviews["results"] = list(reviews.get("results") or []) + mreviews
    ctx = {"prepare": ev.get("prepare"), "gates": ev.get("gates"), "pr": ev.get("pr"),
           "checks": ev.get("checks"), "trace": ev.get("trace"), "reviews": reviews,
           "shared": ev.get("shared"), "finalize": ev.get("finalize"), "history": ev.get("history") or {},
           "tool": ev.get("tool"), "end_point": ev.get("end_point"),
           "violations": ev.get("violations"), "flow": ev.get("flow"), "prepare_only": ev.get("prepare_only"),
           "rework_granted": ev.get("rework_granted"), "reviews_budget_spent": ev.get("reviews_budget_spent"),
           "lane": ev.get("lane"),
           # automatic rework is devflow's own implementer's: a person's change is fixed by its person
           "auto_rework": str(((ev.get("policy_doc") or {}).get("implement") or {}).get("by") or "human") == "agent"
                          and str((ev.get("task") or {}).get("implement") or "agent") == "agent"}
    st = dfcore.adjudicate(ctx)
    if ev.get("notes"):
        # what evaluate noticed about the inputs (rules edited by the change, marker comments it
        # did not trust) is said on the issue too
        st["notes"] = list(ev["notes"]) + list(st.get("notes") or [])
    held = argv[1].strip() if len(argv) > 1 and argv[1] else ""
    if held and not calls:
        # the round was not started: the router's own words say why (a provider over its limit,
        # a login expired), so the person reading the issue knows what is being waited for
        st["notes"] = list(st.get("notes") or []) + [f"model reviews not started this run: {held}"[:400]]

    hist = dict(ev.get("history") or {})
    head = (ev.get("frame") or {}).get("head_sha")
    if calls:
        hist["model_calls"] = int(hist.get("model_calls") or 0) + calls
        hist["review_rounds"] = int(hist.get("review_rounds") or 0) + 1
        if ev.get("review_grant"):
            # the granted round is spent by running, whatever it concluded
            hist["review_grants_spent"] = list(hist.get("review_grants_spent") or []) + [str(head or "")[:12]]
    if st["verdict"] == "REQUEST_CHANGES" and (st.get("wait") or {}).get("reason") != "budget":
        # counted when the change goes back for rework; a head that only met the limit is not a
        # round (it is counted when a grant sends it back)
        if hist.get("last_rc_head") != head:
            hist["rework"] = int(hist.get("rework") or 0) + 1
            hist["last_rc_head"] = head
        hist["last_blocking"] = ctx.get("_blocking_now") or []
        hist["no_progress"] = ctx.get("_no_progress") or 0
    elif st["verdict"] == "PASS" and st["phase"] == "integrate" and not (ev.get("pr") or {}).get("merged"):
        hist["passed_head"] = head
        hist["no_progress"] = 0
    accepted = [a for a in (ev.get("prior_accepted") or [])]
    for v in mreviews:
        if v.get("valid"):
            accepted.append({"frame_id": ev["frame"]["id"], "run": RUN, "sha256": v["sha256"], "review": v["doc"]})
    state = {"schema": "devflow/state@1", "run": RUN, **st, "frame": ev.get("frame"), "history": hist,
             "checks": ev.get("checks"), "classification": ev.get("classification"),
             "accepted_reviews": accepted[-8:], "ops": ev.get("ops_done") or {},
             "issue": ev.get("issue"), "project": ev.get("project"), "pr": ev.get("pr"),
             "shared": ev.get("shared"), "state_comment": ev.get("state_comment"),
             "review_results": [{k: r.get(k) for k in ("source", "valid", "why", "complete", "unverified", "independent")}
                                for r in reviews["results"]]}
    json.dump(state, open(os.path.join(D, "state.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    step.out(**{"status": st["execution"]["status"], "verdict": st["verdict"] or "UNDECIDED",
                      "phase": st["phase"], "progress": st["progress"],
                      "integrate": "yes" if (st["verdict"] == "PASS" and st["phase"] == "integrate" and not st["wait"]) else "no",
                      "reason": "; ".join(st["blockers"][:3])[:300] or (st.get("next") or ""),
                      "next": st.get("next") or "", "state": os.path.join(D, "state.json"),
                      "model_calls": calls,
                      # the commit whose checks the task waits on: after a merge, the merge commit
                      "watch": str((ev.get("finalize") or {}).get("merge_commit") or head or ""),
                      "error": "; ".join(st["execution"]["errors"])[:300]})
    return 0


if __name__ == "__main__":
    # a crash is still an answer: every declared key, status TOOL_FAILURE and the reason (step.main)
    sys.exit(step.main(lambda: main(sys.argv[1:]), verdict="UNDECIDED", phase="", progress="", integrate="no", reason="", next="", state="", model_calls=0, watch="", error="", failure_key="error"))
