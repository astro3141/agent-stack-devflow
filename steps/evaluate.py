"""devflow step: fix the frame of a task and gather every piece of evidence the verdict needs.

usage: evaluate.py <project> <issue> [<fixture>] [<review_mode>]

  project      a registration under config/devflow/projects/<project>.yaml
  issue        the task's issue number (the task is the issue's ```devflow block)
  fixture      fixture remote scenario name (only for a project whose remote is `fixture`)
  review_mode  model | external | none  — who may supply missing reviews in this run

It asks the remote, never a file an earlier attempt left: the pull request's head, the checks on
exactly that commit, the reviews written against it, the approvals given for it, the canonical
branch's head and the policy on it. It writes <ws>/devflow/evaluation.json and, when reviews are
missing and models may supply them, one frozen prompt per reviewer assignment.

What a repeat does: "yes" — the same remote state gives the same evaluation; it writes only this
run's workspace.
"""
REPEATABLE = "yes"
import json, os, re, shutil, sys, time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
import dfcore  # noqa: E402
import step  # noqa: E402 — the stack's step helper: run id, workspace, the answer line
import remote as remote_mod  # noqa: E402
import refs  # noqa: E402

RUN = step.run_id()
WS = dfcore.workspace()
PROXY = dfcore.egress_env()
D = os.path.join(WS, "devflow")
HANDOFF = os.environ.get("DEVFLOW_HANDOFF", "/work/handoff")
CACHE = dfcore.state_dir("tree-cache")   # commit trees, kept across runs (requires.state)
SAFE = re.compile(r"[A-Za-z0-9._-]{1,80}")




def stage_fixture(name):
    """Point this run at its fixture remote.

    A remote in the hand-in directory behaves like a real one: every run reads and changes the same
    file, so what one run did (a published state, a lock) is there for the next — which is what
    resuming a task across runs needs. A package fixture is a template: each run gets its own copy,
    once (a resumed run keeps what its first half changed).
    """
    dest = os.path.join(D, "remote.json")
    if not SAFE.fullmatch(name or ""):
        raise dfcore.ConfigError(f"fixture name {name!r} is not a plain name")
    shared = os.path.join(HANDOFF, f"{name}.json")
    if os.path.isfile(shared):
        if os.path.lexists(dest):
            os.remove(dest)
        os.symlink(shared, dest)
        return dest
    if os.path.isfile(dest):
        return dest
    src = os.path.join(PKG, "fixtures", "remotes", f"{name}.json")
    if os.path.isfile(src):
        shutil.copyfile(src, dest)
        return dest
    raise dfcore.ConfigError(f"no fixture remote {name!r}", hint="the hand-in directory, or packages/devflow/fixtures/remotes/")


def implementers_of(task, pr, changed, hist=None):
    """Everyone who implemented the change. devflow's own commits are the implementer principal's,
    whoever holds the credential that pushed them: the pull request's author is then only the
    account the push went through — unless a commit devflow did not push is in the change too, in
    which case the author counts as well. A commit devflow did not record pushing is never assumed
    to be devflow's, whatever its author field says."""
    hist = hist or {}
    pushed = set(hist.get("pushed") or [])
    commits = set(changed.get("commits") or [])
    ours = commits & pushed
    ids = set()
    for a in changed.get("authors") or []:
        ids.add(dfcore.IMPLEMENTER if (a in dfcore.IMPLEMENTER_AUTHORS and ours) else a)
    ours_only = bool(commits) and commits <= pushed
    if pr and pr.get("author") and not ours_only:
        ids.add(pr["author"])
    impl = task.get("implementer") or {}
    ids |= set(impl.get("identities") or [])
    principals = list(impl.get("principals") or [])
    sessions = list(impl.get("sessions") or [])
    if ours:
        ids.add(dfcore.IMPLEMENTER)
        principals += [dfcore.IMPLEMENTER, dfcore.RESEARCHER]
        sessions += list(hist.get("impl_sessions") or [])
    return {"identities": sorted(i for i in ids if i), "principals": sorted(set(principals)),
            "sessions": sorted(set(sessions)), "devflow_only": ours_only}


def find_pr(rem, task, hist):
    """The task's pull request: the one the task block names; else the one devflow opened for it
    (recorded in the published state, or found by the task's branch)."""
    if task.get("pr"):
        return rem.pull(task["pr"])
    if hist.get("pr"):
        return rem.pull(hist["pr"])
    return rem.find_pull(dfcore.task_branch(task))


ANSWERS = re.compile(r"^\s*(answers|unanswered)\s*:\s*(.+)$", re.M | re.I)
QID = re.compile(r"Q-[\w.-]*\w")


