"""devflow step: put the task's state where the task lives — on GitHub.

usage: publish.py <project> [<why devflow's implementer could not start>]

Two effects, both on the remote, both guarded:

  * one state comment on the task's issue, edited in place: readable text, and a machine copy the
    next run reads back (counters, accepted reviews, completed operations — history, never
    authority: every verdict is recomputed from evidence)
  * a commit status `devflow/verdict` on the head that was judged — so a verdict is attached to
    exactly the commit it is about, and a new commit has none until it is judged again

No labels: a project may use labels as control signals (the infra scanner's orchestrator does),
and a workflow that writes them could start or stop someone else's automation.

Guard: the comment carries this publication's key (frame, phase, verdict, progress, blockers). The
remote is asked first; a comment that already carries the key — because an earlier attempt's
answer was lost after the write — is not written again. A status that already says the same is
not set again.

What a repeat does: "guarded".
"""
REPEATABLE = "guarded"
import json, os, sys

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
import dfcore  # noqa: E402
import step  # noqa: E402 — the stack's step helper: run id, workspace, the answer line
import remote as remote_mod  # noqa: E402

RUN = step.run_id()
WS = dfcore.workspace()
PROXY = dfcore.egress_env()
D = os.path.join(WS, "devflow")

STATUS = {"PASS": "success", "REQUEST_CHANGES": "failure", "CONTRACT_GAP": "failure", "NEEDS_DECISION": "failure"}


def main(argv):
    project = argv[0]
    sp = os.path.join(D, "state.json")
    if not os.path.isfile(sp):
        step.out(**{"status": "TOOL_FAILURE", "published": "no", "evidence_index": "", "error": "no state for this run"})
        return 0
    st = json.load(open(sp, encoding="utf-8"))
    held = argv[1].strip() if len(argv) > 1 and argv[1] else ""
    if held:
        # act sent the task to devflow's implementer and the router held it (a login expired, a
        # provider over its limit): the wait is the environment's, in the router's words — not
        # "open a pull request", which is what adjudicate says when nobody is implementing
        # (measured on infra-scanner-io#26: the expired login was nowhere on the issue)
        st["wait"] = {"reason": "environment", "need": f"devflow's implementer could not start: {held}"[:400],
                      "owner": "operator", "resume_when": "a pass after the implementer's provider is eligible again"}
        st["next"] = st["wait"]["need"]
        json.dump(st, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    frame = st.get("frame") or {}
    # what the issue has to carry: the verdict, and also what was spent and taken — a pass that only
    # added a review on the same head (same verdict, same blockers) still counted its model calls
    # and its round, and a key without them called that "already written" (measured on #24: the
    # judge's single call was left out of the budget)
    hist = st.get("history") or {}
    pub_key = dfcore.sha256(dfcore.canon([frame.get("id"), st["phase"], st.get("verdict"), st.get("progress"),
                                          st.get("blockers"), (st.get("wait") or {}).get("reason"),
                                          {k: v for k, v in hist.items() if k != "last_blocking"},
                                          [a.get("sha256") for a in st.get("accepted_reviews") or []],
                                          (st.get("wait") or {}).get("need") if held else None]))[:16]
    st.setdefault("ops", {})
    did, skipped, err = [], [], ""
    try:
        reg = dfcore.registration(project)
        rem = dfcore.open_remote(reg, WS, PROXY)
        issue = st.get("issue")
        # the key travels in the machine copy, so the remote can answer "was this written?"
        st["ops"]["publish"] = pub_key
        body = dfcore.render_state(st)
        comments = rem.comments(issue)
        writer = rem.writer()
        # only devflow's own marker comment is edited, or counts as "already written": a copy
        # someone else posted is neither devflow's state nor proof that devflow published
        mine = [c for c in dfcore.own_comments(comments, writer) if dfcore.STATE_MARK in (c.get("body") or "")]
        if any(f'"publish":"{pub_key}"' in (c.get("body") or "") for c in mine):
            skipped.append("state comment already says this")
        elif mine:
            rem.edit_comment(mine[-1]["id"], body)
            did.append(f"state comment {mine[-1]['id']} updated")
        else:
            try:
                cid = rem.add_comment(issue, body)
                did.append(f"state comment {cid} added")
            except remote_mod.RemoteError:
                again = [c for c in dfcore.own_comments(rem.comments(issue), writer)
                         if f'"publish":"{pub_key}"' in (c.get("body") or "")]
                if not again:
                    raise
                skipped.append("state comment had been added; the answer was lost")
        head = frame.get("head_sha")
        # a run that pushed a new head leaves the judged head's status as it was; a local candidate
        # is not on the remote, so it has no status there (its state is the issue comment)
        if head and st.get("publish_status", True) and not (st.get("pr") or {}).get("candidate"):
            state = STATUS.get(st.get("verdict") or "", "pending")
            desc = f"{st.get('verdict') or 'undecided'} · {st['phase']} · {st.get('progress')}"
            if st["execution"]["status"] != "OK":
                desc += " · tool failure"
            have = [s for s in rem.statuses(head) if s["context"] == "devflow/verdict"]
            if have and have[-1]["state"] == state and have[-1]["description"] == desc[:140]:
                skipped.append("status already set")
            else:
                try:
                    rem.set_status(head, "devflow/verdict", state, desc)
                    did.append(f"status {state} on {head[:12]}")
                except remote_mod.RemoteError:
                    have = [s for s in rem.statuses(head) if s["context"] == "devflow/verdict"]
                    if not (have and have[-1]["state"] == state):
                        raise
                    skipped.append("status had been set; the answer was lost")
    except (dfcore.ConfigError, remote_mod.RemoteError) as e:
        err = f"{type(e).__name__}: {e}"[:300]

    # the run's evidence index, in the shape the recorder stores (docs/packages.md §4: `items`)
    items = [{"claim": f"check {n}: {r['result']}", "kind": "check", "head": frame.get("head_sha"),
              "detail": r.get("why") or r.get("exception") or ""}
             for n, r in sorted(((st.get("checks") or {}).get("results") or {}).items())]
    items += [{"claim": f"review {r.get('source')}: {'counted' if r.get('valid') else 'not counted'}",
               "kind": "review", "head": frame.get("head_sha"),
               "detail": json.dumps(r.get("complete") if r.get("valid") else r.get("why"), ensure_ascii=False)}
              for r in st.get("review_results") or []]
    items += [{"claim": b, "kind": "blocker", "head": frame.get("head_sha")} for b in st.get("blockers") or []]
    idx = os.path.join(D, "evidence_index.json")
    json.dump({"items": items, "decision": st.get("verdict") or "UNDECIDED", "frame": frame.get("id"),
               "phase": st["phase"], "progress": st.get("progress")},
              open(idx, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(st, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    w = st.get("wait") or {}
    step.out(**{"status": "TOOL_FAILURE" if err else "OK", "published": "no" if err else "yes",
                      "did": "; ".join(did), "skipped": "; ".join(skipped), "evidence_index": idx,
                      # the task's final state for this run, after integration had its say
                      "verdict": st.get("verdict") or "UNDECIDED", "phase": st["phase"],
                      "progress": st.get("progress") or "",
                      "next": f"{w.get('need', '')} (owner: {w.get('owner', '')})" if w else "",
                      "wait": w.get("reason", "") if w else "", "owner": w.get("owner", "") if w else "",
                      "error": err})
    return 0


if __name__ == "__main__":
    # a crash is still an answer: every declared key, status TOOL_FAILURE and the reason (step.main)
    sys.exit(step.main(lambda: main(sys.argv[1:]), published="no", evidence_index="", wait="", owner="", error="", failure_key="error"))
