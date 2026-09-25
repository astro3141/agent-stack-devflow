"""devflow step: turn what the implementer wrote into a commit on the task's branch, and a pull request.

usage: deliver.py <project> <stage> <prepare_produced> <prepare_run> <implement_produced> <implement_run>

Reads the workspace work.py set up and what the model steps of this run produced. In order:

  1. what changed: the tree against the snapshot work.py took when it handed the tree over
  2. scope, by the canonical rules: a change to the project's rule paths (other than the task's own
     trace and research record) is refused — the candidate does not rewrite the rules it is judged
     by; the project's hook classifies the change, and a violation it names is refused
  3. the project's regeneration commands (policy `implement.regenerate`), in the tree; generated
     files the implementer edited by hand are whatever the generator makes of them
  4. one commit, authored devflow-implementer, on devflow/<task id> — created, or fast-forwarded;
     never forced. A branch someone else moved is left alone
  5. the pull request for that branch, opened if there is none
  6. the task's state: waiting for CI on the new head — or, when preparation could not settle
     something, waiting for the decision it names, with what it prepared pushed so a person sees it

Nothing is pushed when a step before 4 fails; the state then says what failed, and the next run's
implementation round (bounded by `implement.max_rounds`) is given it.

What a repeat does: "guarded" — the branch is asked before it is moved, and a pull request is looked
for before one is opened.
"""
REPEATABLE = "guarded"
import fnmatch, json, os, subprocess, sys, time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
import dfcore  # noqa: E402
import stackenv  # noqa: E402
import remote as remote_mod  # noqa: E402
from wstree import tree_map  # noqa: E402

WS_ROOT, PROXY = stackenv.load()
RUN = os.environ.get("CONDUCTOR_SELF_RUN_ID", "manual")
WS = os.path.join(WS_ROOT, RUN)
D = os.path.join(WS, "devflow")
REPO = os.path.join(WS, "repo")