def research_answers(rem, pol, canonical_head, ev):
    """The question IDs merged research records answer, and those they closed without an answer
    ({id: record}): each record's `answers:` / `unanswered:` lines, on the canonical head only — a
    record still in review answers nothing yet."""
    rdir = ""
    for m in ((pol.get("implement") or {}).get("modes") or {}).values():
        for w in m.get("writes") or []:
            rdir = rdir or w.rstrip("/*")
    answered, closed = set(), {}
    if not rdir:
        return answered, closed
    try:
        for path in rem.list_dir(canonical_head, rdir):
            if not path.endswith(".md"):
                continue
            for kind, ids in ANSWERS.findall(rem.file_at(canonical_head, path) or ""):
                for q in QID.findall(ids):
                    if kind.lower() == "answers":
                        answered.add(q)
                    else:
                        closed.setdefault(q, path)
    except remote_mod.RemoteError as e:
        ev["tool"].append(f"RemoteError: {e}")
    return answered, {q: p for q, p in closed.items() if q not in answered}


def approvals(comments, pol, implementers):
    """Human decisions given on the issue, each bound to the exact thing decided:

      /devflow approve <gate> <task digest | head sha>
      /devflow except <check> <head sha> <reason and alternative evidence>

    Counted only from someone the policy names as an approver, who is not an implementer of the
    change. An exception is for one check on one head: a new commit needs a new one.
    """
    who_may = set(pol.get("approvers") or [])
    got, exc = [], []
    for c in comments:
        ok = c.get("user") in who_may and c.get("user") not in set(implementers["identities"])
        body = c.get("body") or ""
        for m in re.finditer(r"^/devflow approve ([a-z0-9_-]+) (\S+)\s*$", body, re.M):
            got.append({"gate": m.group(1), "value": m.group(2), "by": c.get("user"), "counted": ok})
        for m in re.finditer(r"^/devflow except (\S+) (\S{7,40}) (.+)$", body, re.M):
            exc.append({"check": m.group(1), "head": m.group(2), "reason": m.group(3).strip(),
                        "by": c.get("user"), "counted": ok})
    return got, exc


