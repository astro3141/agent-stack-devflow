"""devflow step: take a PASS toward the canonical branch, one shared artifact at a time.

usage: integrate.py <project>

Runs after adjudicate. Two cases:

  * PASS on the current head (phase integrate): serialize on each shared artifact the change
    touches (a lock ref on the remote, created atomically), then check — right before acting —
    that the head is still the head that was judged and, for a shared artifact, that the canonical
    branch has not moved under it. Then either merge (policy `integration.merge_by: agent`) or
    record that the change is ready for the person who merges (`merge_by: human`).
  * done (merged, post-merge checks passed): release the locks this change held.

Every effect is guarded by asking the remote first, because a resumed or re-run step cannot trust
its own workspace to know what happened (docs/packages.md, "a marker for the resume, and a question
to the remote for everything else"):

  lock    creating an existing ref is refused; the ref's value says whether it is ours
  merge   the pull request says whether it is merged, and at which head; GitHub itself refuses a
          merge whose head is not the sha given
  release a ref that is not ours is never deleted

What a repeat does: "guarded" — it recognises an effect that already happened and does not
repeat it.
"""
REPEATABLE = "guarded"
import json, os, sys, time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
import dfcore  # noqa: E402
import stackenv  # noqa: E402
import remote as remote_mod  # noqa: E402

WS_ROOT, PROXY = stackenv.load()
RUN = os.environ.get("CONDUCTOR_SELF_RUN_ID", "manual")
WS = os.path.join(WS_ROOT, RUN)
D = os.path.join(WS, "devflow")


def lock_ref(lock_id):
    return f"refs/devflow/lock/{lock_id}"


