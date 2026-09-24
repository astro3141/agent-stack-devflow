"""devflow step: put the task's state where the task lives — on GitHub.

usage: publish.py <project>

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
sys.path.insert(0, "/work/p281")
import dfcore  # noqa: E402
import remote as remote_mod  # noqa: E402

try:
    import settings
    WS_ROOT = settings.runtime()["paths"]["workspace_root"]
    PROXY = settings.egress_env()
except Exception:
    WS_ROOT, PROXY = os.environ.get("P281_WORKSPACE_ROOT", "/ws"), None
RUN = os.environ.get("CONDUCTOR_SELF_RUN_ID", "manual")
WS = os.path.join(WS_ROOT, RUN)
D = os.path.join(WS, "devflow")

STATUS = {"PASS": "success", "REQUEST_CHANGES": "failure", "CONTRACT_GAP": "failure", "NEEDS_DECISION": "failure"}


def main(argv):
    project = argv[0]
    sp = os.path.join(D, "state.json")
    if not os.path.isfile(sp):
        print(json.dumps({"status": "TOOL_FAILURE", "published": "no", "evidence_index": "", "error": "no state for this run"}))
        return 0
    st = json.load(open(sp, encoding="utf-8"))
    frame = st.get("frame") or {}
    pub_key = dfcore.sha256(dfcore.canon([frame.get("id"), st["phase"], st.get("verdict"), st.get("progress"),
                                          st.get("blockers"), (st.get("wait") or {}).get("reason")]))[:16]
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
        mine = [c for c in comments if dfcore.STATE_MARK in (c.get("body") or "")]
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
                again = [c for c in rem.comments(issue) if f'"publish":"{pub_key}"' in (c.get("body") or "")]
                if not again:
                    raise
                skipped.append("state comment had been added; the answer was lost")
        head = frame.get("head_sha")
        if head:
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
    print(json.dumps({"status": "TOOL_FAILURE" if err else "OK", "published": "no" if err else "yes",
                      "did": "; ".join(did), "skipped": "; ".join(skipped), "evidence_index": idx,
                      # the task's final state for this run, after integration had its say
                      "verdict": st.get("verdict") or "UNDECIDED", "phase": st["phase"],
                      "progress": st.get("progress") or "",
                      "next": f"{w.get('need', '')} (owner: {w.get('owner', '')})" if w else "",
                      "error": err}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        print(json.dumps({"status": "TOOL_FAILURE", "published": "no", "evidence_index": "",
                          "error": f"{type(e).__name__}: {e}"[:300]}))
