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
import stackenv  # noqa: E402
import remote as remote_mod  # noqa: E402

WS_ROOT, PROXY = stackenv.load()
RUN = os.environ.get("CONDUCTOR_SELF_RUN_ID", "manual")
WS = os.path.join(WS_ROOT, RUN)
D = os.path.join(WS, "devflow")
HANDOFF = os.environ.get("DEVFLOW_HANDOFF", "/work/handoff")
SAFE = re.compile(r"[A-Za-z0-9._-]{1,80}")


def emit(**kw):
    print(json.dumps(kw, ensure_ascii=False))


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


def implementers_of(task, pr, changed):
    ids = set(changed.get("authors") or [])
    if pr and pr.get("author"):
        ids.add(pr["author"])
    impl = task.get("implementer") or {}
    ids |= set(impl.get("identities") or [])
    return {"identities": sorted(i for i in ids if i),
            "principals": list(impl.get("principals") or []),
            "sessions": list(impl.get("sessions") or [])}


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
          "issue": issue_no, "review_mode": review_mode, "tool": [], "prepare": {"gaps": [], "decisions": [], "problems": []}}

    def done(**extra):
        ev.update(extra)
        json.dump(ev, open(os.path.join(D, "evaluation.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        need = [a for a in ev.get("assignments") or [] if a.get("by") == "model"]
        emit(status="TOOL_FAILURE" if ev["tool"] else "OK", evaluation=os.path.join(D, "evaluation.json"),
             need_models="yes" if need and not ev["tool"] else "no", assignments=len(need),
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
    except (dfcore.ConfigError, remote_mod.RemoteError) as e:
        ev["tool"].append(f"{type(e).__name__}: {e}" + (f" ({e.hint})" if getattr(e, "hint", "") else ""))
        return done()

    prior = dfcore.read_state(comments)
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
        pr = rem.pull(task["pr"]) if task.get("pr") else None
        changed = rem.changed_files(pr["base_sha"], pr["head_sha"]) if pr else {"files": [], "authors": []}
        crit = dfcore.criteria_blobs(rem, pol, canonical_head)
    except (dfcore.ConfigError, remote_mod.RemoteError) as e:
        ev["tool"].append(f"{type(e).__name__}: {e}" + (f" ({e.hint})" if getattr(e, "hint", "") else ""))
        return done()

    wf = dfcore.workflow_identity()
    frame = dfcore.frame_of(reg, pol, wf, pr, canonical_head, crit, hook_digests, task)
    ev.update(frame=frame, pr=pr, changed=changed["files"], policy={"ref": pol["_ref"], "digest": pol["_digest"]})
    impl = implementers_of(task, pr, changed)
    ev["implementers"] = impl

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
    given, excepts = approvals(comments, pol, impl)
    ev["approvals"] = given + [dict(e, gate="except") for e in excepts]
    ev["end_point"] = str(task.get("end_point") or (pol.get("defaults") or {}).get("end_point") or "integration")
    loop = pol.get("loop") or {}
    ev["history"]["max_no_progress"] = int(loop.get("max_no_progress") or 3)
    if loop.get("max_rework") is not None:
        ev["history"]["max_rework"] = int(loop["max_rework"])
    # what a gate approves is the task's definition, not the links filled in as it progresses
    task_digest = dfcore.sha256(dfcore.canon({k: v for k, v in task.items()
                                              if k not in ("issue", "pr", "transaction_commit")}))[:12]
    ev["task_digest"] = task_digest
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
    indep_covered = {l for r in results if r.get("valid") and r["independent"] for l in r["complete"]}
    budget = rv.get("budget") or {}
    used = int(ev["history"].get("model_calls") or 0)
    for a in assignments:
        need = [l for l in a["lenses"] if l not in (indep_covered if a["independent"] else covered)]
        a["missing"] = need
        a["by"] = "done" if not need else ("model" if review_mode == "model" else "external")
        if a["by"] == "model" and a["independent"] and "model" not in (rv.get("independent_by") or ["model", "human"]):
            a["by"] = "external"
            a["why"] = "the policy requires this independent review from a person"
    model_needed = [a for a in assignments if a["by"] == "model"]
    if used + len(model_needed) > int(budget.get("model_calls") or 6):
        for a in model_needed:
            a["by"] = "external"
            a["why"] = f"the model review budget ({budget.get('model_calls') or 6} calls per task) is spent"
    rounds = int(ev["history"].get("review_rounds") or 0)
    if rounds >= int(rv.get("max_rounds") or 3):
        for a in assignments:
            if a["by"] == "model":
                a["by"] = "external"
                a["why"] = "the review round limit is reached"
    ev["lens_defs"] = chosen
    ev["prior_accepted"] = [a for a in prior.get("accepted_reviews") or [] if a.get("frame_id") == frame["id"]]
    ev["reviews"] = {"required": list(chosen), "why": why_chosen, "results": results,
                     "independent_needed": independent_needed, "independent_lenses": indep_lenses}
    ev["assignments"] = assignments
    ev["history"]["max_rounds"] = int(rv.get("max_rounds") or 3)

    # frozen materials and prompts for the model assignments
    if any(a["by"] == "model" for a in assignments):
        try:
            write_materials(rem, pol, hook_root, task, pr, changed, frame, crit, ev)
        except (remote_mod.RemoteError, dfcore.HookError) as e:
            ev["tool"].append(f"{type(e).__name__}: {e}")
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
             "artifact_ops": None, "not_shown": []}
    json.dump(task, open(os.path.join(m, "task.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
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
    for kind in ("criteria", "contracts"):
        for ref in got.get(kind) or []:
            text = rem.file_at(frame["canonical_head"], ref)
            name = f"{kind}/{ref.replace('/', '__')}.txt"
            p = os.path.join(m, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if text is not None and len(text) > limit:
                index["not_shown"].append({"ref": f"{kind}:{ref}", "bytes": len(text), "limit": limit})
                text = f"(not shown: {len(text)} bytes exceeds the {limit}-byte material limit)"
            open(p, "w", encoding="utf-8").write(f"# {ref} @ {frame['canonical_head']}\n" +
                                                 ("(absent)" if text is None else text))
            index[kind].append(name)
    json.dump(index, open(os.path.join(m, "index.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ev["materials"] = index


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
               .replace("{MATERIALS}", os.path.join(D, "materials"))
               .replace("{OUT}", os.path.join(WS, f"review_{a['reviewer']}.json")))
    p = os.path.join(D, f"prompt_{a['reviewer']}.md")
    open(p, "w", encoding="utf-8").write(body)
    return p


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:           # a step answers in JSON, it does not raise (docs/packages.md)
        emit(status="TOOL_FAILURE", evaluation="", need_models="no", assignments=0, merged="no",
             frame="", head="", error=f"{type(e).__name__}: {e}"[:300])