def read_json(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return None


def matches(path, patterns):
    for p in patterns:
        p = p.rstrip("/")
        if fnmatch.fnmatch(path, p) or path == p or path.startswith(p + "/"):
            return True
    return False


def diff(snap, now):
    """{path: 'A'|'M'|'D'} between the snapshot and the tree now."""
    out = {}
    for p, v in now.items():
        if p not in snap:
            out[p] = "A"
        elif snap[p]["sha1"] != v["sha1"] or snap[p].get("x") != v.get("x"):
            out[p] = "M"
    for p in snap:
        if p not in now:
            out[p] = "D"
    return out


def regenerate(cmds, timeout):
    """Run the project's generators in the tree. `python`/`python3` are this runtime's interpreter
    (the one the steps run under); nothing is run through a shell."""
    ran = []
    for cmd in cmds:
        argv = [sys.executable if c in ("python", "python3") else c for c in cmd]
        try:
            p = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        except (OSError, subprocess.TimeoutExpired) as e:
            return ran, f"{' '.join(cmd)}: {type(e).__name__}: {e}"
        ran.append({"cmd": " ".join(cmd), "rc": p.returncode, "tail": (p.stdout + p.stderr)[-1500:]})
        if p.returncode != 0:
            return ran, f"{' '.join(cmd)} exited {p.returncode}: {(p.stdout + p.stderr)[-600:]}"
    return ran, ""


def main(argv):
    project, stage = argv[0], argv[1]
    prep_ok = len(argv) > 2 and argv[2] == "True"
    prep_run = argv[3] if len(argv) > 3 else ""
    impl_ok = len(argv) > 4 and argv[4] == "True"
    impl_run = argv[5] if len(argv) > 5 else ""
    res_ok = len(argv) > 6 and argv[6] == "True"
    res_run = argv[7] if len(argv) > 7 else ""
    sp = os.path.join(D, "state.json")
    st = json.load(open(sp, encoding="utf-8"))
    ev = json.load(open(os.path.join(D, "evaluation.json"), encoding="utf-8"))
    work = json.load(open(os.path.join(D, "work.json"), encoding="utf-8"))
    snap = json.load(open(os.path.join(D, "snapshot.json"), encoding="utf-8"))
    pol = ev.get("policy_doc") or {}
    impl = pol.get("implement") or {}
    task = ev["task"]
    reg = dfcore.registration(project)
    hist = st.setdefault("history", {})
    hist["impl_sessions"] = sorted(set(hist.get("impl_sessions") or []) | {r for r in (prep_run, impl_run, res_run) if r})
    report = {"stage": stage, "round": work.get("round"), "at": time.time()}

    def finish(ok, **kw):
        report.update(kw, ok=ok)
        hist["last_delivery"] = {k: report.get(k) for k in ("stage", "round", "ok", "failure", "head", "pr")}
        if not ok:
            st["blockers"] = list(st.get("blockers") or []) + [f"devflow implementation round {work.get('round')}: {kw.get('failure')}"]
            st.update(phase="implement", progress="waiting",
                      wait={"reason": "implementation", "need": "devflow's next implementation round, or a person",
                            "owner": "implementer", "resume_when": "a new head commit"})
            st["next"] = st["wait"]["need"]
        st["delivery"] = report
        json.dump(st, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        json.dump(report, open(os.path.join(D, "delivery.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(json.dumps({"status": "OK", "delivered": "yes" if ok and report.get("head") else "no",
                          "head": report.get("head") or "", "pr": str(report.get("pr") or ""),
                          "hold": report.get("hold") or "", "failure": report.get("failure") or "",
                          "error": ""}, ensure_ascii=False))
        return 0

    prep = read_json(os.path.join(D, "prepare.json")) if prep_ok else None
    done = read_json(os.path.join(D, "implement.json")) if impl_ok else None
    research = read_json(os.path.join(D, "research.json")) if res_ok else None
    report["prepare"], report["implement"], report["research"] = prep, done, research
    if stage == "research":
        return deliver_research(project, reg, pol, impl, task, ev, st, hist, work, snap, research, report, finish)
    if stage == "prepare" and prep is None:
        return finish(False, failure="preparation produced no prepare.json in this attempt")
    # the same reading as proceed.py: open questions hold the task, whatever `proceed` says
    hold = bool(prep) and (prep.get("proceed") is not True or bool(prep.get("decisions") or prep.get("gaps")))
    if stage in ("prepare", "implement") and not hold and done is None:
        return finish(False, failure="the implementation produced no implement.json in this attempt")

    trace_path = (pol.get("trace") or {}).get("path", ".devflow/tasks/{task}.yaml").format(task=task["id"])
    research_path = str(impl.get("research_path") or ".devflow/tasks/{task}.research.md").format(task=task["id"])
    own = {trace_path, research_path}
    changes = diff(snap, tree_map(REPO))
    if hold:
        changes = {p: c for p, c in changes.items() if p in own}
    rules = [reg["policy_path"]] + list(pol.get("rule_paths") or []) + list((pol.get("hook") or {}).get("files") or [])
    refused = sorted(p for p in changes if p not in own and matches(p, rules))
    if refused:
        return finish(False, failure=f"the implementation edited the project's rules, which a candidate may not: {refused[:8]}")
    gen = list(impl.get("generated") or [])
    authored = {p: c for p, c in changes.items() if not matches(p, gen)}
    if not authored:
        return finish(False, failure="the implementation changed nothing")

    # the project's own reading of this change, before anything leaves the workspace
    hook_root = os.path.join(D, "policy")
    try:
        cls = dfcore.run_hook(pol, hook_root, "classify", {
            "task": task, "changed_files": sorted(authored), "authors": [dfcore.IMPLEMENTER],
            "pr": None, "canonical_head": work["canonical"]})
    except dfcore.HookError as e:
        return finish(False, failure=f"the project's hook could not classify the change: {e}")
    if cls.get("violations"):
        return finish(False, failure=f"the change breaks the project's scope rules: {cls['violations'][:5]}")

    ran = []
    if not hold and impl.get("regenerate"):
        ran, err = regenerate(impl["regenerate"], int(impl.get("regenerate_timeout_s") or 900))
        report["regenerate"] = ran
        if err:
            return finish(False, failure=f"regeneration failed: {err}")
    after = diff(snap, tree_map(REPO))
    final = {p: c for p, c in after.items() if p in authored or matches(p, gen)}
    report["dropped"] = sorted(p for p in after if p not in final)      # what a generator left behind
    report["changes"] = final

    changes_blob = {}
    for p, c in final.items():
        if c == "D":
            changes_blob[p] = None
        else:
            full = os.path.join(REPO, p)
            changes_blob[p] = (open(full, "rb").read(), "100755" if os.access(full, os.X_OK) else "100644")
    summary = (done or {}).get("summary") or (prep or {}).get("summary") or ""
    title = f"{task['id']}: {task.get('goal', '')}"
    kind = "preparation (waiting for a decision)" if hold else ("rebase onto canonical" if stage == "rebase" else "implementation")
    message = (f"{title}\n\n{kind}, round {work.get('round')}.\n\n{summary}\n\n"
               f"Devflow-Task: {task['id']}\nDevflow-Run: {RUN}\n")
    author = {"name": dfcore.IMPLEMENTER, "email": dfcore.IMPLEMENTER_EMAIL,
              "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    branch = work["branch"]
    try:
        rem = dfcore.open_remote(reg, WS, PROXY)
        now = rem.branch_head_or_none(branch)
        expect = None if work.get("new_branch") else work["parent"]
        if now != expect:
            return finish(False, failure=f"branch {branch} is at {str(now)[:12]}, not at {str(expect)[:12]}: "
                                         "someone else moved it; devflow does not overwrite it")
        head = rem.push_commit(branch, work["parent"], changes_blob, message, author,
                               base=work["base"], extra_parents=work.get("extra_parents") or [])
        hist["pushed"] = list(hist.get("pushed") or []) + [head]
        report["head"] = head
        pr = rem.find_pull(branch)
        if pr is None:
            body = pr_body(task, ev, prep, done, hold)
            try:
                n = rem.create_pull(branch, reg["canonical_branch"], title, body)
            except remote_mod.RemoteError:
                again = rem.find_pull(branch)
                if again is None:
                    raise
                n = again["number"]
        else:
            n = pr["number"]
        hist["pr"] = n
        report["pr"] = n
    except remote_mod.RemoteError as e:
        report["tool"] = f"{type(e).__name__}: {e}"[:300]
        st.setdefault("execution", {"status": "OK", "errors": []})
        st["execution"] = {"status": "TOOL_FAILURE", "errors": list(st["execution"].get("errors") or []) + [report["tool"]]}
        return finish(False, failure=f"the remote refused the delivery: {report['tool']}")

    st["publish_status"] = False            # the verdict status stays on the head it was about
    st["blockers"] = []
    if hold:
        dec, gaps = list(prep.get("decisions") or []), list(prep.get("gaps") or [])
        hist["prepare_hold"] = {"task_digest": ev.get("task_digest"), "decisions": dec, "gaps": gaps, "head": head}
        report["hold"] = "decision" if dec else "contract_gap"
        st.update(phase="prepare", verdict="NEEDS_DECISION" if dec else "CONTRACT_GAP", progress="waiting",
                  blockers=[f"decision: {d}" for d in dec] + [f"contract gap: {g}" for g in gaps],
                  wait={"reason": "decision" if dec else "contract_gap",
                        "need": "answer preparation's questions in the task block (the task changes, and devflow prepares again)",
                        "owner": "decision owner", "resume_when": "the task block records the decision"})
    else:
        hist.pop("prepare_hold", None)
        st.update(phase="verify_review", verdict=None, progress="waiting",
                  wait={"reason": "verification", "need": f"CI on the new head {head[:12]} (PR #{n})",
                        "owner": "CI", "resume_when": "the required checks complete on that head"})
        st["notes"] = [f"devflow pushed {head[:12]} to {branch} ({kind}, round {work.get('round')})"] + \
                      ([f"generated files regenerated: {sorted(p for p in final if matches(p, gen))}"] if gen else [])
    st["next"] = st["wait"]["need"]
    return finish(True)


def deliver_research(project, reg, pol, impl, task, ev, st, hist, work, snap, research, report, finish):
    """The research lane: the record, and nothing else. What it may write is the mode's `writes`
    (the project's research directory) — anything outside is refused, and so is a change the
    project's hook calls out of scope. No generator runs: a record changes no generated artifact."""
    if research is None:
        return finish(False, failure="the research lane produced no research.json in this attempt")
    mode = (impl.get("modes") or {}).get(task.get("type")) or {}
    writes = list(mode.get("writes") or [])
    changes = diff(snap, tree_map(REPO))
    if not changes:
        return finish(False, failure="the research lane changed nothing")
    outside = sorted(p for p in changes if not matches(p, writes))
    if outside:
        return finish(False, failure=f"the research lane wrote outside {writes}: {outside[:8]}")
    try:
        cls = dfcore.run_hook(pol, os.path.join(D, "policy"), "classify", {
            "task": task, "changed_files": sorted(changes), "authors": [dfcore.IMPLEMENTER],
            "pr": None, "canonical_head": work["canonical"]})
    except dfcore.HookError as e:
        return finish(False, failure=f"the project's hook could not classify the change: {e}")
    if cls.get("violations"):
        return finish(False, failure=f"the change breaks the project's scope rules: {cls['violations'][:5]}")
    blobs = {}
    for p, c in changes.items():
        full = os.path.join(REPO, p)
        blobs[p] = None if c == "D" else (open(full, "rb").read(), "100644")
    report["changes"] = changes
    title = f"{task['id']}: {task.get('goal', '')}"
    message = (f"{title}\n\n{task['type']}, round {work.get('round')}.\n\n{research.get('summary') or ''}\n\n"
               f"Devflow-Task: {task['id']}\nDevflow-Run: {RUN}\n")
    author = {"name": dfcore.IMPLEMENTER, "email": dfcore.IMPLEMENTER_EMAIL,
              "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    branch = work["branch"]
    try:
        rem = dfcore.open_remote(reg, WS, PROXY)
        now = rem.branch_head_or_none(branch)
        expect = None if work.get("new_branch") else work["parent"]
        if now != expect:
            return finish(False, failure=f"branch {branch} is at {str(now)[:12]}, not at {str(expect)[:12]}: "
                                         "someone else moved it; devflow does not overwrite it")
        head = rem.push_commit(branch, work["parent"], blobs, message, author, base=work["base"])
        hist["pushed"] = list(hist.get("pushed") or []) + [head]
        report["head"] = head
        pr = rem.find_pull(branch)
        if pr is None:
            lines = [f"{dfcore.PR_MARK} task={task['id']} issue={task.get('issue')} -->",
                     f"Task: #{task.get('issue')} — {task.get('goal', '')}", "",
                     f"A {task['type']} record by devflow's research lane. It records facts with their sources; "
                     "it decides nothing about the criterion. Agents do not merge.", "",
                     research.get("summary") or ""]
            for k in ("decisions", "gaps", "topics"):
                if research.get(k):
                    lines += ["", f"**{k}**"] + [f"- {x}" for x in research[k]]
            try:
                n = rem.create_pull(branch, reg["canonical_branch"], title, "\n".join(lines))
            except remote_mod.RemoteError:
                again = rem.find_pull(branch)
                if again is None:
                    raise
                n = again["number"]
        else:
            n = pr["number"]
        hist["pr"] = n
        report["pr"] = n
    except remote_mod.RemoteError as e:
        report["tool"] = f"{type(e).__name__}: {e}"[:300]
        st["execution"] = {"status": "TOOL_FAILURE", "errors": list((st.get("execution") or {}).get("errors") or []) + [report["tool"]]}
        return finish(False, failure=f"the remote refused the delivery: {report['tool']}")
    st["publish_status"] = False
    st["blockers"] = []
    st.update(phase="verify_review", verdict=None, progress="waiting",
              wait={"reason": "verification", "need": f"CI on the new head {head[:12]} (PR #{n})",
                    "owner": "CI", "resume_when": "the required checks complete on that head"})
    st["notes"] = [f"devflow pushed {head[:12]} to {branch} ({task['type']} record, round {work.get('round')})"]
    st["next"] = st["wait"]["need"]
    return finish(True)


def pr_body(task, ev, prep, done, hold):
    lines = [f"{dfcore.PR_MARK} task={task['id']} issue={task.get('issue')} -->",
             f"Task: #{task.get('issue')} — {task.get('goal', '')}", "",
             "Opened by devflow's implementer. The verdict is on the issue (the devflow comment) and on "
             "the head commit (`devflow/verdict`); agents do not merge.", ""]
    if prep:
        lines += ["## Preparation", prep.get("summary") or "", ""]
        lines += [f"- acceptance: {a}" for a in prep.get("acceptance") or []]
        if hold:
            lines += ["", "**Waiting for a decision before implementation:**"]
            lines += [f"- {d}" for d in (prep.get("decisions") or []) + (prep.get("gaps") or [])]
    if done:
        lines += ["", "## Implementation", done.get("summary") or ""]
        lines += [f"- {c}" for c in done.get("changed") or []]
        if done.get("not_done"):
            lines += ["", "Not done:"] + [f"- {x}" for x in done["not_done"]]
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        print(json.dumps({"status": "TOOL_FAILURE", "delivered": "no", "head": "", "pr": "", "hold": "",
                          "failure": "", "error": f"{type(e).__name__}: {e}"[:300]}))
