"""devflow step: run passes over one task, one after another, until the next move is not devflow's.

usage: drive.py <project> <issue> <profile> <impl_profile> <review_mode> <max_passes> <poll_s> <max_wait_s>

Each pass is an ordinary `devflow` run (its own run id, its own record, its own published state);
this step only decides whether to start the next one, from what the last one published:

  continue   devflow's implementer pushed a head (CI will judge it)       → wait for CI, pass again
             CI is still running on the head                                → wait, pass again
             reviews are missing and a model may still write them          → pass again
  stop       a person has the next move: a decision, a contract gap, a merge, a gate, a hold
             the task is done
             the machinery failed (the pass ended TOOL_FAILURE)
             a pass changed nothing that the last one had not already said (no progress)
             `max_passes` passes, or `max_wait_s` waiting for CI

Resuming is starting this again: every pass reads the task from GitHub.

What a repeat does: "no" — it starts runs, each a new one.
"""
REPEATABLE = "no"
import json, os, re, subprocess, sys, time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
RUNNER = ["/opt/venv/bin/python", "/work/stack/run_workflow.py"]
PARENT = os.environ.get("CONDUCTOR_SELF_RUN_ID", "manual")


def settle(project, head, poll_s, budget_s, sleep=time.sleep):
    """Wait until the checks on `head` have settled: some exist, none is still running, and the set
    did not change between two looks (a workflow that starts late is still seen). Asked of GitHub
    directly — a pass is not spent on watching CI. Returns (settled?, seconds waited)."""
    import dfcore, stackenv, remote as remote_mod
    ws_root, proxy = stackenv.load()
    rem = dfcore.open_remote(dfcore.registration(project), os.path.join(ws_root, PARENT), proxy)
    waited, last = 0, None
    while waited <= budget_s:
        try:
            runs = rem.check_runs(head)
        except remote_mod.RemoteError:
            runs = None
        if runs:
            now = sorted((r["name"], r["status"]) for r in runs)
            if all(r["status"] == "completed" for r in runs) and now == last:
                return True, waited
            last = now
        sleep(poll_s)
        waited += poll_s
    return False, waited


def emit(**kw):
    print(json.dumps(kw, ensure_ascii=False))


def one_pass(run_id, project, issue, profile, impl_profile, review_mode):
    args = RUNNER + ["start", run_id, "devflow", profile, f"project={project}", f"issue={issue}",
                     f"review_mode={review_mode}", f"impl_profile={impl_profile}"]
    subprocess.run(args, capture_output=True, text=True, timeout=3 * 3600)
    shown = subprocess.run(RUNNER + ["show", run_id], capture_output=True, text=True, timeout=60)
    try:
        v = json.loads(shown.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": f"no answer from `show {run_id}`: {shown.stderr[-200:]}"}
    out = v.get("output") or {}
    return {"run": run_id, "verdict": out.get("verdict"), "phase": out.get("phase"),
            "progress": out.get("progress"), "wait": out.get("wait"), "owner": out.get("owner"),
            "next": out.get("next"), "acted": out.get("acted"), "delivered": out.get("delivered"),
            "head": out.get("head"), "need_models": out.get("need_models"),
            "state": v.get("state"), "ok": bool(v.get("completed_ok")),
            "reason": v.get("terminated_reason") or v.get("reason") or ""}


def decide(p, prev, waited, max_wait):
    """(continue?, sleep_s, why). Pure: the controls call it."""
    if p.get("error"):
        return False, 0, p["error"]
    if not p.get("ok"):
        return False, 0, f"the pass did not complete: {p.get('reason') or p.get('state')}"
    if p.get("phase") == "done":
        return False, 0, "the task is done"
    delivered = str(p.get("delivered") or "").startswith("yes")
    w, owner = p.get("wait") or "", p.get("owner") or ""
    if delivered:
        return True, -1, "devflow pushed a new head; CI judges it next"
    if w == "verification" and owner == "CI":
        if waited >= max_wait:
            return False, 0, f"CI has not finished after {int(waited)} s"
        return True, -1, "CI is running on the head"
    # Reviews a model may still supply, and the pass ran none: the router held (a quota observation
    # that failed transiently held two passes in a row — measured: "Claude OAuth usage endpoint is
    # rate limited", gone minutes later). That is waiting on the environment, not no-progress:
    # sleep, then pass again, bounded by max_wait like CI.
    if w == "review" and p.get("need_models") == "yes":
        if waited >= max_wait:
            return False, 0, f"a model may supply the missing reviews, but none could run for {int(waited)} s"
        return True, -2, "reviews are missing and a model may supply them; waiting for an eligible provider"

    def key(x):
        x = x or {}
        return (x.get("verdict"), x.get("phase"), x.get("wait"), x.get("head"), x.get("next"))
    if prev and key(p) == key(prev):
        return False, 0, "this pass reached the same state as the last one: nothing more happens without someone"
    if w == "review" and owner == "reviewer":
        return True, 0, "reviews are missing; a model may supply them"
    if w == "implementation" and not str(p.get("acted") or "none").startswith("none"):
        return True, 0, "devflow's implementer acted and may act again"
    return False, 0, f"the next move is {owner or 'not devflow'}'s: {p.get('next') or w}"


def main(argv):
    project, issue, profile, impl_profile, review_mode = argv[:5]
    max_passes, poll_s, max_wait = int(argv[5]), int(argv[6]), int(argv[7])
    passes, prev, why = [], None, ""
    held = 0                                  # how long reviews have waited for an eligible provider
    for n in range(1, max_passes + 1):
        run_id = re.sub(r"[^a-z0-9-]", "-", f"{PARENT[:24]}-p{n}".lower())
        p = one_pass(run_id, project, issue, profile, impl_profile, review_mode)
        passes.append(p)
        go, ci, why = decide(p, prev, held, max_wait)
        if not go:
            break
        if ci == -1:                          # CI on a head: wait for it here, then pass again
            m = re.search(r"\b([0-9a-f]{40})\b", str(p.get("delivered") or ""))
            head = m.group(1) if m else p.get("head")
            ok, waited = settle(project, head, poll_s, max_wait) if head else (False, 0)
            p["ci_waited_s"] = waited
            if not ok:
                why = f"the checks on {str(head)[:12]} did not settle within {max_wait} s"
                break
            held = 0
            prev = None                       # a settled CI is progress, whatever the state text
        elif ci == -2:                        # no provider could run the reviews: sleep, pass again
            time.sleep(poll_s)
            held += poll_s
            prev = p
        else:
            held = 0
            prev = p
    else:
        why = f"{max_passes} passes"
    last = passes[-1] if passes else {}
    json.dump({"passes": passes, "stopped": why}, open(os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"devflow-auto-{PARENT}.json"), "w"), ensure_ascii=False, indent=1)
    emit(status="OK" if last.get("ok") else "TOOL_FAILURE", passes=len(passes), stopped=why,
         verdict=last.get("verdict") or "", phase=last.get("phase") or "", next=last.get("next") or "",
         last_run=last.get("run") or "", runs=" ".join(x.get("run", "") for x in passes))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        emit(status="TOOL_FAILURE", passes=0, stopped=f"{type(e).__name__}: {e}"[:300], verdict="", phase="",
             next="", last_run="", runs="")