def main(argv):
    project, issue_no = argv[0], argv[1]
    fixture = argv[2] if len(argv) > 2 else ""
    review_mode = (argv[3] if len(argv) > 3 and argv[3] else "model")
    os.makedirs(D, exist_ok=True)
    ev = {"schema": "devflow/evaluation@1", "run": RUN, "at": time.time(), "project": project,
          "issue": issue_no, "review_mode": review_mode, "tool": [], "prepare": {"gaps": [], "decisions": [], "problems": [], "research": []}}

    def done(**extra):
        ev.update(extra)
        json.dump(ev, open(os.path.join(D, "evaluation.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        need = [a for a in ev.get("assignments") or [] if a.get("by") == "model"]
        step.out(reason="", status="TOOL_FAILURE" if ev["tool"] else "OK", evaluation=os.path.join(D, "evaluation.json"),
             lane=ev.get("lane") or "change",
             need_models="yes" if need and not ev["tool"] else "no", assignments=len(need),
             # the model families this round needs all of (a lens that must be completed by
             # several): the workflow asks whether every one is usable before starting any
             models=",".join(sorted({a["provider"] for a in need if a.get("provider")})),
             merged="yes" if (ev.get("pr") or {}).get("merged") else "no",
             frame=(ev.get("frame") or {}).get("id", ""), head=str((ev.get("frame") or {}).get("head_sha") or ""),
             error="; ".join(ev["tool"])[:300])
        return 0

    try:
        reg = dfcore.registration(project)
        if reg["remote"] == "fixture":
            stage_fixture(fixture or reg.get("fixture") or "")
        rem = dfcore.open_remote(reg, WS, PROXY)
        ev["registration"] = {k: reg[k] for k in ("name", "repository", "canonical_branch", "remote", "unconfirmed")}
        canonical_head = rem.branch_head(reg["canonical_branch"])
        issue = rem.issue(issue_no)
        comments = rem.comments(issue_no)
        # who devflow writes as: only its own marker comment carries its counters, and its own
        # comments are never a person's approval (the App login acts as the person who connected it)
        writer = rem.writer()
    except (dfcore.ConfigError, remote_mod.RemoteError) as e:
        ev["tool"].append(f"{type(e).__name__}: {e}" + (f" ({e.hint})" if getattr(e, "hint", "") else ""))
        return done()
    ev["writer"] = writer
    mine = {c["id"] for c in dfcore.own_comments(comments, writer)}
    ignored = [c["id"] for c in comments if c["id"] not in mine and dfcore.STATE_MARK in (c.get("body") or "")]
    if ignored:
        ev.setdefault("notes", []).append(f"marker comment(s) not written by devflow ignored: {ignored[:5]}")

    prior = dfcore.read_state(comments, writer)
    ev["history"] = dict(prior.get("history") or {})
    ev["ops_done"] = dict(prior.get("ops") or {})
    ev["prior"] = {k: prior.get(k) for k in ("phase", "verdict", "frame")} if prior else {}
    ev["state_comment"] = prior.get("_comment_id")

    task, why = dfcore.parse_task(issue)
    if task is None:
        ev["prepare"]["problems"].append(why)
        return done(frame={"id": "", "head_sha": None})
    ev["task"] = task

    try:
        pol = dfcore.load_policy(rem, reg, canonical_head)
        hook_root = os.path.join(D, "policy")
        hook_digests = dfcore.materialize_hook(rem, pol, canonical_head, hook_root)
        base_lenses, lenses = dfcore.load_lenses(pol, hook_root)
        pr = find_pr(rem, task, ev["history"])
        changed = rem.changed_files(pr["base_sha"], pr["head_sha"]) if pr else {"files": [], "authors": []}
        crit = dfcore.criteria_blobs(rem, pol, canonical_head)
    except (dfcore.ConfigError, remote_mod.RemoteError) as e:
        ev["tool"].append(f"{type(e).__name__}: {e}" + (f" ({e.hint})" if getattr(e, "hint", "") else ""))
        return done()

    wf = dfcore.workflow_identity()
    frame = dfcore.frame_of(reg, pol, wf, pr, canonical_head, crit, hook_digests, task)
    ev.update(frame=frame, pr=pr, changed=changed["files"], policy={"ref": pol["_ref"], "digest": pol["_digest"]},
              policy_doc={k: v for k, v in pol.items() if not k.startswith("_")},
              canonical_head=canonical_head, branch=dfcore.task_branch(task))
    impl = implementers_of(task, pr, changed, ev["history"])
    ev["implementers"] = impl
    # a task type the project routes to a lane of its own is reviewed by the research reviewer
    ev["lane"] = "research" if task.get("type") in ((pol.get("implement") or {}).get("modes") or {}) else "change"

    # Holds a person placed on the task (the project's own control labels) come before anything.
    for lab in issue.get("labels") or []:
        for pat, meaning in (pol.get("hold_labels") or {}).items():
            if re.fullmatch(pat.replace("*", ".*"), lab):
                if meaning == "NEEDS_DECISION":
                    ev["prepare"]["decisions"].append(f"label {lab!r} is on the issue: a person has to act first")
                else:
                    ev["prepare"]["problems"].append(f"held by label {lab!r} (released only by a person)")

    # Edits to the rules are a proposal, not the rules: this run judges with the canonical copy.
    rule_paths = [reg["policy_path"]] + list((pol.get("hook") or {}).get("files") or []) + list(pol.get("rule_paths") or [])
    touched_rules = sorted(f for f in changed["files"] if f in rule_paths or remote_mod.glob_any(f, pol.get("rule_paths") or []))

    try:
        cls = dfcore.run_hook(pol, hook_root, "classify", {
            "task": task, "changed_files": changed["files"], "authors": changed.get("authors") or [],
            "pr": {k: (pr or {}).get(k) for k in ("number", "author", "base_ref", "head_ref")} if pr else None,
            "canonical_head": canonical_head})
    except dfcore.HookError as e:
        ev["tool"].append(f"HookError: {e}")
        return done()
    cls.setdefault("impacts", {})
    if touched_rules:
        cls["impacts"]["policy_change"] = True
        ev.setdefault("notes", []).append(f"the change edits the rules {touched_rules}; judged by the canonical rules at {canonical_head[:12]}")
    problems = dfcore.validate_classification(pol, cls)
    ev["prepare"]["problems"] += problems + list(cls.get("problems") or [])
    ev["prepare"]["gaps"] += list(cls.get("gaps") or [])
    ev["prepare"]["decisions"] += list(cls.get("decisions") or [])
    ev["classification"] = cls

    # Human gates the policy attaches to this change class, bound to the exact thing approved.
    gates = (pol.get("human_gates") or {}).get(cls.get("change_class"), [])
    given, excepts = approvals([c for c in comments if c["id"] not in mine], pol, impl)
    ev["approvals"] = given + [dict(e, gate="except") for e in excepts]
    # one more automatic rework round, granted by a person for one head — the project's limit is
    # a default, and passing it is a decision (`/devflow approve rework <head sha>`); a grant for
    # an earlier head is spent, and each head's grant counts once
    grants = {a["value"][:12] for a in given if a["gate"] == "rework" and a["counted"]}
    ev["rework_granted"] = len(grants)
    # one more round of model review for one head, past the budget and the round limit
    # (`/devflow approve reviews <head sha>`) — the limits are defaults, and spending past them is a
    # person's decision; a grant is spent by the round it paid for (adjudicate records it). Each
    # comment is one round: a head whose reviews had to be taken again (the workflow or the policy
    # changed, so the frame did) gets another round by another comment, never by the old one.
    head12 = str((pr or {}).get("head_sha") or "")[:12]
    granted = sum(1 for a in given if a["gate"] == "reviews" and a["counted"] and a["value"][:12] == head12)
    ev["review_grant"] = bool(head12) and granted > (ev["history"].get("review_grants_spent") or []).count(head12)
    ev["end_point"] = str(task.get("end_point") or (pol.get("defaults") or {}).get("end_point") or "integration")
    loop = pol.get("loop") or {}
    ev["history"]["max_no_progress"] = int(loop.get("max_no_progress") or 3)
    if loop.get("max_rework") is not None:
        ev["history"]["max_rework"] = int(loop["max_rework"])
    # what a gate approves is the task's definition, not the links filled in as it progresses
    task_digest = dfcore.sha256(dfcore.canon({k: v for k, v in task.items()
                                              if k not in ("issue", "pr", "transaction_commit")}))[:12]
    ev["task_digest"] = task_digest
    # What devflow's own preparation could not settle (a mapping it could not confirm, a gap in the
    # contract) holds the task until a person records the answer in the task block — which changes
    # the task digest the hold is bound to.
    hold = ev["history"].get("prepare_hold") or {}
    # a head that holds only that preparation is not an implementation, whatever its checks say
    ev["prepare_only"] = bool(hold) and bool(pr) and hold.get("head") == pr.get("head_sha")
    if hold and hold.get("task_digest") == task_digest:
        ev["prepare"]["decisions"] += [f"preparation: {d}" for d in hold.get("decisions") or []]
        ev["prepare"]["gaps"] += [f"preparation: {g}" for g in hold.get("gaps") or []]
        # the research the item waits for: a question a merged record answers no longer holds it;
        # one a record closed without an answer becomes a person's decision (the item still needs
        # it); the rest keep waiting for the research lane
        asked = list(hold.get("research") or [])
        if asked:
            answered, closed = research_answers(rem, pol, canonical_head, ev)
            ev["research_answered"] = sorted(q["id"] for q in asked if q["id"] in answered)
            for q in asked:
                if q["id"] in answered:
                    continue
                if q["id"] in closed:
                    ev["prepare"]["decisions"].append(
                        f"research closed without an answer to {q['id']} ({closed[q['id']]}): {q.get('question')} — "
                        "decide the supported scope, another source, or a new research task")
                else:
                    ev["prepare"]["research"].append(q)
    gate_state = []
    for g in gates:
        want = task_digest if g.get("binds") == "task" else (pr or {}).get("head_sha")
        hit = [a for a in given if a["gate"] == g["id"] and a["counted"] and want and a["value"] in (want, str(want)[:12])]
        gate_state.append({"id": g["id"], "phase": g.get("phase", "prepare"), "binds": g.get("binds"),
                           "value": want, "approved": bool(hit), "by": [a["by"] for a in hit]})
    ev["gates"] = gate_state
    ev["violations"] = list(cls.get("violations") or [])

    # How this change class reaches the canonical branch. `pull_request` is the default; a project
    # may name another flow for a class — the infra scanner's Foundation is published by a person
    # as a commit pair, never as a pull request — and then says what to wait for and how the
    # published result is evidenced (the hook's `transaction_evidence`).
    flow_name = (pol.get("flows") or {}).get(cls.get("change_class"), "pull_request")
    flow = (pol.get("flow_defs") or {}).get(flow_name) or {}
    ev["flow"] = {"name": flow_name, **{k: flow.get(k) for k in ("need", "owner", "resume_when")}}
    if flow_name != "pull_request":
        commit = str(task.get("transaction_commit") or "")
        published = False
        if commit:
            try:
                published = rem.contains(commit, canonical_head)
            except remote_mod.RemoteError as e:
                ev["tool"].append(f"RemoteError: {e}")
        if not published:
            return done(assignments=[])
        try:
            rec_path = str(flow.get("record_path") or "").format(issue=task.get("issue"), commit=commit, task=task["id"])
            te = dfcore.run_hook(pol, hook_root, "transaction_evidence", {
                "task": task, "commit": commit, "canonical_head": canonical_head, "record_path": rec_path,
                "record_text": rem.file_at(canonical_head, rec_path) if rec_path else None})
            post = list((pol.get("post_merge") or {}).get("checks") or [])
            runs = rem.check_runs(canonical_head)
        except (remote_mod.RemoteError, dfcore.HookError) as e:
            ev["tool"].append(f"{type(e).__name__}: {e}")
            return done(assignments=[])
        ev["transaction"] = te
        ev["pr"] = {"number": None, "state": "closed", "merged": True, "head_sha": commit,
                    "base_sha": None, "merge_commit_sha": canonical_head, "flow": flow_name}
        ev["finalize"] = {"post": dfcore.evaluate_checks(post, {}, runs, canonical_head),
                          "delivery": None, "merge_commit": canonical_head,
                          "passed_head": commit if te.get("valid") else "",
                          "why_no_pass": "; ".join(te.get("problems") or []) or None}
        return done(assignments=[])

    if pr and pr.get("merged"):
        # Finalization: the checks that apply after integration, on the commit that landed.
        post = list((pol.get("post_merge") or {}).get("checks") or [])
        try:
            runs = rem.check_runs(pr["merge_commit_sha"]) if pr.get("merge_commit_sha") else []
        except remote_mod.RemoteError as e:
            ev["tool"].append(f"RemoteError: {e}")
            runs = []
        delivery = list((pol.get("delivery") or {}).get("checks") or [])
        ev["finalize"] = {"post": dfcore.evaluate_checks(post, {}, runs, pr.get("merge_commit_sha")),
                          "delivery": dfcore.evaluate_checks(delivery, {}, runs, pr.get("merge_commit_sha"))
                          if ev["end_point"] == "delivery" else None,
                          "merge_commit": pr.get("merge_commit_sha"),
                          "passed_head": ev["history"].get("passed_head") or ""}
        return done(assignments=[])

    if not pr:
        return done(assignments=[])

    # Checks required for this change, on exactly its head.
    chk = pol.get("checks") or {}
    required = list(chk.get("always") or [])
    for k, v in (chk.get("by_change_class") or {}).items():
        if cls.get("change_class") == k:
            required += v
    for k, v in (chk.get("by_impact") or {}).items():
        if (cls.get("impacts") or {}).get(k):
            required += v
    required += list(cls.get("required_checks") or [])
    na = dict(cls.get("not_applicable") or {})
    # a check the hook declares not applicable is shown as such, with its reason — not left out
    required = list(dict.fromkeys(required + list(na)))
    try:
        runs = rem.check_runs(pr["head_sha"])
    except remote_mod.RemoteError as e:
        ev["tool"].append(f"RemoteError: {e}")
        runs = []
    exceptions = {e["check"]: f"{e['reason']} (by {e['by']})" for e in excepts
                  if e["counted"] and pr["head_sha"].startswith(e["head"])}
    ev["checks"] = dfcore.evaluate_checks(required, na, runs, pr["head_sha"], exceptions)
    ev["checks"]["required"] = required

    # Trace, for a change whose meaning is affected: the hook validates it; the engine does not
    # read its content as a claim of conformance — a filled-in form is not a checked one.
    ev["trace"] = None
    if cls.get("trace_required"):
        tpath = (pol.get("trace") or {}).get("path", ".devflow/tasks/{task}.yaml").format(task=task["id"])
        try:
            text = rem.file_at(pr["head_sha"], tpath)
            ev["trace"] = dfcore.run_hook(pol, hook_root, "validate_trace", {
                "task": task, "trace_path": tpath, "trace_text": text, "criteria": crit,
                "change_class": cls.get("change_class")})
            ev["trace"]["path"] = tpath
        except (remote_mod.RemoteError, dfcore.HookError) as e:
            ev["tool"].append(f"{type(e).__name__}: {e}")
        if ev["trace"]:
            ev["prepare"]["gaps"] += list(ev["trace"].get("gaps") or [])
            ev["prepare"]["decisions"] += list(ev["trace"].get("decisions") or [])

    # Shared generated artifacts: on the latest canonical head, and the operations as expected.
    ev["shared"] = {"required": False}
    affected = [a for a in (pol.get("shared_artifacts") or []) if a["id"] in (cls.get("shared_artifacts") or [])]
    if affected:
        try:
            rebased = rem.contains(canonical_head, pr["head_sha"])
            detail, match = [], True
            for a in affected:
                base_text = rem.file_at(canonical_head, a["path"]) or ""
                head_text = rem.file_at(pr["head_sha"], a["path"]) or ""
                tpath = (pol.get("trace") or {}).get("path", ".devflow/tasks/{task}.yaml").format(task=task["id"])
                expected_doc = rem.file_at(pr["head_sha"], tpath)
                got = dfcore.run_hook(pol, hook_root, "artifact_ops", {
                    "artifact": a, "base_text": base_text, "head_text": head_text, "task_doc": expected_doc})
                detail.append({"artifact": a["id"], **{k: got.get(k) for k in ("match", "actual", "expected", "diff")}})
                match = match and bool(got.get("match"))
            ev["shared"] = {"required": True, "rebased": rebased, "ops_match": match if rebased else None,
                            "artifacts": [a["id"] for a in affected], "lock": [a.get("lock", a["id"]) for a in affected],
                            "detail": detail}
        except (remote_mod.RemoteError, dfcore.HookError) as e:
            ev["tool"].append(f"{type(e).__name__}: {e}")
            ev["shared"] = {"required": True, "rebased": False, "ops_match": None}

    # Reviews: which lenses, which already hold for this frame, which a model may still supply.
    chosen, why_chosen = dfcore.select_lenses(base_lenses, lenses, cls)
    assignments = dfcore.assign_reviewers(chosen, pol, cls)
    rv = pol.get("reviews") or {}
    need_models = dfcore.distinct_models_needed(pol, task.get("type"))
    # the models that review this task type, when the project names them (reviews.reviewer_models:
    # e.g. codex for a record claude wrote); several are needed only when distinct_models says so
    named = list(((rv.get("reviewer_models") or {}).get(task.get("type")) or []))
    if named or need_models > 1:
        # a lens is complete only when that many different models completed it: one assignment per
        # model, each carrying the lenses (its own reviewer label, provider and principal). On the
        # research lane every model reviews as the research reviewer, in its profile with the
        # author's hosts: brokered, every vendor runs as a role (agent-stack §55).
        models = (named or list(rv.get("models") or ["claude", "codex"]))[:max(need_models, 1)]
        # the reviewer's label names the review file (review_<label>.json), and every reviewer
        # principal's write rule allows exactly the review_r prefix — a label that drops the r
        # renames the file out of its author's own rights (measured: a finished review, written
        # against nine fetched primary pages, DENIED at the write)
        assignments = [{"reviewer": f"r{i}-{m}", "lenses": sorted(chosen), "independent": False,
                        "provider": m,
                        "principal": dfcore.RESEARCH_REVIEWER if ev.get("lane") == "research" else ""}
                       for i, m in enumerate(models, 1)]
    indep_lenses = sorted({l for a in assignments if a["independent"] for l in a["lenses"]})
    independent_needed = bool(indep_lenses)
    results = []
    try:
        for r in rem.reviews(pr["number"]):
            doc = dfcore.parse_review(r.get("body"))
            if doc is None:
                continue
            if r.get("commit_id") and r["commit_id"] != pr["head_sha"]:
                results.append({"valid": False, "why": [f"GitHub says this review is on {r['commit_id'][:12]}"],
                                "source": f"github-review:{r['id']}"})
                continue
            doc.setdefault("reviewer", {})
            doc["reviewer"]["identity"] = r.get("user")      # who wrote it is GitHub's answer, not the text's
            doc["reviewer"].setdefault("kind", "human")
            v = dfcore.validate_review(doc, frame, {**chosen}, impl)
            v["source"] = f"github-review:{r['id']}"
            results.append(v)
    except remote_mod.RemoteError as e:
        ev["tool"].append(f"RemoteError: {e}")
    # model reviews accepted by an earlier *completed* run, for this very frame
    for acc in prior.get("accepted_reviews") or []:
        if acc.get("frame_id") == frame["id"]:
            v = dfcore.validate_review(acc["review"], frame, {**chosen}, impl)
            v["source"] = f"accepted:{acc.get('run')}"
            results.append(v)
    covered = {l for r in results if r.get("valid") for l in r["complete"]}
    covered_by = {}
    for r in results:
        if r.get("valid"):
            for l in r["complete"]:
                covered_by.setdefault(l, set()).add(dfcore.review_model(r))
    indep_covered = {l for r in results if r.get("valid") and r["independent"] for l in r["complete"]}
    budget = rv.get("budget") or {}
    used = int(ev["history"].get("model_calls") or 0)
    for a in assignments:
        if a.get("provider"):
            # this model's own coverage: another model's review does not fill its slot
            need = [l for l in a["lenses"] if a["provider"] not in covered_by.get(l, set())]
        else:
            need = [l for l in a["lenses"] if l not in (indep_covered if a["independent"] else covered)]
        a["missing"] = need
        a["retries"] = int(rv.get("retries") or 0)
        a["by"] = "done" if not need else ("model" if review_mode == "model" else "external")
        if a["by"] == "model" and a["independent"] and "model" not in (rv.get("independent_by") or ["model", "human"]):
            a["by"] = "external"
            a["why"] = "the policy requires this independent review from a person"
    # A model review of a head whose checks are still running, or already failing, is spent on
    # something that is not the candidate yet: the failure alone decides the next step.
    chk_now = ev.get("checks") or {}
    not_ready = (chk_now.get("pending") or chk_now.get("failed") or chk_now.get("tool")
                 or ev["violations"] or (ev.get("trace") or {}).get("problems"))
    if not_ready:
        for a in assignments:
            if a["by"] == "model":
                a["by"] = "deferred"
                a["why"] = "waiting for this head's checks to complete without a failure"
    model_needed = [a for a in assignments if a["by"] == "model"]
    grant = bool(ev.get("review_grant"))
    if used + len(model_needed) > int(budget.get("model_calls") or 6) and not grant:
        for a in model_needed:
            a["by"] = "external"
            a["why"] = (f"the model review budget ({budget.get('model_calls') or 6} calls per task) is spent — "
                        f"`/devflow approve reviews {str(pr['head_sha'])[:12]}` grants one more round")
    rounds = int(ev["history"].get("review_rounds") or 0)
    if rounds >= int(rv.get("max_rounds") or 3) and not grant:
        for a in assignments:
            if a["by"] == "model":
                a["by"] = "external"
                a["why"] = "the review round limit is reached"
    spent = [a["why"] for a in assignments if a["by"] == "external" and ("budget" in (a.get("why") or "") or "round limit" in (a.get("why") or ""))]
    ev["reviews_budget_spent"] = spent[0] if spent else ""
    ev["lens_defs"] = chosen
    ev["prior_accepted"] = [a for a in prior.get("accepted_reviews") or [] if a.get("frame_id") == frame["id"]]
    ev["reviews"] = {"required": list(chosen), "why": why_chosen, "results": results,
                     "independent_needed": independent_needed, "independent_lenses": indep_lenses,
                     "distinct_models": need_models}
    ev["assignments"] = assignments
    ev["history"]["max_rounds"] = int(rv.get("max_rounds") or 3)

    # frozen materials and prompts for the model assignments
    if any(a["by"] == "model" for a in assignments):
        try:
            write_materials(rem, pol, hook_root, task, pr, changed, frame, crit, ev)
        except (remote_mod.RemoteError, dfcore.HookError) as e:
            ev["tool"].append(f"{type(e).__name__}: {e}")
        present = {k for k, v in (ev.get("materials") or {}).items() if v and k not in ("not_shown", "criteria_notes")}
        for l in chosen.values():
            l["read"] = list(l.get("read") or []) + [k for k in l.get("read_if_present") or []
                                                     if k in present and k not in (l.get("read") or [])]
        for a in assignments:
            if a["by"] == "model":
                a["prompt"] = write_prompt(a, chosen, frame, ev)
                a["expected"] = f"review_{a['reviewer']}.json"
    return done(assignments=assignments)


def write_materials(rem, pol, hook_root, task, pr, changed, frame, crit, ev):
    """Everything a reviewer may read, frozen in the workspace: the diff (base and head of every
    changed file), the task, the trace, and whatever the project's hook says the criteria are.
    A reviewer given only these starts without the implementer's context — which is what makes a
    model review separable from the implementation at all."""
    m = os.path.join(D, "materials")
    os.makedirs(m, exist_ok=True)
    limit = int((pol.get("reviews") or {}).get("max_file_bytes") or 60000)
    index = {"diff": [], "task": "task.json", "trace": None, "criteria": [], "contracts": [],
             "implementation": [], "cited": [], "reference": [], "artifact_ops": None, "checks": "checks.json",
             "not_shown": []}
    json.dump(task, open(os.path.join(m, "task.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # what ran on exactly this head, as the remote reports it — so a reviewer judges the tests'
    # adequacy knowing their results, instead of declaring every execution unverifiable
    json.dump({"head_sha": pr["head_sha"],
               "results": (ev.get("checks") or {}).get("results") or {},
               "note": "check runs on this head as GitHub reports them; a check's own logs and "
                       "artifacts are linked from the pull request, not copied here"},
              open(os.path.join(m, "checks.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for i, f in enumerate(changed["files"]):
        for side, sha in (("base", pr["base_sha"]), ("head", pr["head_sha"])):
            text = rem.file_at(sha, f)
            name = f"diff/{i:03d}-{side}-{os.path.basename(f)}.txt"
            p = os.path.join(m, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            # Never cut silently: a reviewer shown part of a file has not reviewed the file. What
            # does not fit is named, and the review of it stays unverified (CADP: lossy observation).
            if text is not None and len(text) > limit:
                index["not_shown"].append({"ref": f"diff:{f}@{side}", "bytes": len(text), "limit": limit})
                body = f"(not shown: {len(text)} bytes exceeds the {limit}-byte material limit)"
            else:
                body = "(absent on this side)" if text is None else text
            open(p, "w", encoding="utf-8").write(f"# {f} @ {side} {sha}\n{body}")
            index["diff"].append(name)
    if ev.get("trace"):
        index["trace"] = "trace.json"
        json.dump(ev["trace"], open(os.path.join(m, "trace.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if ev.get("shared", {}).get("required"):
        index["artifact_ops"] = "artifact_ops.json"
        json.dump(ev["shared"], open(os.path.join(m, "artifact_ops.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    got = dfcore.run_hook(pol, hook_root, "materials", {"task": task, "changed_files": changed["files"],
                                                        "classification": ev.get("classification")})
    # criteria and contracts as the canonical branch holds them; the implementation a verdict runs
    # through (the hook names it) as the head has it
    for kind, at in (("criteria", frame["canonical_head"]), ("contracts", frame["canonical_head"]),
                     ("implementation", pr["head_sha"])):
        for ref in got.get(kind) or []:
            text = rem.file_at(at, ref)
            name = f"{kind}/{ref.replace('/', '__')}.txt"
            p = os.path.join(m, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if text is not None and len(text) > limit:
                index["not_shown"].append({"ref": f"{kind}:{ref}", "bytes": len(text), "limit": limit})
                text = f"(not shown: {len(text)} bytes exceeds the {limit}-byte material limit)"
            open(p, "w", encoding="utf-8").write(f"# {ref} @ {at}\n" +
                                                 ("(absent)" if text is None else text))
            index[kind].append(name)
    # a research-lane record is reviewed with what its author was given: the reference material and
    # the criterion's own words, read from the official files at the canonical head
    if ev.get("lane") == "research":
        impl_pol = pol.get("implement") or {}
        got = refs.references(rem, impl_pol, os.path.join(m, "reference"), CACHE, RUN)
        index["reference"] = [f"reference/{r['as']}/" for r in got if r.get("files")]
        tree = refs.cached_tree(rem, frame["canonical_head"], CACHE, RUN)
        files, notes = refs.criteria_text(pol, hook_root, task, tree, os.path.join(m, "criteria_text"))
        index["criteria"] += [os.path.relpath(f, m).replace(os.sep, "/") for f in files]
        index["criteria_notes"] = notes
    # what a changed document cites: a record that says "collector/lib/core.sh:589 derives …" is
    # reviewed against that file as the head has it, or the claim can only be taken on trust
    # (measured: a research record's review left its base lens unverified for exactly this)
    for ref in cited_paths(rem, pr, changed["files"], limit=int((pol.get("reviews") or {}).get("max_cited") or 20)):
        text = rem.file_at(pr["head_sha"], ref)
        if text is None:
            continue
        name = f"cited/{ref.replace('/', '__')}.txt"
        p = os.path.join(m, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if len(text) > limit:
            index["not_shown"].append({"ref": f"cited:{ref}", "bytes": len(text), "limit": limit})
            text = f"(not shown: {len(text)} bytes exceeds the {limit}-byte material limit)"
        open(p, "w", encoding="utf-8").write(f"# {ref} @ {pr['head_sha']}\n" + text)
        index["cited"].append(name)
    json.dump(index, open(os.path.join(m, "index.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ev["materials"] = index


# a path with or without directories: a file at the repository root is cited as `collection-plan.tsv`
# (measured: both reviewers of a record left its claim about that file unverified, because it was
# the one cited file this pattern required a directory for). A path with directories may have any
# extension (a record cites fixture captures such as `cmd/svc_systemctl_list.stdout`); a bare name
# only a source or data extension, so host names are not looked up. Existence at the head filters the rest.
CITE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w-][\w.-]*\.[A-Za-z0-9]+|[\w-][\w.-]*\.(?:py|sh|yaml|yml|tsv|json|md|txt))(?::\d+)?(?![\w/-])")


def cited_paths(rem, pr, changed, limit=20):
    """Repository paths the changed documents mention (`dir/file.ext` or a root `file.ext`, optionally `:line`), that
    exist at the head and are not themselves part of the change — at most `limit`, in order of first
    mention."""
    out = []
    for f in changed:
        if not f.endswith((".md", ".txt", ".rst")):
            continue
        text = rem.file_at(pr["head_sha"], f) or ""
        for m in CITE.finditer(text):
            ref = m.group(1).lstrip("./")
            if ref in changed or ref in out or ref.startswith(("http", "www.")):
                continue
            out.append(ref)
    return [r for r in out[:limit * 3] if rem.file_at(pr["head_sha"], r) is not None][:limit]


# A research record is worth another round only when the items it serves would be worse off
# without it: a fact they need is missing, or one they would rely on cannot be trusted.
RESEARCH_BLOCKING = """- This is a research record: it serves the task's `items` and answers its `questions`. A finding
  is **blocking** only when it endangers them, and it names what in `affects`:
  - *insufficient* — a question the task lists, or a fact the served items need to be implemented,
    is not established by the record;
  - *unreliable* — a claim the served items would rely on is wrong, is attributed to a source that
    does not state it, or is stated wider than its evidence reaches.
  Everything else — a claim no served item relies on, wording, a peripheral citation, something
  that should be marked unverified but decides nothing for the items — is **advisory**: the record
  is merged with it noted, and no further round is spent on it. A blocking finding without
  `affects` is treated as advisory. The same holds for what you could not verify: if it endangers
  what the items need, make it a blocking finding with `affects`; declared under `unverified`, it
  is recorded as a limit of your review and does not hold the record.
"""


def write_prompt(a, chosen, frame, ev):
    tpl = open(os.path.join(PKG, "prompts", "review.md"), encoding="utf-8").read()
    lens_text = []
    for lid in a["lenses"]:
        l = chosen[lid]
        qs = "\n".join(f"    - `{q['id']}`: {q['q']}" for q in l.get("questions") or [])
        lens_text.append(f"- **{lid}** — {l.get('title', '')}\n  read: {', '.join(l.get('read') or [])}\n  questions:\n{qs}")
    body = (tpl.replace("{LENSES}", "\n".join(lens_text))
               .replace("{LENS_IDS}", json.dumps(a["lenses"]))
               .replace("{HEAD}", str(frame["head_sha"])).replace("{FRAME}", frame["id"])
               .replace("{POLICY}", frame["policy"]["digest"])
               .replace("{BASIS}", frame["basis"])
               .replace("{WEB}", refs.web_hosts(dfcore.RESEARCH_REVIEWER, PKG)
                        if a.get("principal") == dfcore.RESEARCH_REVIEWER
                        else "none — this review has no page fetching; search results, where your tools have them, are secondary sources")
               .replace("{BLOCKING}", RESEARCH_BLOCKING if ev.get("lane") == "research" else "")
               .replace("{MATERIALS}", os.path.join(D, "materials"))
               .replace("{OUT}", os.path.join(WS, f"review_{a['reviewer']}.json")))
    p = os.path.join(D, f"prompt_{a['reviewer']}.md")
    open(p, "w", encoding="utf-8").write(body)
    return p


if __name__ == "__main__":
    # a crash is still an answer: every declared key, status TOOL_FAILURE and the reason (step.main)
    sys.exit(step.main(lambda: main(sys.argv[1:]), evaluation="", need_models="no", assignments=0, models="", merged="no", frame="", head="", lane="change", error=dfcore.CRASH))
