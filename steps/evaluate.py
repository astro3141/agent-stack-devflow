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
CACHE = dfcore.state_dir("tree-cache")   # commit trees, kept across runs (requires.state)
SAFE = re.compile(r"[A-Za-z0-9._-]{1,80}")
# the longest prompt a brokered call carries (agent-stack stack/profile_runner.py refuses past
# 200,000 characters), less room for nothing in particular
QUERY_MAX_CHARS = 190000




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
    shared = step.handoff(f"{name}.json")      # <handoff_root>/<name>.json, the stack's root
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
    superseded = {p["number"] for p in hist.get("superseded_prs") or []}
    if task.get("pr") and task["pr"] not in superseded:
        return rem.pull(task["pr"])
    if hist.get("pr"):
        return rem.pull(hist["pr"])
    return rem.find_pull(dfcore.task_branch(task))


def rules_changed_since(rem, pol, hold, canonical_head, ev):
    """The project's rule files (its policy, hook, lenses, templates, instructions — `.devflow/` and
    the policy's rule_paths) that changed on the canonical branch since the hold was prepared; []
    when none, or when it cannot be told. A hold written before this records where it was prepared
    in the preparation commit's parent."""
    at = hold.get("rules_at")
    try:
        if not at and hold.get("head"):
            c = rem.commit(hold["head"])
            at = (c or {}).get("parents", [None])[0]
        if not at or at == canonical_head:
            return []
        files = rem.changed_files(at, canonical_head)["files"]
    except remote_mod.RemoteError as e:
        ev["tool"].append(f"RemoteError: {e}")
        return []
    rules = list(pol.get("rule_paths") or [])
    return sorted(f for f in files if f.startswith(".devflow/") or remote_mod.glob_any(f, rules))


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
        step.out(status="TOOL_FAILURE" if ev["tool"] else "OK", evaluation=os.path.join(D, "evaluation.json"),
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
        pr = find_pr(rem, task, ev["history"])
        pol = dfcore.load_policy(rem, reg, canonical_head)
        approval_ref = canonical_head
        live_flow = (pol.get("flow_defs") or {}).get(
            (pol.get("flows") or {}).get(task.get("change_class")), {}) or {}
        promotion_pass = ev["history"].get("promotion_pass") or {}
        # After merge, approval inputs are read at M's first parent, never from the guard B just
        # promoted. The old PASS's workflow identity is trusted only from our own published state,
        # and only for the same head. Candidate JSON cannot choose this historical trust anchor.
        merged_promotion_required = bool(pr and pr.get("merged") and (live_flow.get("promotion") or promotion_pass))
        if merged_promotion_required:
            merge = rem.commit(pr.get("merge_commit_sha")) if pr.get("merge_commit_sha") else None
            parents = list((merge or {}).get("parents") or [])
            if len(parents) == 2:
                approval_ref = parents[0]
                pol = dfcore.load_policy(rem, reg, approval_ref)
                pr = dict(pr, observed_base_sha=pr["base_sha"], base_sha=approval_ref)
            historical_flow = (pol.get("flow_defs") or {}).get(
                (pol.get("flows") or {}).get(task.get("change_class")), {}) or {}
            if not historical_flow.get("promotion"):
                ev["prepare"]["problems"].append("the merged Foundation has no promotion contract at its actual pre-merge base")
        hook_root = os.path.join(D, "policy")
        hook_digests = dfcore.materialize_hook(rem, pol, approval_ref, hook_root)
        base_lenses, lenses = dfcore.load_lenses(pol, hook_root)
        changed = rem.changed_files(pr["base_sha"], pr["head_sha"]) if pr else {"files": [], "authors": []}
        crit = dfcore.criteria_blobs(rem, pol, approval_ref)
    except (dfcore.ConfigError, remote_mod.RemoteError) as e:
        ev["tool"].append(f"{type(e).__name__}: {e}" + (f" ({e.hint})" if getattr(e, "hint", "") else ""))
        return done()

    wf = dfcore.workflow_identity()
    if pr and pr.get("merged") and promotion_pass.get("head") == pr["head_sha"]:
        wf = promotion_pass.get("workflow") or wf
    frame = dfcore.frame_of(reg, pol, wf, pr, canonical_head, crit, hook_digests, task)
    ev.update(frame=frame, pr=pr, changed=changed["files"], policy={"ref": pol["_ref"], "digest": pol["_digest"]},
              policy_doc={k: v for k, v in pol.items() if not k.startswith("_")},
              canonical_head=canonical_head, branch=dfcore.task_branch(task))
    ev["approval_ref"] = approval_ref
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
        blobs = dfcore.hook_blobs(pol, changed["files"], lambda path: rem.file_at(pr["base_sha"], path),
                                  lambda path: rem.file_at(pr["head_sha"], path)) if pr else {}
        cls = dfcore.run_hook(pol, hook_root, "classify", {
            "task": task, "changed_files": changed["files"], "authors": changed.get("authors") or [],
            "pr": {k: (pr or {}).get(k) for k in ("number", "author", "base_ref", "head_ref")} if pr else None,
            "canonical_head": canonical_head, "blobs": blobs})
    except dfcore.HookError as e:
        ev["tool"].append(f"HookError: {e}")
        return done()
    cls.setdefault("impacts", {})
    # what the project's reading says this change must show — shown with the state, judged below
    ev["required_evidence"] = list(cls.get("required_evidence") or []) if isinstance(cls.get("required_evidence"), list) \
        else (["criteria_mapping"] if cls.get("trace_required") else [])
    for n in cls.get("notes") or []:
        ev.setdefault("notes", []).append(f"classification: {n}")
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
    # Resolve the flow before Gate1: a privileged project's operational verifier/writer blobs are
    # part of what the person approves, not merely paths named by the policy document.
    flow_name = (pol.get("flows") or {}).get(cls.get("change_class"), "pull_request")
    flow = (pol.get("flow_defs") or {}).get(flow_name) or {}
    flow_kind = str(flow.get("kind") or flow_name)
    promotion = flow.get("promotion") or {}
    approval_paths = sorted({dfcore.flow_path(p, task) for p in promotion.get("approval_paths") or []})
    try:
        approval_blobs = {p: rem.blob_id(approval_ref, p) for p in approval_paths}
    except remote_mod.RemoteError as e:
        ev["tool"].append(f"RemoteError: {e}")
        return done(assignments=[])
    missing_approval_blobs = sorted(p for p, blob in approval_blobs.items() if not blob)
    if missing_approval_blobs:
        ev["prepare"]["problems"].append(
            f"the privileged flow approval paths are absent on canonical: {missing_approval_blobs}")
    # A proposal approval also binds the rules and workflow that give the approved scope meaning.
    # A policy transition therefore cannot silently reuse a gate1 comment made under the old lane.
    proposal_digest = dfcore.sha256(dfcore.canon({"task": task_digest, "policy": pol["_digest"],
                                                  "workflow": wf.get("digest"),
                                                  "hooks": hook_digests,
                                                  "approval_blobs": approval_blobs}))[:12]
    ev["proposal_digest"] = proposal_digest
    ev["proposal_bindings"] = approval_blobs

    # A named flow may declare that it is still a pull-request flow.  This lets a project require a
    # privileged two-commit promotion envelope without falling into the legacy out-of-band
    # transaction path merely because its flow has a more precise name.
    ev["flow"] = {"name": flow_name, "kind": flow_kind,
                  **{k: flow.get(k) for k in ("need", "owner", "resume_when")},
                  "promotion": flow.get("promotion")}
    # What devflow's own preparation could not settle (a mapping it could not confirm, a gap in the
    # contract) holds the task until a person records the answer in the task block — which changes
    # the task digest the hold is bound to.
    hold = ev["history"].get("prepare_hold") or {}
    # a head that holds only that preparation is not an implementation, whatever its checks say
    ev["prepare_only"] = bool(hold) and bool(pr) and hold.get("head") == pr.get("head_sha")
    # Preparation answers the task under the project's rules as they were; when the rules changed
    # since (the hook, the instructions, the policy — what preparation read), its questions may be
    # answered or asked differently now, so it is done again. Measured: U-37 (#21) asked a person to
    # compare its quotes with the PDF because the hook could not read it; once it could, the hold
    # stood until the task changed.
    rules_changed = hold and rules_changed_since(rem, pol, hold, canonical_head, ev)
    if rules_changed:
        ev.setdefault("notes", []).append(
            f"the project's rules changed since preparation ({', '.join(rules_changed[:4])}): it is done again")
    ev["hold_released"] = bool(ev["prepare_only"]) and not (hold.get("task_digest") == task_digest and not rules_changed)
    if hold and hold.get("task_digest") == task_digest and not rules_changed:
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
        want = (task_digest if g.get("binds") == "task" else
                proposal_digest if g.get("binds") == "proposal" else
                (pr or {}).get("head_sha"))
        accepted = (str(want),) if g.get("exact") else (str(want), str(want)[:12])
        hit = [a for a in given if a["gate"] == g["id"] and a["counted"] and want
               and a["value"] in accepted]
        gate_state.append({"id": g["id"], "phase": g.get("phase", "prepare"), "binds": g.get("binds"),
                           "value": want, "exact": bool(g.get("exact")),
                           "approved": bool(hit), "by": [a["by"] for a in hit]})
    ev["gates"] = gate_state
    ev["violations"] = list(cls.get("violations") or [])

    # A privileged PR is valid only as the configured A+B envelope: exact product scope in A,
    # inert promotion files in B, and (after integration) an unmodified merge-commit tree.
    if flow_kind == "pull_request" and promotion and pr:
        try:
            ev["promotion"] = dfcore.validate_promotion(rem, promotion, task, task_digest,
                                                         proposal_digest, pr, canonical_head,
                                                         reg.get("repository"), reg.get("canonical_branch"))
            ev["violations"] += [f"promotion: {p}" for p in ev["promotion"].get("problems") or []]
        except remote_mod.RemoteError as e:
            ev["tool"].append(f"RemoteError: {e}")
            return done(assignments=[])

    # A genuinely non-PR flow remains the legacy externally published transaction path.
    if flow_kind != "pull_request":
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
        promotion_ok = (not promotion and not merged_promotion_required) or (bool((ev.get("promotion") or {}).get("valid"))
            and promotion_pass.get("head") == pr["head_sha"]
            and promotion_pass.get("proposal_digest") == proposal_digest
            and promotion_pass.get("approval_ref") == approval_ref
            and promotion_pass.get("gates_approved") is True)
        ev["finalize"] = {"post": dfcore.evaluate_checks(post, {}, runs, pr.get("merge_commit_sha")),
                          "delivery": dfcore.evaluate_checks(delivery, {}, runs, pr.get("merge_commit_sha"))
                          if ev["end_point"] == "delivery" else None,
                          "merge_commit": pr.get("merge_commit_sha"),
                          "passed_head": (ev["history"].get("passed_head") or "") if promotion_ok else "",
                          "why_no_pass": (None if promotion_ok else
                                          "; ".join((ev.get("promotion") or {}).get("problems") or [])
                                          or "no pre-merge PASS with all human gates for this exact proposal and head")}
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
    if dfcore.criteria_mapping_required(cls):
        tpath = (pol.get("trace") or {}).get("path", ".devflow/tasks/{task}.yaml").format(task=task["id"])
        try:
            text = rem.file_at(pr["head_sha"], tpath)
            ev["trace"] = dfcore.run_hook(pol, hook_root, "validate_trace", {
                "task": task, "trace_path": tpath, "trace_text": text, "criteria": crit,
                "change_class": cls.get("change_class")})
            ev["trace"]["path"] = tpath
        except (remote_mod.RemoteError, dfcore.HookError) as e:
            ev["tool"].append(f"{type(e).__name__}: {e}")
        if ev["trace"] and ev.get("hold_released"):
            # the trace on a head that holds only a preparation is that preparation's; its hold is
            # released (the task or the rules changed), so its open questions are asked again by
            # the next preparation, not held over from the last one (measured on U-37, #21)
            ev.setdefault("notes", []).append("the held preparation's trace is not judged: it is prepared again")
        elif ev["trace"]:
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
            # what a reviewer needs to judge "regenerated on the latest canonical head, in the
            # confirmed order" without looking anywhere else (shared-artifact is a query): which
            # canonical head this head contains, which of this head's checks regenerated and
            # verified the artifact (the policy's `verified_by`), and how merges onto it are ordered
            results = (ev.get("checks") or {}).get("results") or {}
            regen = {a["id"]: {c: (results.get(c) or {}).get("result", "NOT_RUN") for c in a.get("verified_by") or []}
                     for a in affected}
            order = {}
            for a in affected:
                lock = a.get("lock", a["id"])
                order[a["id"]] = {"lock": f"refs/devflow/lock/{lock}", "held_by": rem.ref(f"refs/devflow/lock/{lock}"),
                                  "rule": "changes to this artifact merge one at a time: devflow takes the lock for a "
                                          "head at integration and releases it when that head is merged or replaced"}
            ev["shared"] = {"required": True, "rebased": rebased, "ops_match": match if rebased else None,
                            "artifacts": [a["id"] for a in affected], "lock": [a.get("lock", a["id"]) for a in affected],
                            "detail": detail,
                            "canonical_branch": reg["canonical_branch"], "canonical_head": canonical_head,
                            "head_sha": pr["head_sha"], "head_contains_canonical_head": rebased,
                            "regenerated_by": regen or None, "order": order}
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
    # A head that holds only devflow's preparation (waiting for a decision, a gap or research) is
    # not an implementation: reviewing it as one spends model calls on nothing to judge. Measured:
    # #19's preparation head, once its CI had finished, was sent to four implementation reviewers.
    if ev.get("prepare_only"):
        for a in assignments:
            if a["by"] == "model":
                a["by"] = "deferred"
                a["why"] = "the head holds only preparation; it is reviewed once it is implemented"
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
            # a lens that reads the repository gets the tree of exactly the judged head, frozen in
            # the materials like the rest: read where its questions need it, never pasted inline
            reads = {k for a in assignments if a["by"] == "model" for l in a["lenses"] for k in chosen[l].get("read") or []}
            if "repository" in reads:
                refs.copy_tree(refs.cached_tree(rem, pr["head_sha"], CACHE, RUN), os.path.join(D, "materials", "repository"))
                ev["materials"]["repository"] = "repository/"
                json.dump(ev["materials"], open(os.path.join(D, "materials", "index.json"), "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
        except (remote_mod.RemoteError, dfcore.HookError) as e:
            ev["tool"].append(f"{type(e).__name__}: {e}")
        present = {k for k, v in (ev.get("materials") or {}).items() if v and k not in ("not_shown", "criteria_notes")}
        for l in chosen.values():
            l["read"] = list(l.get("read") or []) + [k for k in l.get("read_if_present") or []
                                                     if k in present and k not in (l.get("read") or [])]
        # A judge or an investigator. When every piece of evidence a review needs can be listed
        # before the call — the lens's materials, closed — it is a judge: the materials in the
        # prompt, a closed call (the stack's `closed` execution profile: no tools, one turn). When
        # finding what evidence is needed is itself the review, it is an investigator with tools: a
        # lens that reads the repository, a prompt past what a brokered call carries, a research
        # record's review (it fetches primary pages). Either way the answer is the review's schema,
        # checked by the stack (--output-schema, agent-stack §102). Measured on #24: lenses that
        # investigate, judged closed, came back unverified with one model and complete with another;
        # given the repository with tools, they completed.
        judges = ev.get("lane") != "research" and str(rv.get("mode") or "query") == "query"
        for a in assignments:
            if a["by"] == "model":
                a["expected"] = f"review_{a['reviewer']}.json"
                a["schema"] = write_schema(a, chosen, frame)
                # a lens that reads the repository reads the code it needs with tools
                browses = any("repository" in (chosen[l].get("read") or []) for l in a["lenses"])
                if judges and not browses:
                    a["prompt"], a["inlined"] = write_query(a, chosen, frame, ev)
                    a["closed"] = True
                    # a brokered call's prompt is bounded (agent-stack profile_runner: 200,000
                    # characters, refused past that). Measured: a scanner-outcomes review with its
                    # contracts and implementation inline came to 370 KB and was refused twice.
                    # Such a review reads its materials with its tools instead.
                    size = len(open(a["prompt"], encoding="utf-8").read())
                    limit = int(rv.get("query_max_chars") or QUERY_MAX_CHARS)
                    if size > limit:
                        a.pop("closed"), a.pop("inlined")
                        a["prompt"] = write_prompt(a, chosen, frame, ev)
                        ev.setdefault("notes", []).append(
                            f"review {a['reviewer']} reads its materials with tools: inline they come to "
                            f"{size} characters, past the {limit} a brokered prompt may carry")
                else:
                    a["prompt"] = write_prompt(a, chosen, frame, ev)
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


def review_schema(a, chosen, frame):
    """The answer a review query must give (agent-stack: the schema keywords the door checks). The
    head, frame and digests are constants: a review for anything else is not one. Question answers
    are not required here — an unanswered question leaves its lens incomplete (validate_review),
    which is a review that says less, not one that broke its shape."""
    lenses = list(a["lenses"])
    texts = {"type": "array", "items": {"type": "string"}}
    by_lens = lambda value: {"type": "object", "additionalProperties": False,
                             "properties": {l: value for l in lenses}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"devflow review {a['reviewer']}",
        "type": "object", "additionalProperties": False,
        "required": ["head_sha", "frame_id", "policy_digest", "basis_digest", "lenses", "materials",
                     "answers", "findings", "unverified", "limits", "verdict"],
        "properties": {
            "head_sha": {"const": frame["head_sha"]},
            "frame_id": {"const": frame["id"]},
            "policy_digest": {"const": frame["policy"]["digest"]},
            "basis_digest": {"const": frame["basis"]},
            "lenses": {"type": "array", "items": {"enum": lenses}},
            "materials": {"type": "array", "items": {
                "type": "object", "required": ["ref", "read"], "additionalProperties": False,
                "properties": {"ref": {"type": "string"}, "read": {"type": "boolean"}}}},
            "answers": by_lens({"type": "object", "additionalProperties": False,
                                "properties": {q["id"]: {"type": "string"}
                                               for l in lenses for q in (chosen[l].get("questions") or [])}}),
            "findings": {"type": "array", "items": {
                "type": "object", "required": ["lens", "severity", "claim", "basis", "evidence"],
                "additionalProperties": False,
                "properties": {
                    "lens": {"enum": lenses},
                    "severity": {"enum": ["blocking", "advisory"]},
                    "claim": {"type": "string", "minLength": 1},
                    "basis": {"type": "object", "required": ["kind", "ref"], "additionalProperties": False,
                              "properties": {"kind": {"enum": ["criterion", "contract", "observation", "test", "task"]},
                                             "ref": {"type": "string"}}},
                    "evidence": {"type": "string"},
                    "affects": {"type": "string"}}}},
            "unverified": by_lens(texts),
            "limits": by_lens(texts),
            "verdict": {"enum": ["PASS", "REQUEST_CHANGES", "CONTRACT_GAP", "NEEDS_DECISION"]},
        },
    }


def write_schema(a, chosen, frame):
    """The review's answer contract, as a file: the door checks every model review against it
    (--output-schema), whether the review judges closed or investigates with tools."""
    sp = os.path.join(D, f"schema_{a['reviewer']}.json")
    json.dump(review_schema(a, chosen, frame), open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return sp


def write_query(a, chosen, frame, ev):
    """A closed review (a judge): the instructions, the review's schema, and every material its
    lenses read, inline. Returns (prompt path, the material kinds put in the prompt) — what was
    put in is what was read, whatever the answer lists (adjudicate.py)."""
    m = os.path.join(D, "materials")
    index = ev.get("materials") or {}
    kinds = []
    for l in a["lenses"]:
        kinds += [k for k in chosen[l].get("read") or [] if k not in kinds]
    sections, inlined = [], []
    for k in kinds:
        entry = index.get(k)
        names = entry if isinstance(entry, list) else ([entry] if entry else [])
        if not names:
            continue
        inlined.append(k)
        for name in names:
            p = os.path.join(m, name)
            if os.path.isdir(p):
                continue
            try:
                body = open(p, encoding="utf-8").read()
            except OSError as e:
                body = f"(not readable: {type(e).__name__})"
            sections.append(f"## {k}: {name}\n\n````\n{body}\n````\n")
    if index.get("not_shown"):
        sections.append("## not_shown\n\n" + "\n".join(f"- {x['ref']} ({x['bytes']} bytes > {x['limit']})"
                                                         for x in index["not_shown"]) + "\n")
    schema = review_schema(a, chosen, frame)
    lens_text = []
    for lid in a["lenses"]:
        l = chosen[lid]
        qs = "\n".join(f"    - `{q['id']}`: {q['q']}" for q in l.get("questions") or [])
        lens_text.append(f"- **{lid}** — {l.get('title', '')}\n  read: {', '.join(l.get('read') or [])}\n  questions:\n{qs}")
    tpl = open(os.path.join(PKG, "prompts", "review_query.md"), encoding="utf-8").read()
    body = (tpl.replace("{LENSES}", "\n".join(lens_text))
               .replace("{LENS_IDS}", json.dumps(a["lenses"]))
               .replace("{HEAD}", str(frame["head_sha"])).replace("{FRAME}", frame["id"])
               .replace("{POLICY}", frame["policy"]["digest"])
               .replace("{BASIS}", frame["basis"])
               .replace("{BLOCKING}", "")
               .replace("{SCHEMA}", json.dumps(schema, ensure_ascii=False, indent=1))
               .replace("{MATERIAL_TEXT}", "\n".join(sections) or "(none)"))
    pp = os.path.join(D, f"prompt_{a['reviewer']}.md")
    open(pp, "w", encoding="utf-8").write(body)
    return pp, inlined


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
    sys.exit(step.main(lambda: main(sys.argv[1:]), evaluation="", need_models="no", assignments=0, models="", merged="no", frame="", head="", lane="change", error="", failure_key="error"))