def main(argv):
    project = argv[0]
    sp = os.path.join(D, "state.json")
    st = json.load(open(sp, encoding="utf-8"))
    ev = json.load(open(os.path.join(D, "evaluation.json"), encoding="utf-8"))
    act = {"at": time.time(), "run": RUN, "did": [], "skipped": []}

    def finish(status="OK", **kw):
        st.setdefault("integration", {}).update(act, **kw)
        json.dump(st, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(json.dumps({"status": status, "merged": "yes" if kw.get("merged") else "no",
                          "ready": "yes" if kw.get("ready") else "no", "phase": st["phase"],
                          "progress": st["progress"], "detail": "; ".join(act["did"] + act["skipped"])[:300],
                          "error": kw.get("error", "")}, ensure_ascii=False))
        return 0

    try:
        reg = dfcore.registration(project)
        rem = dfcore.open_remote(reg, WS, PROXY)
        pol = dfcore.load_policy(rem, reg, ev["frame"]["policy"]["ref"])
    except (dfcore.ConfigError, remote_mod.RemoteError) as e:
        st["execution"]["status"] = "TOOL_FAILURE"
        st["execution"]["errors"].append(f"integrate: {e}")
        return finish("TOOL_FAILURE", error=str(e)[:300])
    integ = pol.get("integration") or {}
    frame = st["frame"]
    head = frame["head_sha"]
    locks = [l for l in ((st.get("shared") or {}).get("lock") or [])] if (st.get("shared") or {}).get("required") else []
    if integ.get("serialize_all"):
        locks = sorted(set(locks) | {integ["serialize_all"]})

    try:
        # ------------------------------------------------ release, after a completed integration
        if st["phase"] == "done":
            # the merged run computed no shared-artifact state: look at every lock the policy has
            every = sorted({a.get("lock", a["id"]) for a in pol.get("shared_artifacts") or []} | set(locks))
            for l in every:
                held = rem.ref(lock_ref(l))
                if held and held == (st["history"].get("passed_head") or head):
                    rem.delete_ref(lock_ref(l))
                    act["did"].append(f"released {l}")
                elif held:
                    act["skipped"].append(f"{l} is held by {held[:12]}, not by this change")
            return finish()

        # ------------------------------------------------ the head is still the one judged?
        # asked before any lock is taken: a head nobody judged must not hold the shared artifact
        pr = rem.pull(frame["pr"])
        if pr["head_sha"] != head:
            st.update(phase="verify_review", verdict=None, progress="waiting")
            st["wait"] = {"reason": "verification", "need": "the head moved after the verdict; evaluate again",
                          "owner": "devflow", "resume_when": "the next run"}
            return finish()

        # ------------------------------------------------ serialize
        for l in locks:
            held = rem.ref(lock_ref(l))
            if held == head:
                act["skipped"].append(f"{l} already held by this head")
                continue
            if held and held != head and rem.contains(held, head):
                # an earlier head of this same change took it; the change still holds it
                rem.delete_ref(lock_ref(l))
                act["did"].append(f"{l}: moved from this change's earlier head {held[:12]}")
                held = None
            if held:
                st["progress"] = "waiting"
                st["wait"] = {"reason": "integration_order", "need": f"{l} is being integrated by {held[:12]}",
                              "owner": "the change holding it", "resume_when": f"{lock_ref(l)} is released"}
                st["blockers"].append(f"shared artifact {l} is held by another change ({held[:12]})")
                return finish()
            try:
                rem.create_ref(lock_ref(l), head)
                act["did"].append(f"took {l}")
            except remote_mod.Conflict:
                if rem.ref(lock_ref(l)) != head:          # someone else won the race
                    st["progress"] = "waiting"
                    st["wait"] = {"reason": "integration_order", "need": f"{l} was taken first by another change",
                                  "owner": "the change holding it", "resume_when": f"{lock_ref(l)} is released"}
                    return finish()
                act["skipped"].append(f"{l}: our earlier request had already taken it")
            except remote_mod.RemoteError:
                if rem.ref(lock_ref(l)) == head:          # it happened; the answer was lost
                    act["skipped"].append(f"{l}: taken, answer lost")
                else:
                    raise

        # ------------------------------------------------ re-check right before acting
        pr = rem.pull(frame["pr"])
        canonical = rem.branch_head(reg["canonical_branch"])
        if pr["head_sha"] != head:
            st.update(phase="verify_review", verdict=None, progress="waiting")
            st["wait"] = {"reason": "verification", "need": "the head moved after the verdict; evaluate again",
                          "owner": "devflow", "resume_when": "the next run"}
            return finish()
        if locks and not rem.contains(canonical, head):
            st.update(phase="implement", verdict=None, progress="waiting")
            st["wait"] = {"reason": "implementation", "need": "the canonical branch moved: update, regenerate, re-verify",
                          "owner": "implementer", "resume_when": "the head contains the canonical head"}
            st["blockers"].append(f"canonical moved to {canonical[:12]} before the merge")
            return finish()

        # ------------------------------------------------ what GitHub itself enforces here, asked
        # Serialization above is devflow's lock because it works on every plan; a merge queue or
        # required checks are used only where they exist, and the state says which is the case.
        gh = {"merge_queue": rem.merge_queue(reg["canonical_branch"]),
              "protection": rem.protection(reg["canonical_branch"])}
        act["github"] = gh
        if not (gh["protection"] or {}).get("available", True):
            act["skipped"].append("no branch protection or rulesets on this plan: the checks are evidence, not enforced")

        # ------------------------------------------------ merge, or hand it to the person who does
        if pr.get("merged"):
            act["skipped"].append("already merged")
            return finish(merged=True)
        if integ.get("merge_by", "human") != "agent":
            st["progress"] = "waiting"
            st["wait"] = {"reason": "merge", "need": f"merge pull request #{pr['number']} at exactly {head[:12]}",
                          "owner": integ.get("merge_owner") or "the merge owner the policy names",
                          "resume_when": "the pull request is merged; the next run verifies the merge commit"}
            act["did"].append("ready for the merge owner")
            return finish(ready=True)
        key = dfcore.op_key(frame, "merge")
        try:
            rem.merge(pr["number"], head, integ.get("method", "squash"))
            act["did"].append(f"merged #{pr['number']} at {head[:12]}")
        except remote_mod.RemoteError:
            again = rem.pull(pr["number"])
            if not (again.get("merged") and again.get("head_sha") == head):
                raise
            act["skipped"].append("merge had happened; the answer was lost")
        st.setdefault("ops", {})[key] = "merge"
        st["progress"] = "waiting"
        st["wait"] = {"reason": "verification", "need": "post-merge verification on the merge commit",
                      "owner": "CI", "resume_when": "the post-merge checks complete"}
        return finish(merged=True)
    except remote_mod.RemoteError as e:
        st["execution"]["status"] = "TOOL_FAILURE"
        st["execution"]["errors"].append(f"integrate: {e}")
        st["progress"] = "waiting"
        st["wait"] = {"reason": "environment", "need": "the remote failed during integration; run again (it is guarded)",
                      "owner": "operator", "resume_when": "the remote answers"}
        return finish("TOOL_FAILURE", error=str(e)[:300])


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        print(json.dumps({"status": "TOOL_FAILURE", "merged": "no", "ready": "no", "phase": "", "progress": "",
                          "detail": "", "error": f"{type(e).__name__}: {e}"[:300]}))
