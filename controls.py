"""devflow controls — the workflow's own risks, driven through its real steps against fixture remotes.

usage (agent container):  /opt/venv/bin/python /work/packages/devflow/controls.py

Every scenario runs the real step files as subprocesses (evaluate → adjudicate → integrate →
publish), exactly as Conductor runs them, against a fixture remote built by fixtures/build.py
with one thing changed. No model call, no network, no GitHub. What this proves is the machinery:
that a verdict follows from the evidence, and that stale, failed, missing or repeated evidence
does not turn into a PASS or a duplicate effect. It proves nothing about the scanner.

Groups, by the risk each pins:
  flow        normal progress to done across runs; each verdict's return, wait and resume
  block       a failed, pending, skipped or unreviewed head is never integrated
  stale       a review of an older head, or of another policy, is not a review of this one
  foundation  independence is an identity separate from the implementer — not another model
  evidence    a failed attempt's output, or a file an earlier attempt left, is not evidence
  shared      the shared Plan: canonical moved, operations differ, counts, the lock
  effects     an effect that happened with its answer lost is not repeated on resume
  rules       a change to the rules is judged by the canonical rules, not by itself
  tool        a machinery failure is TOOL_FAILURE with no verdict — never a rejection
  implement   devflow prepares and implements within the canonical rules; what it may push, when it
              stops, whose branch it is
"""
import contextlib, copy, hashlib, io, json, os, secrets, shutil, subprocess, sys, tempfile

PKG = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PKG, "fixtures"))
sys.path.insert(0, os.path.join(PKG, "lib"))
import build  # noqa: E402
sys.path.insert(0, "/work/stack/steps")
import step as stack_step  # noqa: E402 — the roots, as the steps see them

PY = sys.executable
STEPS = os.path.join(PKG, "steps")
# under scripts/packages.sh controls the workspace, evidence and state roots are a scratch
# directory for this process (agent-stack #54); the hand-in root is the instance's own
PATHS = stack_step.runtime()["paths"]
WS_ROOT = PATHS["workspace_root"]
HANDOFF_ROOT = PATHS["handoff_root"]
PASS_, FAIL_ = [], []


def check(name, got, want):
    ok = got == want
    (PASS_ if ok else FAIL_).append(name)
    print(f"  {'ok  ' if ok else 'FAIL'}  {name:<74} {got!r}" + ("" if ok else f"  (expected {want!r})"))


def step(name, args, env):
    p = subprocess.run([PY, os.path.join(STEPS, name)] + args, capture_output=True, text=True, env=env, cwd="/work")
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"status": "CRASH", "error": (p.stderr or p.stdout)[-400:]}


class Sim:
    """A task on a fixture remote that persists across runs, like a real one."""

    def __init__(self, db):
        # the fixture remote is a hand-in by name (step.handoff), under the instance's hand-in
        # root, named for this control alone and removed by close()
        self.name = "ctl-" + secrets.token_hex(6)
        os.makedirs(HANDOFF_ROOT, exist_ok=True)
        self.path = os.path.join(HANDOFF_ROOT, f"{self.name}.json")
        self.save(db)
        self.runs = []

    def save(self, db):
        json.dump(db, open(self.path, "w", encoding="utf-8"), ensure_ascii=False)

    @property
    def db(self):
        return json.load(open(self.path, encoding="utf-8"))

    def edit(self, fn):
        db = self.db
        out = fn(db)
        self.save(db)
        return out

    def run(self, review_mode="external", receipt=None, project="scanner-fixture"):
        rid = "ctl-" + secrets.token_hex(4)
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": rid}
        ws = os.path.join(WS_ROOT, rid)
        r = {"ws": ws}
        r["evaluate"] = step("evaluate.py", [project, "3", self.name, review_mode], env)
        rc = receipt(ws) if receipt else ""
        r["adjudicate"] = step("adjudicate.py", [rc], env)
        a = r["adjudicate"]
        if a.get("integrate") == "yes" or a.get("phase") == "done":
            r["integrate"] = step("integrate.py", [project], env)
        r["publish"] = step("publish.py", [project], env)
        try:
            r["state"] = json.load(open(os.path.join(ws, "devflow", "state.json"), encoding="utf-8"))
        except Exception:
            r["state"] = {}
        self.runs.append(ws)
        return r

    def run_pass(self, model=None, project="scanner-fixture", review_mode="external"):
        """One pass as devflow.yaml wires it, with act and the implementation branch. `model(stage,
        ws)` stands in for the model steps: it writes into ws/repo and ws/devflow/<stage>.json and
        returns whether it produced its file (a model that fails returns False)."""
        rid = "ctl-" + secrets.token_hex(4)
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": rid}
        ws = os.path.join(WS_ROOT, rid)
        self.runs.append(ws)
        r = {"ws": ws}
        r["evaluate"] = step("evaluate.py", [project, "3", self.name, review_mode], env)
        r["adjudicate"] = step("adjudicate.py", [""], env)
        r["act"] = step("act.py", [], env)
        do = r["act"].get("do")
        if do in ("prepare", "implement", "rebase", "research"):
            r["work"] = step("work.py", [project, do], env)
            if r["work"].get("status") == "OK":
                produced = {"prepare": "", "implement": "", "research": ""}
                if do == "research":
                    produced["research"] = str(bool(model and model("research", ws)))
                if do == "prepare":
                    produced["prepare"] = str(bool(model and model("prepare", ws)))
                    r["proceed"] = step("proceed.py", [produced["prepare"]], env)
                if do == "implement" or (do == "prepare" and r["proceed"].get("proceed") == "yes"):
                    produced["implement"] = str(bool(model and model("implement", ws)))
                r["deliver"] = step("deliver.py", [project, do, produced["prepare"], "run-prep" if produced["prepare"] else "",
                                                   produced["implement"], "run-impl" if produced["implement"] else "",
                                                   produced["research"], "run-res" if produced["research"] else ""], env)
        elif r["adjudicate"].get("integrate") == "yes" or r["adjudicate"].get("phase") == "done":
            r["integrate"] = step("integrate.py", [project], env)
        r["publish"] = step("publish.py", [project], env)
        try:
            r["state"] = json.load(open(os.path.join(ws, "devflow", "state.json"), encoding="utf-8"))
        except Exception:
            r["state"] = {}
        return r

    def close(self):
        try:
            os.remove(self.path)
        except OSError:
            pass
        for ws in self.runs:
            shutil.rmtree(ws, ignore_errors=True)


def R(db):
    return db["repos"][build.REPO]


def verdict(r):
    return r["state"].get("verdict")


def phase(r):
    return r["state"].get("phase")


def set_runs(db, sha, runs):
    R(db)["check_runs"][sha] = runs


def canonical_digest(db):
    r = R(db)
    text = r["commits"][r["branches"][build.CANONICAL]]["files"][".devflow/project.yaml"]
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def canonical_basis(db):
    r = R(db)
    return build.basis_of(r["commits"][r["branches"][build.CANONICAL]]["files"])


def add_review(db, head, lenses=build.LENSES_PASS, user="rev-person", **kw):
    """A review written now: under the policy, criteria and contracts on canonical now."""
    rv = R(db)["reviews"].setdefault("7", [])
    rv.append({"id": 200 + len(rv), "user": user, "commit_id": head, "state": "COMMENTED",
               "body": build.review_body(head, lenses, digest=canonical_digest(db), basis=canonical_basis(db), **kw)})


def edit_policy(db, fn, head_too=True):
    """Change the policy on the canonical commit, as if it had been merged there — and, by default,
    in the pull request's head too, which was made on top of it. head_too=False is canonical
    moving under a head that does not contain the change."""
    import yaml
    r = R(db)
    c = r["branches"][build.CANONICAL]
    pol = yaml.safe_load(r["commits"][c]["files"][".devflow/project.yaml"])
    fn(pol)
    text = yaml.safe_dump(pol, allow_unicode=True, sort_keys=False)
    r["commits"][c]["files"][".devflow/project.yaml"] = text
    if head_too:
        r["commits"][r["pulls"]["7"]["head_sha"]]["files"][".devflow/project.yaml"] = text


def set_task(db, **fields):
    import re
    import yaml
    body = R(db)["issues"]["3"]["body"]
    m = re.search(r"```devflow\n(.*?)\n```", body, re.S)
    t = yaml.safe_load(m.group(1))
    t.update(fields)
    R(db)["issues"]["3"]["body"] = body.replace(m.group(1), yaml.safe_dump(t, allow_unicode=True, sort_keys=False).strip())


def head_file(db, path, text):
    r = R(db)
    r["commits"][r["pulls"]["7"]["head_sha"]]["files"][path] = text


def land_and_update(db, path, text):
    """`path` changes on canonical in a new commit, and the pull request is updated onto it: a new
    head with the same change on top, green checks. Returns the new head."""
    r = R(db)
    c0 = r["branches"][build.CANONICAL]
    c1 = build.move_canonical(db, {path: text})
    old = r["commits"][r["pulls"]["7"]["head_sha"]]["files"]
    files = dict(old, **{path: text})
    new = "h9" + build.sha(json.dumps(files, sort_keys=True))[:38]
    r["commits"][new] = {"parents": [c1], "files": files, "author": "impl-bot"}
    r["pulls"]["7"].update(head_sha=new, base_sha=c1)
    r["check_runs"][new] = build.check_runs()
    assert c1 != c0
    return new


def base_files_before(db):
    """The canonical tree before its last commit — what a review written earlier stood on."""
    r = R(db)
    c = r["branches"][build.CANONICAL]
    return r["commits"][r["commits"][c]["parents"][0]]["files"]


# ================================================================================== flow
def controls_flow():
    print("flow — normal progress, and each verdict's return, wait and resume")
    s = Sim(build.scanner_pass())
    try:
        r1 = s.run()
        check("the fixture task passes on its head", (verdict(r1), phase(r1)), ("PASS", "integrate"))
        check("an agent does not merge here: it waits for the merge owner",
              (r1["integrate"].get("ready"), r1["integrate"].get("merged"), r1["state"]["wait"]["reason"]),
              ("yes", "no", "merge"))
        check("the shared Plan lock is taken by exactly this head",
              R(s.db)["refs"].get("refs/devflow/lock/collection-plan"), R(s.db)["pulls"]["7"]["head_sha"])
        check("the verdict is on the head commit as a status",
              [x["state"] for x in R(s.db)["statuses"][R(s.db)["pulls"]["7"]["head_sha"]]], ["success"])
        check("the terminal execution is OK and publication succeeded",
              (r1["state"]["execution"]["status"], r1["publish"].get("published")), ("OK", "yes"))
        # a person merges; CI reports on the merge commit; the next run resumes from GitHub alone
        db = s.db
        from remote import FixtureRemote
        s.save(db)
        FixtureRemote(s.path, build.REPO).merge(7, R(db)["pulls"]["7"]["head_sha"], "merge")
        r2 = s.run()
        check("after the merge the next run verifies the merge commit and ends done",
              (verdict(r2), phase(r2), r2["state"]["progress"]), ("PASS", "done", "stopped"))
        check("and the commit it says to wait on is the merge commit, not the pull request's head",
              r2["adjudicate"].get("watch"), R(s.db)["pulls"]["7"]["merge_commit_sha"])
        check("and releases the lock this change held", R(s.db)["refs"].get("refs/devflow/lock/collection-plan"), None)
        check("one state comment for the task, edited in place",
              len([c for c in R(s.db)["issues"]["3"]["comments"] if "devflow:state" in c["body"]]), 1)
    finally:
        s.close()

    # merged, but CI has not reported on the merge commit yet → wait, not done
    s = Sim(build.scanner_pass())
    try:
        s.run()
        s.edit(lambda db: R(db).__setitem__("post_merge_check_runs", []))
        from remote import FixtureRemote
        FixtureRemote(s.path, build.REPO).merge(7, R(s.db)["pulls"]["7"]["head_sha"], "merge")
        r = s.run()
        check("merged without post-merge results: integrate, waiting on verification",
              (phase(r), r["state"]["progress"], r["state"]["wait"]["reason"]), ("integrate", "waiting", "verification"))
        s.edit(lambda db: R(db)["check_runs"].__setitem__(R(db)["pulls"]["7"]["merge_commit_sha"],
                                                         build.check_runs(["scanner-quick", "scanner-publish"], "success")
                                                         + build.check_runs(["devflow-policy"], "failure")))
        r = s.run()
        check("a post-merge FAIL is a decision (fix forward or revert), not done",
              (verdict(r), phase(r)), ("NEEDS_DECISION", "integrate"))
    finally:
        s.close()

    # REQUEST_CHANGES → implement → a new head → back to PASS
    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: set_runs(db, R(db)["pulls"]["7"]["head_sha"],
                                   build.check_runs(["scanner-quick", "devflow-policy", "plan-ops", "distro-smoke"])
                                   + build.check_runs(["scanner-publish"], "failure")))
        r = s.run()
        check("a required check FAIL on the head: REQUEST_CHANGES, back to implement",
              (verdict(r), phase(r), r["state"]["wait"]["reason"]), ("REQUEST_CHANGES", "implement", "implementation"))
        check("REQUEST_CHANGES is not integrated", "integrate" in r, False)
        check("and the status on that head says failure",
              R(s.db)["statuses"][R(s.db)["pulls"]["7"]["head_sha"]][-1]["state"], "failure")
        new = s.edit(lambda db: build.push_new_head(db))
        s.edit(lambda db: (set_runs(db, new, build.check_runs()), add_review(db, new)))
        r = s.run()
        check("the fix is pushed, checked and reviewed on the new head: PASS again", verdict(r), "PASS")
    finally:
        s.close()

    # CONTRACT_GAP from the trace, NEEDS_DECISION from a provisional mapping and from a hold label
    for name, change, want in (
        ("a state the contract cannot express (GAP) is CONTRACT_GAP, in preparation",
         lambda db: head_file(db, ".devflow/tasks/T-0054.yaml", build.TRACE.replace("target_absent: NA", "target_absent: GAP-absence-meaning")),
         ("CONTRACT_GAP", "prepare")),
        ("a provisional mapping is NEEDS_DECISION — it is not a criterion",
         lambda db: head_file(db, ".devflow/tasks/T-0054.yaml", build.TRACE.replace("status: confirmed", "status: provisional")),
         ("NEEDS_DECISION", "prepare")),
        ("the project's needs-human label on the issue is NEEDS_DECISION",
         lambda db: R(db)["issues"]["3"]["labels"].append("needs-human"), ("NEEDS_DECISION", "prepare")),
        ("proposed-tech-lead holds the task without a verdict",
         lambda db: R(db)["issues"]["3"]["labels"].append("proposed-tech-lead"), (None, "prepare")),
        ("a collection failure mapped to FAIL is REQUEST_CHANGES",
         lambda db: head_file(db, ".devflow/tasks/T-0054.yaml", build.TRACE.replace("collection_failed: UNKNOWN", "collection_failed: FAIL")),
         ("REQUEST_CHANGES", "implement")),
        ("an expected state that is not a scanner state is REQUEST_CHANGES",
         lambda db: head_file(db, ".devflow/tasks/T-0054.yaml", build.TRACE.replace("expected: REVIEW", "expected: 취약")),
         ("REQUEST_CHANGES", "implement")),
    ):
        s = Sim(build.scanner_pass())
        try:
            s.edit(change)
            r = s.run()
            check(name, (verdict(r), phase(r)), want)
        finally:
            s.close()

    # no pull request yet → implement, waiting for the implementer, no verdict
    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: set_task(db, pr=None))
        r = s.run()
        check("no pull request yet: implement, waiting, no verdict",
              (phase(r), r["state"]["progress"], verdict(r)), ("implement", "waiting", None))
    finally:
        s.close()

    # a person's change has no automatic rounds to count: a second failing head is theirs to fix
    fail_publish = build.check_runs(["scanner-quick", "devflow-policy", "plan-ops", "distro-smoke"]) + build.check_runs(["scanner-publish"], "failure")
    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: set_task(db, implement="human"))
        s.edit(lambda db: set_runs(db, R(db)["pulls"]["7"]["head_sha"], fail_publish))
        s.run()
        nh = s.edit(lambda db: build.push_new_head(db))
        s.edit(lambda db: set_runs(db, nh, fail_publish))
        rh = s.run()
        check("a person's change failing again: REQUEST_CHANGES for them to fix, not devflow's rework budget (measured on #24)",
              (verdict(rh), rh["state"]["wait"]["reason"]), ("REQUEST_CHANGES", "implementation"))
    finally:
        s.close()

    # the rework limit (OPERATING_POLICY: one automatic rework) bounds devflow's own implementer:
    # it escalates instead of looping
    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: (edit_policy(db, lambda p: p.setdefault("implement", {}).__setitem__("by", "agent")),
                           set_task(db, implement="agent")))
        s.edit(lambda db: set_runs(db, R(db)["pulls"]["7"]["head_sha"], fail_publish))
        r1 = s.run()
        new = s.edit(lambda db: build.push_new_head(db))
        s.edit(lambda db: set_runs(db, new, fail_publish))
        r2 = s.run()
        check("the first REQUEST_CHANGES returns to implement", verdict(r1), "REQUEST_CHANGES")
        check("the same failure again after the one allowed rework: the verdict stands, the wait is the budget, no further loop",
              (verdict(r2), r2["state"]["wait"]["reason"]), ("REQUEST_CHANGES", "budget"))
        r2b = s.run()
        check("judging that head again changes nothing: no rework counted, no no-progress escalation",
              (verdict(r2b), r2b["state"]["wait"]["reason"], r2b["state"]["history"].get("rework")),
              ("REQUEST_CHANGES", "budget", r2["state"]["history"].get("rework")))
        check("and the wait names how a person grants one more round",
              f"/devflow approve rework {new[:12]}" in r2["state"]["wait"]["need"], True)
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append(
            {"id": 91, "user": "impl-bot", "body": f"/devflow approve rework {new[:12]}"}))
        r3 = s.run()
        check("a grant from an implementer is not counted: still waiting on the budget", r3["state"]["wait"]["reason"], "budget")
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append(
            {"id": 92, "user": "astro3141", "body": f"/devflow approve rework {new[:12]}"}))
        r4 = s.run()
        check("an approver grants one more round for that head: REQUEST_CHANGES, back to implement",
              (verdict(r4), phase(r4)), ("REQUEST_CHANGES", "implement"))
        newer = s.edit(lambda db: build.push_new_head(db, {"items/kisa/U-54.yaml": "id: U-54\n# v3\n"}))
        s.edit(lambda db: set_runs(db, newer, fail_publish))
        r5 = s.run()
        check("the grant is spent by that round: the next failing head waits on the budget again",
              (verdict(r5), r5["state"]["wait"]["reason"]), ("REQUEST_CHANGES", "budget"))
    finally:
        s.close()


# ================================================================================== block
def controls_block():
    print("")
    print("block — a head without complete, passing evidence is never integrated")
    for name, change, want_verdict in (
        ("checks still pending on the head", lambda db: set_runs(db, R(db)["pulls"]["7"]["head_sha"],
                                                                  build.check_runs(["scanner-quick", "devflow-policy", "plan-ops", "distro-smoke"])), None),
        ("a required check skipped (NOT_RUN) on the head", lambda db: set_runs(db, R(db)["pulls"]["7"]["head_sha"],
                                                                               build.check_runs(["scanner-quick", "devflow-policy", "plan-ops", "distro-smoke"])
                                                                               + build.check_runs(["scanner-publish"], "skipped")), None),
        ("no review at all", lambda db: R(db)["reviews"].__setitem__("7", []), None),
        ("a review that did not answer a lens's questions (unverified, not failed)",
         lambda db: (R(db)["reviews"].__setitem__("7", []),
                     add_review(db, R(db)["pulls"]["7"]["head_sha"],
                                unverified={"criteria-trace": ["the PDF page could not be opened"]})), None),
    ):
        s = Sim(build.scanner_pass())
        try:
            s.edit(change)
            r = s.run()
            check(f"{name}: no verdict, not integrated", (verdict(r), "integrate" in r), (want_verdict, False))
        finally:
            s.close()

    # a review that says it could not verify something creates no defect
    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: (R(db)["reviews"].__setitem__("7", []),
                           add_review(db, R(db)["pulls"]["7"]["head_sha"],
                                      unverified={"criteria-trace": ["p.131 not readable"]})))
        r = s.run()
        check("an unverified lens holds PASS and invents no blocking finding",
              (verdict(r), r["state"]["reviews"]["blocking"], sorted(r["state"]["reviews"]["unverified"])),
              (None, [], ["criteria-trace"]))
    finally:
        s.close()

    # a lens answered with its limits stated is complete: honesty about reach is not a gap
    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: (R(db)["reviews"].__setitem__("7", []),
                           add_review(db, R(db)["pulls"]["7"]["head_sha"],
                                      limits={"shared-artifact": ["relied on plan-ops PASS in checks.json"]})))
        r = s.run()
        check("stated limits (what an answer relied on) do not hold PASS",
              (verdict(r), r["state"]["reviews"]["unverified"]), ("PASS", {}))
    finally:
        s.close()

    # an approved exception satisfies a NOT_RUN check on that head only, and never a FAIL
    s = Sim(build.scanner_pass())
    try:
        head = R(s.db)["pulls"]["7"]["head_sha"]
        s.edit(lambda db: set_runs(db, head, build.check_runs(["scanner-quick", "devflow-policy", "plan-ops", "distro-smoke"])
                                   + build.check_runs(["scanner-publish"], "skipped")))
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append(
            {"id": 9, "user": "astro3141", "body": f"/devflow except scanner-publish {head[:12]} runner outage; local build_submission.py log attached"}))
        r = s.run()
        check("an approver's exception for the skipped check on this head satisfies it",
              (verdict(r), r["state"]["checks"]["results"]["scanner-publish"]["result"]), ("PASS", "NOT_RUN"))
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append(
            {"id": 10, "user": "impl-bot", "body": f"/devflow except plan-ops {head[:12]} trust me"}))
        s.edit(lambda db: set_runs(db, head, build.check_runs(["scanner-quick", "devflow-policy", "scanner-publish", "distro-smoke"])
                                   + build.check_runs(["plan-ops"], "skipped")))
        r = s.run()
        check("an implementer cannot except their own check", verdict(r), None)
        s.edit(lambda db: set_runs(db, head, build.check_runs(["scanner-quick", "devflow-policy", "plan-ops", "distro-smoke"])
                                   + build.check_runs(["scanner-publish"], "failure")))
        r = s.run()
        check("and no exception turns a FAIL into anything but REQUEST_CHANGES", verdict(r), "REQUEST_CHANGES")
    finally:
        s.close()


# ================================================================================== stale
def controls_stale():
    print("")
    print("stale — a verdict is about one head, one policy, one set of criteria")
    s = Sim(build.scanner_pass())
    try:
        r1 = s.run()
        new = s.edit(lambda db: build.push_new_head(db))
        s.edit(lambda db: set_runs(db, new, build.check_runs()))
        r2 = s.run()
        check("a new head after PASS: the review of the old head does not count",
              (verdict(r2), r2["state"]["reviews"]["missing"]), (None, build.LENSES_PASS))
        check("and the new head is not integrated", "integrate" in r2, False)
        check("the frame changed with the head", r1["state"]["frame"]["id"] != r2["state"]["frame"]["id"], True)
        # a review that claims the new head in its text but GitHub attaches to the old commit
        s.edit(lambda db: R(db)["reviews"]["7"].append(
            {"id": 300, "user": "rev-person", "commit_id": R(db)["reviews"]["7"][0]["commit_id"],
             "state": "COMMENTED", "body": build.review_body(new, build.LENSES_PASS)}))
        r3 = s.run()
        check("a review whose text names the new head but GitHub puts on the old one does not count",
              [x["why"] for x in r3["state"]["review_results"] if x["source"] == "github-review:300"],
              [["GitHub says this review is on " + R(s.db)["reviews"]["7"][0]["commit_id"][:12]]])
        s.edit(lambda db: add_review(db, new))
        r4 = s.run()
        check("the same review attached to the new head does count: PASS", verdict(r4), "PASS")
    finally:
        s.close()

    # head moves between the verdict and the merge step: integrate re-checks and stops
    s = Sim(build.scanner_pass())
    try:
        rid = "ctl-" + secrets.token_hex(4)
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": rid}
        step("evaluate.py", ["scanner-fixture", "3", s.name, "external"], env)
        step("adjudicate.py", [""], env)
        s.edit(lambda db: build.push_new_head(db))
        out = step("integrate.py", ["scanner-fixture"], env)
        st = json.load(open(os.path.join(WS_ROOT, rid, "devflow", "state.json")))
        check("the head moved after the verdict: integrate refuses and the verdict is withdrawn",
              (out.get("ready"), st["verdict"], st["phase"]), ("no", None, "verify_review"))
        check("and takes no lock for a head nobody judged",
              R(s.db)["refs"].get("refs/devflow/lock/collection-plan"), None)
        s.runs.append(os.path.join(WS_ROOT, rid))
    finally:
        s.close()

    # A pinned criterion or contract lands on canonical and the change is updated onto it: the diff
    # is the same, the head is new-but-equivalent, and only what the judgement rests on moved. Each
    # case on its own task, so one case's staleness is not another's.
    for path, text, what in (
            ("docs/baselines/current/주요정보통신기반시설 기술적 취약점 분석·평가 방법 상세가이드.pdf",
             "PDF-KISA-2026-REVISED", "an official criterion"),
            ("docs/adr/existing-primitive-semantic-parity.md", "# ADR parity, amended\n", "an ADR"),
            ("evaluator/applicability_registry.py", "APPLICABILITY_REGISTRY = {'x': 1}\n", "a TD §5.7 registry")):
        s = Sim(build.scanner_pass())
        try:
            r1 = s.run()
            head = s.edit(lambda db, path=path, text=text: land_and_update(db, path, text))
            s.edit(lambda db, head=head: R(db)["reviews"]["7"].append(
                {"id": 500, "user": "rev-person", "commit_id": head, "state": "COMMENTED",
                 "body": build.review_body(head, build.LENSES_PASS, digest=canonical_digest(db),
                                           basis=build.basis_of(base_files_before(db)))}))
            r2 = s.run()
            check(f"{what} amended on canonical: the frame changes",
                  r1["state"]["frame"]["id"] != r2["state"]["frame"]["id"], True)
            check(f"{what} amended: a review of the same diff on the old basis does not count",
                  (verdict(r2), [x["why"] for x in r2["state"]["review_results"] if x["source"] == "github-review:500"][0][0].startswith("written against other criteria or contracts")),
                  (None, True))
            s.edit(lambda db, head=head: add_review(db, head))
            check(f"{what} amended: a review on the new basis does", verdict(s.run()), "PASS")
        finally:
            s.close()

    # a policy change on canonical: reviews name the policy digest they were written under
    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: edit_policy(db, lambda p: p["loop"].__setitem__("max_no_progress", 2)))
        r3 = s.run()
        check("the policy changing on canonical invalidates reviews written under the old one",
              (verdict(r3), sorted({w for x in r3["state"]["review_results"] for w in (x["why"] or [])})),
              (None, ["written under another policy version"]))
    finally:
        s.close()


# ================================================================================== foundation
def foundation_pr_db(review_user="rev-person", review_kind=None):
    """A Foundation in a pull request — possible in devflow generally; the scanner itself publishes
    Foundations as a transaction, so this variant removes that flow from the fixture's policy."""
    db = build.scanner_pass()
    edit_policy(db, lambda p: p.pop("flows", None))
    set_task(db, change_class="FOUNDATION", scope_files=["collector/lib/core.sh", ".devflow/tasks/T-0054.yaml"],
             impacts=["contract_changed"],
             implementer={"identities": ["impl-bot"], "principals": ["impl-principal"]})
    r = R(db)
    head = r["pulls"]["7"]["head_sha"]
    files = r["commits"][head]["files"]
    for k in [k for k in files if k not in r["commits"][r["branches"][build.CANONICAL]]["files"]
              and k != ".devflow/tasks/T-0054.yaml"]:
        files.pop(k)
    files["collection-plan.tsv"] = files["dist/collection-plan.tsv"] = build.plan(build.OPS)
    files["collector/lib/core.sh"] = "stat -L -c\n"
    files[".devflow/tasks/T-0054.yaml"] = build.TRACE.split("expected_plan_ops")[0]
    r["reviews"]["7"] = []
    return db


def approve(db, gate, value, user="astro3141"):
    R(db)["issues"]["3"]["comments"].append({"id": 50 + len(R(db)["issues"]["3"]["comments"]), "user": user,
                                             "body": f"/devflow approve {gate} {value}"})


def controls_foundation():
    print("")
    print("foundation — an independent review is a separate identity, bound to this head")
    s = Sim(foundation_pr_db())
    try:
        r = s.run()
        check("a Foundation without gate 1 waits for the human approval of its scope",
              (phase(r), verdict(r), r["state"]["wait"]["reason"]), ("prepare", None, "decision"))
        task_digest = json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json")))["task_digest"]
        s.edit(lambda db: approve(db, "gate1", task_digest, user="impl-bot"))
        r = s.run()
        check("an implementer's own approval of gate 1 does not count", phase(r), "prepare")
        s.edit(lambda db: approve(db, "gate1", task_digest))
        head = R(s.db)["pulls"]["7"]["head_sha"]
        lenses = json.load(open(os.path.join(s.run()["ws"], "devflow", "evaluation.json")))["reviews"]["required"]
        s.edit(lambda db: add_review(db, head, lenses, user="impl-bot"))
        r = s.run()
        check("gate 1 approved; the only review is the implementer's: independent review missing",
              (verdict(r), r["state"]["reviews"]["independent_ok"]), (None, False))
        # same principal through another vendor: still one reviewer
        body = build.review_body(head, lenses, digest=canonical_digest(s.db), basis=canonical_basis(s.db))
        doc = json.loads(body.split("```devflow-review\n")[1].split("\n```")[0])
        doc["reviewer"] = {"kind": "model", "principal": "impl-principal", "provider": "codex", "fresh_context": True}
        s.edit(lambda db: R(db)["reviews"]["7"].append({"id": 400, "user": "model-runner", "commit_id": head, "state": "COMMENTED",
                                                       "body": "```devflow-review\n" + json.dumps(doc) + "\n```"}))
        r = s.run()
        check("a different model under the implementer's principal is not independent",
              r["state"]["reviews"]["independent_ok"], False)
        s.edit(lambda db: add_review(db, head, lenses, user="rev-person"))
        r = s.run()
        check("a separate person's complete review on this head: independence holds",
              r["state"]["reviews"]["independent_ok"], True)
        check("and gate 2 (the exact commit) is still the person's: PASS, waiting for approval, not integrated",
              (verdict(r), r["state"]["wait"]["need"].startswith("/devflow approve gate2"), "integrate" in r),
              ("PASS", True, False))
        s.edit(lambda db: approve(db, "gate2", head[:12]))
        r = s.run()
        check("gate 2 approved for this head: ready for the merge owner", (verdict(r), r["integrate"].get("ready")), ("PASS", "yes"))
        new = s.edit(lambda db: build.push_new_head(db, {"collector/lib/core.sh": "stat -L -c # v2\n"}))
        s.edit(lambda db: set_runs(db, new, build.check_runs()))
        r = s.run()
        check("a new head: the independent review and gate 2 of the old head do not carry over",
              (verdict(r), r["state"]["reviews"]["independent_ok"]), (None, False))
    finally:
        s.close()

    # declared ORDINARY but the change touches a semantic owner: never silently accepted
    db = build.scanner_pass()
    head_file(db, "collector/lib/core.sh", "stat -L -c\n")
    s = Sim(db)
    try:
        r = s.run()
        check("an ordinary change touching collector/ is REQUEST_CHANGES (revert, or prepare a Foundation)",
              verdict(r), "REQUEST_CHANGES")
    finally:
        s.close()

    # the scanner's own flow: Foundation is published as A+B by a person; the record evidences it
    db = build.scanner_pass()
    set_task(db, change_class="FOUNDATION", scope_files=["collector/lib/core.sh"], pr=None)
    s = Sim(db)
    try:
        r = s.run()
        task_digest = json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json")))["task_digest"]
        s.edit(lambda db: approve(db, "gate1", task_digest))
        r = s.run()
        check("a transaction Foundation waits for the person's A+B publication, not for a PR",
              (phase(r), r["state"]["wait"]["owner"]), ("implement", "human operator"))

        def publish_ab(db, record):
            rr = R(db)
            c = rr["branches"][build.CANONICAL]
            files = dict(rr["commits"][c]["files"])
            files["collector/lib/core.sh"] = "stat -L -c\n"
            rr["commits"]["a" * 40] = {"parents": [c], "files": files, "author": "impl-bot"}
            files2 = dict(files)
            files2[".issue-orchestrator/rebaseline/3-" + "a" * 40 + ".yaml"] = record
            rr["commits"]["b" * 40] = {"parents": ["a" * 40], "files": files2, "author": "astro3141"}
            rr["branches"][build.CANONICAL] = "b" * 40
            rr["check_runs"]["b" * 40] = build.check_runs(["scanner-quick", "scanner-publish", "devflow-policy", "distro-smoke"])
        good = ("foundation_commit: " + "a" * 40 + "\nfoundation_actor: impl-bot\nhuman_approval: {approver: astro3141}\n"
                "validation_evidence: {validated_sha: " + "a" * 40 + ", exit_code: 0}\n"
                "independent_review: {reviewer: rev-person, session_id: s-1, reviewed_sha: " + "a" * 40 + ", blocking_findings: []}\n")
        s.edit(lambda db: publish_ab(db, good.replace("reviewer: rev-person", "reviewer: impl-bot")))
        s.edit(lambda db: set_task(db, transaction_commit="a" * 40))
        r = s.run()
        check("published, but the record's reviewer is the actor: not done, a decision",
              (verdict(r), phase(r)), ("NEEDS_DECISION", "integrate"))
        s.edit(lambda db: R(db)["commits"]["b" * 40]["files"].__setitem__(
            ".issue-orchestrator/rebaseline/3-" + "a" * 40 + ".yaml", good))
        r = s.run()
        check("published with a record binding an independent review and validation to A: done",
              (verdict(r), phase(r)), ("PASS", "done"))
    finally:
        s.close()


# ================================================================================== evidence
def model_receipt(ws, members=None, context=None, write=True, stale=False, tamper=False, frame_from=None, policy_digest=None):
    """What steps/tasks.py writes for a fan-out: the review file and a receipt naming it. Without
    `members`, one member per model assignment evaluate made — whatever the policy's grouping."""
    ev = json.load(open(os.path.join(ws, "devflow", "evaluation.json")))
    frame = ev["frame"]
    if members is None:
        members = {a["reviewer"]: a["lenses"] for a in ev.get("assignments") or [] if a.get("by") == "model"}
    rc = {"context": context or frame["id"], "members": {}}
    for label, lenses in members.items():
        name = f"review_{label}.json"
        body = build.review_body(frame["head_sha"], lenses, basis=frame["basis"], **({"digest": policy_digest} if policy_digest else {}))
        doc = json.loads(body.split("```devflow-review\n")[1].split("\n```")[0])
        doc["frame_id"] = frame["id"]
        text = json.dumps(doc)
        path = os.path.join(ws, name)
        if write:
            open(path, "w").write(text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if tamper:
            open(path, "w").write(text.replace('"PASS"', '"PASS" '))
        rc["members"][label] = {"artifact": name, "produced": write and not stale, "sha256": digest if write else "",
                                "attempts": 1, "result": {"status": "COMPLETED" if write else "FAILED",
                                                          "principal": "devflow-reviewer", "provider": "claude",
                                                          "run_id": f"{os.path.basename(ws)}-{label}-claude",
                                                          "produced_stale": stale}}
    p = os.path.join(ws, "devflow", "review_receipt.json")
    json.dump(rc, open(p, "w"))
    return p


def controls_evidence():
    print("")
    print("evidence — only what this attempt produced, for this frame, counts")
    base = build.scanner_pass()
    R(base)["reviews"]["7"] = []
    for name, kw, want in (
        ("a model review this attempt produced, hash intact, for this frame", {}, "PASS"),
        ("a review file an earlier attempt left (produced_stale) is not evidence", {"stale": True}, None),
        ("a failed member's missing output is not evidence", {"write": False}, None),
        ("a review file changed after the receipt recorded it is not evidence", {"tamper": True}, None),
        ("a receipt for another frame is not evidence", {"context": "0000000000000000"}, None),
    ):
        s = Sim(copy.deepcopy(base))
        try:
            r = s.run(review_mode="model",
                      receipt=lambda ws, kw=kw: model_receipt(ws, **kw))
            check(name, verdict(r), want)
        finally:
            s.close()

    s = Sim(copy.deepcopy(base))
    try:
        r = s.run(review_mode="model", receipt=lambda ws: model_receipt(ws))
        acc = r["state"].get("accepted_reviews") or []
        ev0 = json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json")))
        check("per_lens: one model reviewer per lens, each with only its own lens",
              sorted((a["lenses"][0], len(a["lenses"])) for a in ev0["assignments"]),
              sorted((l, 1) for l in build.LENSES_PASS))
        check("each accepted model review is published with its frame",
              (len(acc), {a["frame_id"] for a in acc} == {ev0["frame"]["id"]}), (len(build.LENSES_PASS), True))
        r2 = s.run(review_mode="model")
        check("the next run on the same frame reuses it without a model call",
              (verdict(r2), r2["evaluate"].get("need_models")), ("PASS", "no"))
        new = s.edit(lambda db: build.push_new_head(db))
        s.edit(lambda db: set_runs(db, new, build.check_runs()))
        r3 = s.run(review_mode="model")
        check("a new head asks for a new model review instead of reusing the old one",
              r3["evaluate"].get("need_models"), "yes")
        rw = r3["evaluate"]
        ev = json.load(open(os.path.join(r3["ws"], "devflow", "evaluation.json")))
        check("its prompt is frozen with the new head and names only the materials",
              (os.path.isfile(ev["assignments"][0]["prompt"]), ev["frame"]["head_sha"] in open(ev["assignments"][0]["prompt"]).read()),
              (True, True))
        mat = json.load(open(os.path.join(r3["ws"], "devflow", "materials", "checks.json")))
        check("the reviewer is given the check results of exactly that head",
              (mat["head_sha"], sorted(mat["results"])), (new, sorted(build.CHECKS)))
    finally:
        s.close()

    # the model review budget: when spent, reviews become external, never silently skipped
    s = Sim(copy.deepcopy(base))
    try:
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append(
            {"id": 77, "user": "devflow", "app": "devflow-app", "body": '<!-- devflow:state {"history":{"model_calls":14}} -->'}))
        r = s.run(review_mode="model")
        ev = json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json")))
        check("the model budget spent: no model review, the assignment waits for a person",
              (r["evaluate"].get("need_models"), ev["assignments"][0]["by"], verdict(r)), ("no", "external", None))
        head = R(s.db)["pulls"]["7"]["head_sha"]
        check("and the wait names the grant", f"/devflow approve reviews {head[:12]}" in r["state"]["wait"]["need"], True)
        check("the wait is the budget, told apart from reviews simply not done yet", r["state"]["wait"]["reason"], "budget")
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append(
            {"id": 78, "user": "impl-bot", "body": f"/devflow approve reviews {head[:12]}"}))
        r = s.run(review_mode="model")
        check("a grant from an implementer is not counted", r["evaluate"].get("need_models"), "no")
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append(
            {"id": 79, "user": "astro3141", "body": f"/devflow approve reviews {head[:12]}"}))
        r = s.run(review_mode="model")
        check("an approver's grant for this head: one more model round, past the budget",
              r["evaluate"].get("need_models"), "yes")
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append(
            {"id": 80, "user": "devflow", "app": "devflow-app", "body": '<!-- devflow:state {"history":{"model_calls":16,'
             f'"review_grants_spent":["{head[:12]}"]}}}} -->'}))
        r = s.run(review_mode="model")
        check("once that round ran, the grant is spent: the budget holds again", r["evaluate"].get("need_models"), "no")
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append(
            {"id": 81, "user": "astro3141", "body": f"/devflow approve reviews {head[:12]}"}))
        r = s.run(review_mode="model")
        check("another comment for the same head is another round (its reviews had to be taken again)",
              r["evaluate"].get("need_models"), "yes")
    finally:
        s.close()

    # the state devflow reads back is its own: a marker comment anyone else writes carries nothing
    # (agent-stack docs/packages.md: a reading nobody authenticated is not a reading). Measured
    # before this: the control above, with its comment by any user, reset the budget.
    s = Sim(copy.deepcopy(base))
    try:
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append(
            {"id": 77, "user": "devflow", "app": "devflow-app", "body": '<!-- devflow:state {"history":{"model_calls":14}} -->'}))
        forged = '<!-- devflow:state {"history":{"model_calls":0,"pushed":[]},"ops":{}} -->'
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append({"id": 90, "user": "mallory", "body": forged}))
        r = s.run(review_mode="model")
        ev = json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json")))
        check("a marker comment by another author is ignored: the spent budget stays spent",
              (r["evaluate"].get("need_models"), ev["history"].get("model_calls")), ("no", 14))
        check("and the issue says a marker it did not trust was there",
              any("not written by devflow ignored: [90]" in n for n in r["state"].get("notes") or []), True)
        s.edit(lambda db: R(db)["issues"]["3"]["comments"].append({"id": 91, "user": "devflow", "body": forged}))
        r = s.run(review_mode="model")
        check("one by devflow's own account but not through its App (written by hand) is ignored too",
              (r["evaluate"].get("need_models"),
               json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json")))["history"].get("model_calls")),
              ("no", 14))
        comments = R(s.db)["issues"]["3"]["comments"]
        check("and publishing edits devflow's own comment, never the forged ones",
              ([c["body"] for c in comments if c["id"] in (90, 91)],
               "**devflow**" in next(c["body"] for c in comments if c["id"] == 77)),
              ([forged, forged], True))
    finally:
        s.close()

    # what a pass spent is published even when the verdict did not change: the key that says
    # "already written" covers the history and the reviews taken, not only the verdict (#24: a pass
    # that added one judge's review on the same head left its call out of the budget)
    s = Sim(copy.deepcopy(base))
    try:
        r = s.run(review_mode="model")
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": os.path.basename(r["ws"])}
        again = step("publish.py", ["scanner-fixture"], env)
        check("the same state published again is not written again", "already says this" in (again.get("skipped") or ""), True)
        sp = os.path.join(r["ws"], "devflow", "state.json")
        st = json.load(open(sp, encoding="utf-8"))
        st["history"]["model_calls"] = int(st["history"].get("model_calls") or 0) + 1
        st["history"]["review_rounds"] = int(st["history"].get("review_rounds") or 0) + 1
        json.dump(st, open(sp, "w", encoding="utf-8"))
        more = step("publish.py", ["scanner-fixture"], env)
        body = next(c["body"] for c in R(s.db)["issues"]["3"]["comments"] if "devflow:state" in c["body"])
        check("same verdict, one more model call: the comment is updated and carries it",
              ("updated" in (more.get("did") or ""), f'"model_calls":{st["history"]["model_calls"]}' in body), (True, True))
        # the router held devflow's implementer (#26: an expired login): the issue says so
        held = step("publish.py", ["scanner-fixture", "no eligible provider: claude=unknown: the login's token expired"], env)
        body = next(c["body"] for c in R(s.db)["issues"]["3"]["comments"] if "devflow:state" in c["body"])
        check("the implementer held by the router: the wait is the environment's, in the router's words",
              ("updated" in (held.get("did") or ""), "waiting (environment): devflow's implementer could not start: "
               "no eligible provider: claude=unknown: the login's token expired" in body, "open a pull request" in body),
              (True, True, False))
    finally:
        s.close()


# ================================================================================== shared
def controls_shared():
    print("")
    print("shared — the collection Plan: latest canonical, expected operations, one at a time")
    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: build.move_canonical(db))
        r = s.run()
        check("canonical moved under a Plan change: back to implement to update and regenerate",
              (phase(r), verdict(r), "integrate" in r), ("implement", None, False))
        # the implementer updates onto canonical and regenerates: a new head, new checks, new reviews
        def rebase(db):
            rr = R(db)
            c = rr["branches"][build.CANONICAL]
            old = rr["commits"][rr["pulls"]["7"]["head_sha"]]["files"]
            files = dict(rr["commits"][c]["files"])
            for k in ("items/kisa/U-54.yaml", "bindings/linux/u54.yaml", "collection-plan.tsv", "dist/collection-plan.tsv",
                      "tests/test_u54_ftp.py", ".devflow/tasks/T-0054.yaml"):
                files[k] = old[k]
            new = "h3" + "0" * 38
            rr["commits"][new] = {"parents": [c], "files": files, "author": "impl-bot"}
            rr["pulls"]["7"]["head_sha"] = new
            rr["pulls"]["7"]["base_sha"] = c
            rr["check_runs"][new] = build.check_runs()
            return new
        new = s.edit(rebase)
        r = s.run()
        check("on the new canonical, the old head's review no longer counts", verdict(r), None)
        s.edit(lambda db: add_review(db, new))
        r = s.run()
        check("regenerated, checked and reviewed again on the new head: PASS", verdict(r), "PASS")
    finally:
        s.close()

    for name, change in (
        ("an operation nobody expected is REQUEST_CHANGES",
         lambda db: (head_file(db, "collection-plan.tsv", build.plan(build.OPS + [build.NEW_OP, ("op-0000000000f5", "file_lines", "1", "/etc/ftpusers", "-")])))),
        ("a count assertion moved to match the output, not the expected operations",
         lambda db: head_file(db, ".devflow/tasks/T-0054.yaml", build.TRACE.replace("from: 3, to: 4", "from: 3, to: 5"))),
        ("a Plan change with no expected operations declared",
         lambda db: head_file(db, ".devflow/tasks/T-0054.yaml", build.TRACE.split("expected_plan_ops")[0])),
    ):
        s = Sim(build.scanner_pass())
        try:
            s.edit(change)
            r = s.run()
            check(name, verdict(r), "REQUEST_CHANGES")
        finally:
            s.close()

    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: R(db)["refs"].__setitem__("refs/devflow/lock/collection-plan", "f" * 40))
        r = s.run()
        check("the Plan is being integrated by another change: this one waits its turn",
              (verdict(r), r["state"]["wait"]["reason"], R(s.db)["refs"]["refs/devflow/lock/collection-plan"]),
              ("PASS", "integration_order", "f" * 40))
    finally:
        s.close()

    # the lock was taken by an earlier head of this same change: the change still holds it
    s = Sim(build.scanner_pass())
    try:
        s.run()
        new = s.edit(lambda db: build.push_new_head(db))
        s.edit(lambda db: (set_runs(db, new, build.check_runs()), add_review(db, new)))
        r = s.run()
        check("a new, passing head of the same change takes over the lock its earlier head held",
              (verdict(r), R(s.db)["refs"]["refs/devflow/lock/collection-plan"]), ("PASS", new))
    finally:
        s.close()

    # canonical moves between the verdict and the merge step
    s = Sim(build.scanner_pass())
    try:
        rid = "ctl-" + secrets.token_hex(4)
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": rid}
        step("evaluate.py", ["scanner-fixture", "3", s.name, "external"], env)
        step("adjudicate.py", [""], env)
        s.edit(lambda db: build.move_canonical(db))
        step("integrate.py", ["scanner-fixture"], env)
        st = json.load(open(os.path.join(WS_ROOT, rid, "devflow", "state.json")))
        check("canonical moved right before the merge step: integrate sends it back to update",
              (st["phase"], st["verdict"]), ("implement", None))
        s.runs.append(os.path.join(WS_ROOT, rid))
    finally:
        s.close()


# ================================================================================== effects
def controls_effects():
    print("")
    print("effects — an effect that happened, with its answer lost, is not repeated")
    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: db.__setitem__("faults", {"lose_response": ["add_comment", "create_ref", "set_status"]}))
        r1 = s.run()
        check("every write took effect and lost its answer; the step asked the remote and carried on",
              (r1["publish"].get("published"), r1["integrate"].get("ready")), ("yes", "yes"))
        r2 = s.run()
        db = s.db
        check("after a resume: still one state comment",
              len([c for c in R(db)["issues"]["3"]["comments"] if "devflow:state" in c["body"]]), 1)
        check("still one status on the head",
              len(R(db)["statuses"][R(db)["pulls"]["7"]["head_sha"]]), 1)
        check("the lock is held once, by this head", R(db)["refs"]["refs/devflow/lock/collection-plan"],
              R(db)["pulls"]["7"]["head_sha"])
    finally:
        s.close()

    # a project that lets devflow merge: the merge's answer is lost, and a resume does not merge twice
    db = build.scanner_pass()
    edit_policy(db, lambda p: p["integration"].__setitem__("merge_by", "agent"))
    R(db)["reviews"]["7"] = []
    add_review(db, R(db)["pulls"]["7"]["head_sha"])          # written under that policy
    db["faults"] = {"lose_response": ["merge"]}
    s = Sim(db)
    try:
        r1 = s.run()
        check("merge_by agent: the merge happened and its answer was lost — the step saw the PR merged",
              r1["integrate"].get("merged"), "yes")
        commits_after = len(R(s.db)["commits"])
        r2 = s.run()
        check("the resume verifies the merge commit instead of merging again",
              (len(R(s.db)["commits"]), phase(r2)), (commits_after, "done"))
    finally:
        s.close()

    # a push whose answer was lost, met again when the step is re-entered (a retry, or a resume
    # that starts the step over — agent-stack §84): this run's own commit is recognised and kept;
    # a commit someone else put there is not taken for it
    import importlib.util
    from remote import FixtureRemote
    s = Sim(build.scanner_pass())
    try:
        rem = FixtureRemote(s.path, build.REPO)
        base_sha = R(s.db)["branches"][build.CANONICAL]
        author = {"name": "devflow-implementer", "email": "x", "date": "2026-10-03T00:00:00Z"}
        mine = rem.push_commit("devflow/t-x", base_sha, {"a.txt": (b"x", "100644")},
                               "T-X: x\n\nDevflow-Task: T-X\nDevflow-Run: ctl-run-1\n", author)
        other = rem.push_commit("devflow/t-y", base_sha, {"b.txt": (b"y", "100644")},
                                "a person's commit\n\nDevflow-Run: ctl-run-1\n", dict(author, name="someone"))
        os.environ["CONDUCTOR_SELF_RUN_ID"] = "ctl-run-1"
        spec = importlib.util.spec_from_file_location("deliver_ctl", os.path.join(STEPS, "deliver.py"))
        deliver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deliver)
        check("a push this run made, whose answer was lost, is recognised as its own",
              deliver.own_push(rem, mine, {"parent": base_sha}), True)
        check("but not a commit by anyone else, nor one onto another parent",
              (deliver.own_push(rem, other, {"parent": base_sha}), deliver.own_push(rem, mine, {"parent": "f" * 40})),
              (False, False))
        os.environ["CONDUCTOR_SELF_RUN_ID"] = "ctl-run-2"
        spec.loader.exec_module(deliver)
        check("nor another run's push", deliver.own_push(rem, mine, {"parent": base_sha}), False)
    finally:
        os.environ.pop("CONDUCTOR_SELF_RUN_ID", None)
        s.close()


# ================================================================================== rules
def controls_rules():
    print("")
    print("rules — a candidate's edits to the rules do not judge the candidate")
    db = build.scanner_pass()
    import yaml
    head = R(db)["pulls"]["7"]["head_sha"]
    pol = yaml.safe_load(R(db)["commits"][head]["files"][".devflow/project.yaml"])
    pol["checks"]["by_impact"] = {}
    pol["hold_labels"] = {}
    head_file(db, ".devflow/project.yaml", yaml.safe_dump(pol, allow_unicode=True))
    s = Sim(db)
    try:
        s.edit(lambda db: set_runs(db, head, build.check_runs(["scanner-quick", "devflow-policy"])))
        r = s.run()
        check("the candidate drops scanner-publish from its own policy: still required (canonical rules)",
              "scanner-publish" in r["state"]["checks"]["results"], True)
        check("and the change is marked as a change to the rules, with the policy-change lens",
              ("policy-change" in r["state"]["reviews"]["required"]), True)
        check("so it is not a PASS on the rules it proposes", verdict(r), None)
    finally:
        s.close()

    db = build.scanner_pass()
    set_task(db, type="docs", impacts=[], items=[])
    r_ = R(db)
    head = r_["pulls"]["7"]["head_sha"]
    base = r_["commits"][r_["branches"][build.CANONICAL]]["files"]
    r_["commits"][head]["files"] = dict(base, **{"docs/NOTES.md": "notes\n"})
    r_["check_runs"][head] = build.check_runs(["scanner-quick", "devflow-policy"])
    r_["reviews"]["7"] = []
    add_review(db, head, ["base"])
    s = Sim(db)
    try:
        r = s.run()
        res = r["state"]["checks"]["results"]
        check("a docs-only change: Builder and Plan checks are NOT_APPLICABLE, with the reason",
              (res["scanner-publish"]["result"], bool(res["scanner-publish"].get("why"))), ("NOT_APPLICABLE", True))
        check("no trace is required, only the base review, and it passes", (r["state"]["reviews"]["required"], verdict(r)),
              (["base"], "PASS"))
    finally:
        s.close()


# ================================================================================== tool
def controls_tool():
    print("")
    print("tool — machinery failing is not a judgement about the work")
    # outside the stack's PYTHONPATH a step cannot know its workspace or its egress: it stops before
    # answering anything — `import step` fails, which agent-stack's contract calls the right answer
    # (a step that carried on with a guessed root would answer like one that did not)
    env = {**os.environ, "PYTHONPATH": "", "CONDUCTOR_SELF_RUN_ID": "ctl-" + secrets.token_hex(4)}
    for name, args in (("evaluate.py", ["scanner-fixture", "3", "ctl", "external"]), ("adjudicate.py", [""]),
                       ("integrate.py", ["scanner-fixture"]), ("publish.py", ["scanner-fixture"]),
                       ("review_plan.py", ["claude", "claude", "direct", "devflow-reviewer"])):
        out = step(name, args, env)
        check(f"{name} outside the stack: stops at `import step`, no answer on defaults",
              (out.get("status"), "No module named 'step'" in str(out.get("error"))), ("CRASH", True))
    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: set_runs(db, R(db)["pulls"]["7"]["head_sha"],
                                   build.check_runs(["scanner-quick", "devflow-policy", "plan-ops", "distro-smoke"])
                                   + build.check_runs(["scanner-publish"], "cancelled")))
        r = s.run()
        check("a cancelled CI job: TOOL_FAILURE, no verdict, waiting on the environment",
              (r["state"]["execution"]["status"], verdict(r), r["state"]["wait"]["reason"]),
              ("TOOL_FAILURE", None, "environment"))
        check("and it is not integrated", "integrate" in r, False)
    finally:
        s.close()
    s = Sim(build.scanner_pass())
    try:
        s.edit(lambda db: db.__setitem__("faults", {"down": True}))
        r = s.run()
        check("the remote is down: evaluate says TOOL_FAILURE and nothing is judged",
              (r["evaluate"].get("status"), r["adjudicate"].get("verdict")), ("TOOL_FAILURE", "UNDECIDED"))
    finally:
        s.close()
    db = build.scanner_pass()
    R(db)["commits"][R(db)["branches"][build.CANONICAL]]["files"][".devflow/hooks/scanner_hook.py"] = "raise SystemExit(3)\n"
    s = Sim(db)
    try:
        r = s.run()
        check("a hook that crashes: TOOL_FAILURE, no verdict",
              (r["evaluate"].get("status"), verdict(r)), ("TOOL_FAILURE", None))
    finally:
        s.close()


# ================================================================================== implement
GEN_PY = ("import os, sys\n"
          "if os.path.exists('FAILGEN'):\n"
          "    sys.exit('generator: refused')\n"
          "os.makedirs('dist', exist_ok=True)\n"
          "open('dist/collection-plan.tsv', 'w').write(open('collection-plan.tsv').read())\n")


def impl_db(by="agent", max_rounds=3, task_type=None, reviewer_models=None):
    """A task with no pull request yet, in a project that lets devflow implement — and that routes
    research and survey tasks to the research lane."""
    db = build.scanner_pass()

    def pol(p):
        p["task_types"] = list(p.get("task_types") or []) + ["research", "survey"]
        if reviewer_models:
            p.setdefault("reviews", {})["reviewer_models"] = reviewer_models
            p["reviews"]["distinct_models"] = {}
        else:
            p.setdefault("reviews", {})["distinct_models"] = {"research": 2, "survey": 2}
        if not by:
            p.pop("implement", None)
        else:
            p["implement"] = {"by": by, "max_rounds": max_rounds, "regenerate": [["python3", "tools/gen.py"]],
                              "generated": ["dist/collection-plan.tsv"],
                              "references": [{"as": "legacy", "ref": "reference/legacy", "path": "corpus/"}],
                              "modes": {"research": {"writes": ["docs/research/"], "path": "docs/research/{task}.md"},
                                        "survey": {"writes": ["docs/research/"], "path": "docs/research/{task}.md"}}}
    edit_policy(db, pol, head_too=False)
    r = R(db)
    r["commits"][r["branches"][build.CANONICAL]]["files"]["tools/gen.py"] = GEN_PY
    r["commits"][r["branches"][build.CANONICAL]]["files"]["fixtures/el8/cmd/svc_list.stdout"] = "named.service loaded\n"
    r["pulls"], r["reviews"] = {}, {}
    r["commits"]["ref0"] = {"parents": [], "author": "someone", "files": {"corpus/a.sh": "ps -ef | grep vsftpd\n"}}
    r["branches"]["reference/legacy"] = "ref0"
    set_task(db, pr=None, implementer=None)     # devflow implements; nobody else is named
    if task_type:
        set_task(db, type=task_type, change_class="ORDINARY", impacts=[], items=["U-54"])
    return db


def research_model(stage, ws, extra=None, report_it=True):
    """What the research lane writes: one record, and its report."""
    write(ws, "docs/research/t-0054.md", "# FTP daemons on the target distributions\n\n- vsftpd ... (source: packages.ubuntu.com, 2026-09-25, primary)\n"
                                         "- the generator copies the source Plan (tools/gen.py:5) into collection-plan.tsv; nothing reads docs/nowhere.py or nowhere.tsv\n"
                                         "- the capture fixtures/el8/cmd/svc_list.stdout lists named.service (docs.redhat.com, read 2026-09-26)\n")
    for path, text in (extra or {}).items():
        write(ws, path, text)
    if report_it:
        report(ws, "research", {"summary": "how FTP daemons are packaged", "topics": [], "decisions": [], "gaps": []})
    return report_it


def write(ws, path, text):
    p = os.path.join(ws, "repo", path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(text)


def report(ws, stage, doc):
    json.dump(doc, open(os.path.join(ws, "devflow", f"{stage}.json"), "w", encoding="utf-8"))


def good_model(stage, ws, extra=None):
    """What a correct implementer writes for the fixture's U-54 task."""
    if stage == "prepare":
        write(ws, ".devflow/tasks/T-0054.yaml", build.TRACE)
        write(ws, ".devflow/tasks/T-0054.research.md", "- vsftpd is the FTP daemon (primary: distro docs)\n")
        report(ws, "prepare", {"proceed": True, "summary": "U-54 on service_state", "acceptance": ["REVIEW when active"],
                               "plan": ["items/kisa/U-54.yaml"], "decisions": [], "gaps": []})
        return True
    write(ws, "items/kisa/U-54.yaml", "id: U-54\nbaseline_status: confirmed\n")
    write(ws, "bindings/linux/u54.yaml", "produces: [service.state]\n")
    write(ws, "collection-plan.tsv", build.plan(build.OPS + [build.NEW_OP]))
    write(ws, "tests/test_u54_ftp.py", "assert len(ops) == 4\n")
    for path, text in (extra or {}).items():
        write(ws, path, text)
    report(ws, "implement", {"summary": "U-54", "changed": ["items/kisa/U-54.yaml — the item"], "tests": [],
                             "not_done": [], "disputed": []})
    return True


def branch_of(db):
    return R(db)["branches"].get("devflow/t-0054")


def controls_implement():
    print("\nimplement — devflow prepares and implements; the canonical rules decide what may be pushed")
    import yaml
    s = Sim(impl_db(by=None))
    try:
        r = s.run_pass(good_model)
        check("a project that does not let devflow implement: nothing is acted on, the task waits for a person",
              (r["act"].get("do"), phase(r), r["state"]["wait"]["reason"]), ("none", "implement", "implementation"))
    finally:
        s.close()

    s = Sim(impl_db())
    try:
        r = s.run_pass(good_model)
        db = s.db
        head = branch_of(db)
        prn = str(r["state"]["history"].get("pr"))
        pr = R(db)["pulls"].get(prn) or {}
        files = R(db)["commits"].get(head, {}).get("files", {})
        check("no implementation yet: devflow prepares, implements, and pushes one commit on devflow/<task>",
              (r["act"].get("do"), r["deliver"].get("delivered"), bool(head)), ("prepare", "yes", True))
        check("the commit is devflow-implementer's, on top of canonical",
              (R(db)["commits"][head]["author"], R(db)["commits"][head]["parents"]),
              ("devflow-implementer", [R(db)["branches"][build.CANONICAL]]))
        check("the project's generator ran: the generated file follows the source it is generated from",
              files.get("dist/collection-plan.tsv") == files.get("collection-plan.tsv") == build.plan(build.OPS + [build.NEW_OP]), True)
        check("the trace and the research record are in the change",
              (".devflow/tasks/T-0054.yaml" in files, ".devflow/tasks/T-0054.research.md" in files), (True, True))
        check("a pull request is opened for the branch, marked as devflow's",
              (pr.get("head_ref"), dfcore_mark() in (pr.get("body") or "")), ("devflow/t-0054", True))
        check("the published state waits for CI on the new head, with no verdict",
              (phase(r), verdict(r), r["state"]["wait"]["reason"], r["state"]["wait"]["owner"]),
              ("verify_review", None, "verification", "CI"))
        check("no verdict status is put on a head nobody judged", R(db).get("statuses", {}).get(head), None)

        r2 = s.run_pass(good_model, review_mode="model")
        check("the next pass finds devflow's pull request from the published state",
              (r2["state"].get("pr") or {}).get("number"), int(prn))
        ev2 = json.load(open(os.path.join(r2["ws"], "devflow", "evaluation.json"), encoding="utf-8"))
        check("while CI runs, no model review is spent on the head", (r2["evaluate"].get("need_models"),
              sorted({a["by"] for a in ev2.get("assignments") or []})), ("no", ["deferred"]))
        check("the implementer is devflow-implementer, not the account the push went through",
              (ev2["implementers"]["identities"], ev2["implementers"]["principals"]),
              (["devflow-implementer"], ["devflow-implementer", "devflow-researcher"]))

        # a change's review is a query: materials and schema in the prompt, checked by the stack
        s.edit(lambda db: set_runs(db, head, build.check_runs()))
        rq = s.run_pass(good_model, review_mode="model")
        evq = json.load(open(os.path.join(rq["ws"], "devflow", "evaluation.json"), encoding="utf-8"))
        ab_ = next(a for a in evq["assignments"] if a["by"] == "model" and "base" in a["lenses"])
        check("the base review reads the repository: it investigates with tools, with the tree of exactly the head in its materials",
              (bool(ab_.get("closed")), os.path.isfile(os.path.join(rq["ws"], "devflow", "materials", "repository", "tools", "gen.py")),
               evq["materials"].get("repository")),
              (False, True, "repository/"))
        by_lens = {l: a for a in evq["assignments"] if a["by"] == "model" for l in a["lenses"]}
        check("a judge or an investigator: criteria-trace (does the code reach the condition?) investigates; "
              "shared-artifact (a closed comparison, with this head's check results) judges closed",
              (bool(by_lens["criteria-trace"].get("closed")), bool(by_lens["shared-artifact"].get("closed")),
               "checks" in (by_lens["shared-artifact"].get("inlined") or [])),
              (False, True, True))
        check("and every review, judge or investigator, carries its schema (--output-schema, agent-stack §102)",
              all(a.get("schema") for a in evq["assignments"] if a["by"] == "model"), True)
        ops = json.load(open(os.path.join(rq["ws"], "devflow", "materials", "artifact_ops.json"), encoding="utf-8"))
        check("and its packet says which canonical head the change sits on, what regenerated it and how merges are ordered "
              "(measured on #24: without these the query declared 'regenerated' unverified)",
              (ops.get("canonical_head") == R(s.db)["branches"][build.CANONICAL], ops.get("head_contains_canonical_head"),
               "regenerated_by" in ops, sorted((ops.get("order") or {}).get("collection-plan", {}))),
              (True, True, True, ["held_by", "lock", "rule"]))
        aq = next(a for a in evq["assignments"] if a["by"] == "model" and a.get("closed"))
        body = open(aq["prompt"], encoding="utf-8").read()
        schema = json.load(open(aq["schema"], encoding="utf-8"))
        sys.path.insert(0, "/work/stack")
        import query as stack_query
        answer = {"head_sha": head, "frame_id": evq["frame"]["id"], "policy_digest": evq["frame"]["policy"]["digest"],
                  "basis_digest": evq["frame"]["basis"], "lenses": aq["lenses"],
                  "materials": [{"ref": "diff", "read": True}], "answers": {}, "findings": [],
                  "unverified": {}, "limits": {}, "verdict": "PASS"}
        check("a change's review is a query: its materials are in the prompt, with the schema it must answer in",
              ("# MATERIALS" in body, "## diff:" in body, '"const": "' + head + '"' in body, aq["inlined"][:1]),
              (True, True, True, ["diff"]))
        check("the schema uses only what the stack's door checks, and a well-formed answer passes it",
              (stack_query.unchecked(schema), stack_query.problems(answer, schema)), ([], []))
        check("and an answer for another head does not",
              bool(stack_query.problems(dict(answer, head_sha="0" * 40), schema)), True)
        envq = {**os.environ, "CONDUCTOR_SELF_RUN_ID": os.path.basename(rq["ws"])}
        step("review_plan.py", ["claude", "claude", "direct", "devflow-reviewer", "research-default", ""], envq)
        planq = json.load(open(os.path.join(rq["ws"], "devflow", "review_plan.json"), encoding="utf-8"))
        want = {a["reviewer"]: a for a in evq["assignments"] if a["by"] == "model"}
        check("and each fan-out member carries its schema to the door; a judge runs in the closed profile",
              sorted((m["label"], m["steps"][0].get("schema") == want[m["label"]]["schema"],
                      m["steps"][0].get("profile") == ("closed" if want[m["label"]].get("closed") else None))
                     for m in planq["members"]),
              sorted((l, True, True) for l in want))
        # a review whose materials, inline, are past what a brokered prompt may carry reads them
        # with its tools instead (measured: a 370 KB query refused by the runner, twice)
        s.edit(lambda db: edit_policy(db, lambda p: p.setdefault("reviews", {}).__setitem__("query_max_chars", 500),
                                      head_too=False))
        rb = s.run_pass(good_model, review_mode="model")
        evb = json.load(open(os.path.join(rb["ws"], "devflow", "evaluation.json"), encoding="utf-8"))
        ab = [a for a in evb["assignments"] if a["by"] == "model"]
        check("a review too large to carry inline is a task review, and the issue says why",
              ([a.get("closed") for a in ab] == [None] * len(ab) and bool(ab),
               "# MATERIALS" in open(ab[0]["prompt"], encoding="utf-8").read(),
               any("reads its materials with tools" in n for n in evb.get("notes") or [])),
              (True, False, True))
        s.edit(lambda db: edit_policy(db, lambda p: p["reviews"].pop("query_max_chars", None), head_too=False))

        def green_and_reviewed(db):
            set_runs(db, head, build.check_runs())
            R(db).setdefault("reviews", {})[prn] = [{"id": 900, "user": "rev-person", "commit_id": head, "state": "COMMENTED",
                                                    "body": build.review_body(head, build.LENSES_PASS, digest=canonical_digest(db),
                                                                              basis=canonical_basis(db))}]
        s.edit(green_and_reviewed)
        r3 = s.run_pass(good_model)
        check("green checks and a review on devflow's head: PASS, handed to the merge owner (agents do not merge)",
              (verdict(r3), phase(r3), (r3["state"].get("wait") or {}).get("reason"), r3["act"].get("do")),
              ("PASS", "integrate", "merge", "none"))
    finally:
        s.close()

    s = Sim(impl_db())
    try:
        def fails_ci(db):
            set_runs(db, branch_of(db), build.check_runs(["devflow-policy", "scanner-publish", "plan-ops", "distro-smoke"])
                     + build.check_runs(["scanner-quick"], "failure"))
        s.run_pass(good_model)
        first = branch_of(s.db)
        s.edit(fails_ci)
        seen = {}

        def fixer(stage, ws):
            seen["feedback"] = json.load(open(os.path.join(ws, "devflow", "feedback.json"), encoding="utf-8"))
            return good_model(stage, ws, {"items/kisa/U-54.yaml": "id: U-54\nbaseline_status: confirmed\n# fixed\n"})
        r = s.run_pass(fixer)
        db = s.db
        check("a failing check on devflow's head: REQUEST_CHANGES, and devflow implements again",
              (r["adjudicate"].get("verdict"), r["act"].get("do"), r["deliver"].get("delivered")),
              ("REQUEST_CHANGES", "implement", "yes"))
        check("the implementer is told which check failed", [c["name"] for c in seen["feedback"]["failed_checks"]],
              ["scanner-quick"])
        check("the fix is a new commit on top of the failed head — the branch fast-forwards, nothing is forced",
              (R(db)["commits"][branch_of(db)]["parents"], len(r["state"]["history"]["pushed"])), ([first], 2))
    finally:
        s.close()

    s = Sim(impl_db())
    try:
        r = s.run_pass(lambda st, ws: good_model(st, ws, {".devflow/project.yaml": "schema: devflow/project@1\n"}))
        check("an implementation that edits the project's rules is not pushed",
              (r["deliver"].get("delivered"), "rules" in r["deliver"].get("failure", ""), branch_of(s.db)), ("no", True, None))
        check("the round is spent, and the task waits for implementation with the reason",
              (r["state"]["history"]["implement_rounds"], r["state"]["wait"]["reason"],
               any("rules" in b for b in r["state"]["blockers"])), (1, "implementation", True))
    finally:
        s.close()

    s = Sim(impl_db())
    try:
        r = s.run_pass(lambda st, ws: good_model(st, ws, {"collector/collect.sh": "#!/bin/sh\necho changed\n"}))
        check("an ordinary change touching the collector breaks the project's scope rules: not pushed",
              (r["deliver"].get("delivered"), "scope" in r["deliver"].get("failure", "")), ("no", True))
    finally:
        s.close()

    s = Sim(impl_db())
    try:
        r = s.run_pass(lambda st, ws: good_model(st, ws) if st == "prepare" else False)
        check("an implementation step that produced nothing in this attempt: nothing is pushed",
              (r["deliver"].get("delivered"), "no implement.json" in r["deliver"].get("failure", "")), ("no", True))
    finally:
        s.close()

    s = Sim(impl_db())
    try:
        r = s.run_pass(lambda st, ws: good_model(st, ws, {"FAILGEN": "x"}))
        check("the project's generator fails: nothing is pushed, and the failure is the reason",
              (r["deliver"].get("delivered"), "regeneration failed" in r["deliver"].get("failure", "")), ("no", True))
    finally:
        s.close()

    s = Sim(impl_db())
    try:
        def hold(stage, ws):
            write(ws, ".devflow/tasks/T-0054.yaml", build.TRACE)
            write(ws, ".devflow/tasks/T-0054.research.md", "- open question\n")
            write(ws, "items/kisa/U-54.yaml", "id: U-54\n")          # not pushed while the task is held
            report(ws, "prepare", {"proceed": False, "summary": "needs a decision",
                                   "decisions": ["is the FTPS exception observable?"], "gaps": []})
            return True
        r = s.run_pass(hold)
        files = R(s.db)["commits"][branch_of(s.db)]["files"]
        check("preparation that leaves a question: its record is pushed, the implementation is not",
              (r.get("proceed", {}).get("proceed"), ".devflow/tasks/T-0054.yaml" in files, files.get("items/kisa/U-54.yaml")),
              ("no", True, None))
        check("and the task waits for that decision: NEEDS_DECISION in preparation",
              (verdict(r), phase(r), r["state"]["wait"]["reason"]), ("NEEDS_DECISION", "prepare", "decision"))
        s.edit(lambda db: set_runs(db, branch_of(db), build.check_runs()))
        r2 = s.run_pass(good_model)
        check("green checks on that head change nothing: preparation is not an implementation",
              (verdict(r2), phase(r2), r2["act"].get("do")), ("NEEDS_DECISION", "prepare", "none"))
        r2m = s.run_pass(good_model, review_mode="model")
        check("and no model is asked to review it as one (measured on #19: four reviewers were)",
              r2m["evaluate"].get("need_models"), "no")
        s.edit(lambda db: set_task(db, decisions=["FTPS: not observable; REVIEW (astro3141, 2026-09-25)"]))
        before = branch_of(s.db)
        r3 = s.run_pass(good_model)
        check("once the task records the answer, devflow prepares again on its branch and implements",
              (r3["act"].get("do"), r3["deliver"].get("delivered"), R(s.db)["commits"][branch_of(s.db)]["parents"]),
              ("prepare", "yes", [before]))
    finally:
        s.close()

    # the project's rules change after a preparation was held: it is prepared again (measured on
    # U-37: the hook learned to read the criterion PDF, and the hold stood)
    s = Sim(impl_db())
    try:
        def held(stage, ws):
            # like U-37's: the held preparation's own trace leaves a mapping unresolved
            write(ws, ".devflow/tasks/T-0054.yaml", build.TRACE.replace(
                "status: confirmed", "status: unresolved\n    open_question: is the FTPS exception observable?"))
            write(ws, ".devflow/tasks/T-0054.research.md", "- open question\n")
            report(ws, "prepare", {"proceed": False, "summary": "needs a decision",
                                   "decisions": ["is the FTPS exception observable?"], "gaps": []})
            return True
        s.run_pass(held)
        s.edit(lambda db: set_runs(db, branch_of(db), build.check_runs()))
        def land(change):
            def put(db):
                r = R(db)
                c = r["branches"][build.CANONICAL]
                files = dict(r["commits"][c]["files"], **change)
                new = "c2" + build.sha(json.dumps(files, sort_keys=True))[:38]
                r["commits"][new] = {"parents": [c], "files": files, "author": "astro3141"}
                r["branches"][build.CANONICAL] = new
            return put
        s.edit(land({"items/kisa/U-02.yaml": "id: U-02\n# elsewhere\n"}))
        r = s.run_pass(good_model)
        check("canonical moved, but none of the project's rules: the preparation's hold stands",
              (verdict(r), r["act"].get("do")), ("NEEDS_DECISION", "none"))
        s.edit(land({".devflow/instructions/prepare.md": "read the PDF\n"}))
        before = branch_of(s.db)
        r = s.run_pass(good_model)
        check("a rule file changed since preparation: it is prepared again on its branch, and the issue says why",
              (r["act"].get("do"), r["deliver"].get("delivered"), R(s.db)["commits"][branch_of(s.db)]["parents"],
               any("rules changed since preparation" in n for n in json.load(open(os.path.join(
                   r["ws"], "devflow", "evaluation.json"), encoding="utf-8")).get("notes") or [])),
              ("prepare", "yes", [before], True))
    finally:
        s.close()

    def needs_research(stage, ws):
        write(ws, ".devflow/tasks/T-0054.yaml", build.TRACE)
        write(ws, ".devflow/tasks/T-0054.research.md", "- which unit vsftpd installs on el8: open\n")
        report(ws, "prepare", {"proceed": False, "summary": "needs a fact", "decisions": [], "gaps": [],
                               "research": [{"question": "which unit does vsftpd install on el8?",
                                             "why": "U-54: the active condition"}]})
        return True

    def merged_record(text):
        def put(db):
            R(db)["commits"][R(db)["branches"][build.CANONICAL]]["files"]["docs/research/r-ftp.md"] = text
        return put

    s = Sim(impl_db())
    try:
        r = s.run_pass(needs_research)
        check("preparation that needs a fact no record establishes: the item waits for research, with no verdict",
              (verdict(r), phase(r), r["state"]["wait"]["reason"], r["state"]["wait"]["owner"]),
              (None, "prepare", "research", "research lane"))
        check("the question gets an ID a research record answers by, and the wait names it",
              ([q["id"] for q in r["state"]["history"]["prepare_hold"]["research"]], "Q-T-0054-1" in r["state"]["wait"]["need"]),
              (["Q-T-0054-1"], True))
        s.edit(lambda db: set_runs(db, branch_of(db), build.check_runs()))
        r2 = s.run_pass(good_model)
        check("a later pass keeps waiting on that question; nothing is acted on",
              (verdict(r2), r2["state"]["wait"]["reason"], r2["act"].get("do")), (None, "research", "none"))
        s.edit(merged_record("# FTP\nanswers: Q-T-0054-2\n"))
        r3 = s.run_pass(good_model)
        check("a merged record answering another question releases nothing", r3["state"]["wait"]["reason"], "research")
        s.edit(merged_record("# FTP\nanswers: Q-T-0054-1\n\n- vsftpd.service (source ...)\n"))
        before = branch_of(s.db)
        r4 = s.run_pass(good_model)
        check("once a merged record answers it, the item is prepared again on its branch — not implemented straight away",
              (r4["act"].get("do"), r4["deliver"].get("delivered"), R(s.db)["commits"][branch_of(s.db)]["parents"]),
              ("prepare", "yes", [before]))
    finally:
        s.close()

    s = Sim(impl_db())
    try:
        s.run_pass(needs_research)
        s.edit(merged_record("# FTP\nunanswered: Q-T-0054-1\n"))
        r = s.run_pass(good_model)
        check("research closed without the answer the item needs: a person's decision, and the item still waits",
              (verdict(r), r["state"]["wait"]["reason"], any("research closed without an answer to Q-T-0054-1" in b
                                                            for b in r["state"]["blockers"]), r["act"].get("do")),
              ("NEEDS_DECISION", "decision", True, "none"))
    finally:
        s.close()

    s = Sim(impl_db(task_type="research"))
    try:
        s.run_pass(research_model)
        s.edit(lambda db: set_runs(db, branch_of(db), build.check_runs()))
        r = s.run_pass(research_model)
        check("a research record whose task is unchanged is not rewritten", r["act"].get("do"), "none")
        s.edit(lambda db: set_task(db, goal="FTP daemons on the target distributions (facts only)"))
        before = branch_of(s.db)
        r2 = s.run_pass(lambda st, ws: research_model(st, ws, {"docs/research/t-0054.md": "# FTP daemons (facts only)\n"}))
        check("the task block changed: the record is rewritten for it, on its branch",
              (r2["act"].get("do"), r2["deliver"].get("delivered"), R(s.db)["commits"][branch_of(s.db)]["parents"]),
              ("research", "yes", [before]))
    finally:
        s.close()

    s = Sim(impl_db())
    try:
        s.run_pass(good_model)

        def human_commit(db):
            r = R(db)
            h = branch_of(db)
            files = dict(r["commits"][h]["files"], **{"items/kisa/U-54.yaml": "id: U-54\n# by hand\n"})
            new = "e" + build.sha(json.dumps(files, sort_keys=True))[:39]
            r["commits"][new] = {"parents": [h], "files": files, "author": "someone"}
            r["branches"]["devflow/t-0054"] = new
            for p in r["pulls"].values():
                p["head_sha"] = new
            set_runs(db, new, build.check_runs(["devflow-policy", "scanner-publish", "plan-ops", "distro-smoke"])
                     + build.check_runs(["scanner-quick"], "failure"))
        s.edit(human_commit)
        r = s.run_pass(good_model)
        ev = json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json"), encoding="utf-8"))
        check("someone else's commit on devflow's branch: they and the account behind the PR are implementers",
              ("someone" in ev["implementers"]["identities"], "operator" in ev["implementers"]["identities"]), (True, True))
        check("and devflow does not take the branch back: its implementer continues it",
              (verdict(r), r["act"].get("do")), ("REQUEST_CHANGES", "none"))
    finally:
        s.close()

    s = Sim(impl_db(max_rounds=1))
    try:
        s.run_pass(good_model)
        s.edit(lambda db: set_runs(db, branch_of(db), build.check_runs(["scanner-quick"], "failure")))
        r = s.run_pass(good_model)
        check("implement.max_rounds spent: devflow stops implementing and says so",
              (verdict(r), r["act"].get("do"), "max_rounds" in r["act"].get("why", "")), ("REQUEST_CHANGES", "none", True))
    finally:
        s.close()

    s = Sim(impl_db())
    try:
        def races(stage, ws):
            s.edit(lambda db: R(db)["branches"].__setitem__("devflow/t-0054", R(db)["branches"][build.CANONICAL]))
            return good_model(stage, ws)
        r = s.run_pass(races)
        check("a branch someone created meanwhile is not overwritten",
              (r["deliver"].get("delivered"), "does not overwrite" in r["deliver"].get("failure", "")), ("no", True))
    finally:
        s.close()

    s = Sim(impl_db(task_type="research"))
    try:
        r = s.run_pass(research_model)
        db = s.db
        head = branch_of(db)
        files = R(db)["commits"].get(head, {}).get("files", {})
        changed = sorted(p for p in files if files.get(p) != R(db)["commits"][R(db)["branches"][build.CANONICAL]]["files"].get(p))
        check("a research task goes to the research lane, not to preparation and implementation",
              (r["act"].get("do"), r["deliver"].get("delivered")), ("research", "yes"))
        check("its commit holds the record and nothing else — no generator ran", changed, ["docs/research/t-0054.md"])
        check("and it waits for CI and review like any change",
              (phase(r), r["state"]["wait"]["reason"]), ("verify_review", "verification"))
        ev = json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json"), encoding="utf-8"))
        s.edit(lambda db: set_runs(db, branch_of(db), build.check_runs()))
        r2 = s.run_pass(research_model, review_mode="model")
        ev2 = json.load(open(os.path.join(r2["ws"], "devflow", "evaluation.json"), encoding="utf-8"))
        check("its review carries the research-record lens", "research-record" in (ev2.get("reviews") or {}).get("required", []), True)
        check("a repository file the record cites, in a directory or at the root, is in the reviewer's materials (and a path that is not there is not)",
              sorted(os.path.basename(c) for c in (ev2.get("materials") or {}).get("cited", [])), ["collection-plan.tsv.txt", "fixtures__el8__cmd__svc_list.stdout.txt", "tools__gen.py.txt"])
        check("a research record's review is the research lane's: its own reviewer, with its author's inputs",
              (r2["evaluate"].get("lane"), (ev2.get("materials") or {}).get("reference")), ("research", ["reference/legacy/"]))
        prompt = open(next(a["prompt"] for a in ev2["assignments"] if a.get("prompt")), encoding="utf-8").read()
        check("and it is told the hosts it may fetch from — the researcher's",
              ("packages.ubuntu.com" in prompt and "{WEB}" not in prompt), True)
        check("the round names the model families it needs all of",
              r2["evaluate"].get("models"), "claude,codex")
        wf = yaml.safe_load(open(os.path.join(PKG, "devflow.yaml"), encoding="utf-8"))
        steps_by = {a["name"]: a for a in wf["agents"]}
        check("and the workflow asks whether every one is usable before starting any (the stack's admit_models.py)",
              (steps_by["evaluate"]["routes"][0]["to"], "evaluate.output.models" in steps_by["evaluate"]["routes"][0]["when"],
               steps_by["admit"]["args"][0], [r["to"] for r in steps_by["admit"]["routes"]]),
              ("admit", True, "/work/stack/steps/admit_models.py", ["review_plan", "adjudicate"]))
        env_h = {**os.environ, "CONDUCTOR_SELF_RUN_ID": os.path.basename(r2["ws"])}
        step("adjudicate.py", ["", "claude: exhausted: session 91% >= 80%"], env_h)
        held = json.load(open(os.path.join(r2["ws"], "devflow", "state.json"), encoding="utf-8"))
        check("a round held for a provider says so on the issue, in the router's words",
              any("not started this run: claude: exhausted" in n for n in held.get("notes") or []), True)
        check("two models review it, both as the research reviewer (brokered, every vendor runs as a role) — labels inside the review_r write rule",
              sorted((a["reviewer"], a.get("provider"), a.get("principal")) for a in ev2["assignments"]),
              [("r1-claude", "claude", "devflow-research-reviewer"), ("r2-codex", "codex", "devflow-research-reviewer")])
        plan = json.load(open(os.path.join(r2["ws"], "devflow", "review_plan.json"), encoding="utf-8")) \
            if os.path.isfile(os.path.join(r2["ws"], "devflow", "review_plan.json")) else None
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": os.path.basename(r2["ws"])}
        # the admission the round started on, as admit_models.py writes it; the stack's roles.py
        # binds the named models against it
        adm = tempfile.mkdtemp(prefix="devflow-adm-")

        def admitted(**eligible):
            json.dump({"wanted": sorted(eligible), "decision": {"evaluated": [
                {"provider": p, "eligible": ok, "why": "within limits" if ok else "exhausted: session 91% >= 80%"}
                for p, ok in eligible.items()]}}, open(os.path.join(adm, "admission.json"), "w"))
            step("review_plan.py", ["", "", "direct", "devflow-reviewer", "research-default", adm], env)
            return json.load(open(os.path.join(r2["ws"], "devflow", "review_plan.json"), encoding="utf-8"))
        plan = admitted(claude=True, codex=True)
        check("and each runs on its own model, bound by the stack's roles.py: its login and principal",
              sorted((m["steps"][0]["provider"], m["steps"][0]["login"], m["steps"][0]["principal"]) for m in plan["members"]),
              [("claude", "claude", "devflow-research-reviewer"), ("codex", "codex", "devflow-research-reviewer")])
        plan = admitted(claude=False, codex=True)
        check("a named model the admission did not find eligible is left out, with the admission's words",
              ([m["label"] for m in plan["members"]], any("91%" in u for u in plan["unbound"])), (["r2-codex"], True))
        shutil.rmtree(adm, ignore_errors=True)

        def one_person_review(db):
            h = branch_of(db)
            n = str(r2["state"]["history"]["pr"])
            R(db).setdefault("reviews", {})[n] = [{"id": 950, "user": "rev-person", "commit_id": h, "state": "COMMENTED",
                                                  "body": build.review_body(h, ["base", "research-record"], digest=canonical_digest(db),
                                                                            basis=canonical_basis(db))}]
        s.edit(one_person_review)
        r3 = s.run_pass(research_model)
        check("one complete review is one model: the lens is not complete until a second model completes it",
              ((r3["state"].get("reviews") or {}).get("status"), any("fewer than 2 different models" in n for n in r3["state"].get("notes") or [])),
              ("incomplete", True))
        check("a research record's review investigates with tools (it fetches primary pages; a closed call has no web)",
              [a.get("closed") for a in ev2["assignments"]], [None, None])
        check("and the lenses that may read it must read it — it is there",
              ("cited" in ev2["lens_defs"]["base"]["read"], "cited" in ev2["lens_defs"]["research-record"]["read"]), (True, True))
        check("its reviewers are told a finding blocks only when it endangers what the served items need",
              ("A finding\n  is **blocking** only when it endangers them" in prompt, "{BLOCKING}" in prompt), (True, False))

        def blocking_review(affects):
            def put(db):
                h = branch_of(db)
                n = str(r2["state"]["history"]["pr"])
                f = {"lens": "research-record", "severity": "blocking", "claim": "a peripheral sentence has no source",
                     "basis": {"kind": "observation", "ref": "docs/research/t-0054.md:3"}, "evidence": "no source named"}
                if affects:
                    f["affects"] = affects
                R(db).setdefault("reviews", {})[n] = [{"id": 951, "user": "rev-person", "commit_id": h, "state": "COMMENTED",
                                                      "body": build.review_body(h, ["base", "research-record"], verdict="REQUEST_CHANGES",
                                                                                findings=[f], digest=canonical_digest(db),
                                                                                basis=canonical_basis(db))}]
            return put
        s.edit(blocking_review(None))
        r4 = s.run_pass(research_model)
        check("a blocking finding on a record that names nothing it endangers: shown as advisory, no further round",
              (verdict(r4), r4["act"].get("do"), any("name nothing they endanger" in n for n in r4["state"].get("notes") or [])),
              (None, "none", True))
        s.edit(blocking_review("U-54: which unit is active"))
        r5 = s.run_pass(research_model)
        check("one that names the item condition it endangers holds the record: REQUEST_CHANGES",
              verdict(r5), "REQUEST_CHANGES")
    finally:
        s.close()

    s = Sim(impl_db(task_type="research", reviewer_models={"research": ["codex"]}))
    try:
        s.run_pass(research_model)
        s.edit(lambda db: set_runs(db, branch_of(db), build.check_runs()))
        r = s.run_pass(research_model, review_mode="model")
        ev = json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json"), encoding="utf-8"))
        check("a project that names codex to review research records: one reviewer, codex, as the research reviewer",
              ([(a["reviewer"], a.get("provider"), a.get("principal")) for a in ev["assignments"]], r["evaluate"].get("models")),
              ([("r1-codex", "codex", "devflow-research-reviewer")], "codex"))
        check("and one model completing a lens is enough (distinct_models not raised)",
              (ev["reviews"]["distinct_models"]), 1)
    finally:
        s.close()

    s = Sim(impl_db(task_type="research"))
    try:
        r = s.run_pass(lambda st, ws: research_model(st, ws, {"items/kisa/U-54.yaml": "id: U-54\n"}))
        check("a research lane that writes outside the research directory: nothing is pushed",
              (r["deliver"].get("delivered"), "outside" in r["deliver"].get("failure", ""), branch_of(s.db)), ("no", True, None))
    finally:
        s.close()

    s = Sim(impl_db(task_type="survey"))
    try:
        r = s.run_pass(lambda st, ws: research_model(st, ws, report_it=False))
        check("a survey step that produced no report in this attempt: nothing is pushed",
              (r["act"].get("do"), r["deliver"].get("delivered"), "no research.json" in r["deliver"].get("failure", "")),
              ("research", "no", True))
    finally:
        s.close()

    import dfcore

    def research_ctx(lane, codex_unverified):
        rv = lambda prov, complete, unv: {"valid": True, "complete": complete, "unverified": unv, "findings": [],
                                          "verdict": "PASS", "independent": True,
                                          "reviewer": {"kind": "model", "provider": prov, "identity": f"devflow-model:{prov}"}}
        return {"lane": lane, "pr": {"head_sha": "h" * 40, "base_sha": "b" * 40, "state": "open"},
                "checks": {"results": {}}, "history": {},
                "reviews": {"required": ["base", "research-record"], "distinct_models": 2,
                            "results": [rv("claude", ["base", "research-record"], {}),
                                        rv("codex", ["base"], {"research-record": codex_unverified})]}}
    declared = {"not_read": [], "unanswered": [], "declared": ["a fixture the record names is not in cited"]}
    st = dfcore.adjudicate(research_ctx("research", declared))
    check("research lane: two PASS reviews, one declaring scope it could not verify — a limit, not a hold",
          (st["verdict"], st["phase"], any("stay noted as limits" in n for n in st["notes"])), ("PASS", "integrate", True))
    st = dfcore.adjudicate(research_ctx("research", dict(declared, not_read=["cited"])))
    check("but material the reviewer did not read still leaves the lens incomplete", st["reviews"]["status"], "incomplete")
    st = dfcore.adjudicate(research_ctx("change", declared))
    check("and on a change, declared unverified scope still holds the lens", st["reviews"]["status"], "incomplete")

    sys.path.insert(0, STEPS)
    import drive
    d = lambda **p: drive.decide({"ok": True, **p}, None, 0, 3600)[:2]
    check("drive: a pass that pushed a head waits for CI, then passes again", d(delivered="yes abc PR 4"), (True, -1))
    check("drive: CI still running on the head: wait, then pass again", d(wait="verification", owner="CI"), (True, -1))
    check("drive: a decision is a person's move: stop", d(wait="decision", owner="decision owner")[0], False)
    check("drive: the task is done: stop", d(phase="done")[0], False)
    check("drive: a failed pass: stop", drive.decide({"ok": False, "reason": "x"}, None, 0, 1)[0], False)
    same = {"ok": True, "verdict": None, "phase": "verify_review", "wait": "review", "owner": "reviewer", "head": "h"}
    check("drive: a pass that reached the same state as the last one: stop (no progress)",
          drive.decide(same, dict(same), 0, 1)[0], False)
    holdable = dict(same, need_models="yes")
    check("drive: reviews a model may still supply but none ran (router held): wait, then pass again",
          drive.decide(holdable, dict(holdable), 0, 3600)[:2], (True, -2))
    check("drive: and that wait is bounded", drive.decide(holdable, dict(holdable), 3600, 3600)[0], False)

    class _Checks:
        sha = None

        def check_runs(self, sha):
            self.sha = sha
            return [{"name": "scanner-quick", "status": "completed"}, {"name": "devflow/verdict", "status": "in_progress"}]
    stub, orig = _Checks(), dfcore.open_remote
    dfcore.open_remote = lambda *a, **k: stub
    try:
        settled = drive.settle("scanner-fixture", "m" * 40, 1, 10, sleep=lambda s: None)
    finally:
        dfcore.open_remote = orig
    check("drive: CI has settled when the checks have — devflow's own in-progress verdict is not CI",
          (settled[0], stub.sha), (True, "m" * 40))
    # after a merge: the pass names the merge commit as the one to wait on, and the drive waits there
    passes = [{"ok": True, "run": "p1", "phase": "integrate", "verdict": "PASS", "wait": "verification",
               "owner": "CI", "head": "h" * 40, "watch": "m" * 40},
              {"ok": True, "run": "p2", "phase": "done", "verdict": "PASS"}]
    watched = []
    saved = drive.one_pass, drive.settle
    drive.one_pass = lambda *a, **k: passes.pop(0)
    drive.settle = lambda project, head, *a, **k: (watched.append(head), (True, 0))[1]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            drive.main(["scanner-fixture", "3", "p", "lt", "model", "3", "1", "10"])
    finally:
        drive.one_pass, drive.settle = saved
    check("drive: after a merge it waits on the commit the pass names (the merge commit), not the pull request's head",
          watched, ["m" * 40])


def controls_auth():
    print("\nauth — the panel's GitHub login: used as it is, refreshed before it expires, never guessed")
    import ghauth
    d = tempfile.mkdtemp(prefix="devflow-auth-")
    old = {k: os.environ.get(k) for k in ("DEVFLOW_GITHUB_HOME", "DEVFLOW_GITHUB_TOKEN")}
    os.environ["DEVFLOW_GITHUB_HOME"] = d
    os.environ.pop("DEVFLOW_GITHUB_TOKEN", None)
    path = os.path.join(d, ghauth.TOKEN_FILE)
    calls = []

    def post(url, fields):
        calls.append(fields)
        return {"access_token": "ghu_new", "expires_in": 28800, "refresh_token": "ghr_new",
                "refresh_token_expires_in": 15897600}
    try:
        try:
            ghauth.token()
            got = "no error"
        except ghauth.AuthError as e:
            got = "연결" in e.hint
        check("no login and no token: refused, and the hint names the panel's 연결", got, True)
        os.environ["DEVFLOW_GITHUB_TOKEN"] = "ghp_env"
        check("DEVFLOW_GITHUB_TOKEN alone still works", ghauth.token(), "ghp_env")
        ghauth.write_token(path, {"access_token": "gho_forever", "client_id": "c"})
        check("a login token that does not expire is used as it is — and before the env token",
              (ghauth.token(post=post), calls), ("gho_forever", []))
        now = 1_000_000.0
        ghauth.write_token(path, {"access_token": "ghu_old", "client_id": "c", "expires_at": now + 60,
                                  "refresh_token": "ghr_old", "refresh_expires_at": now + 10**6, "login": "astro3141"})
        tok = ghauth.token(now=now, post=post)
        kept = json.load(open(path))
        check("an 8-hour token about to expire is refreshed with its refresh token, no secret, no person",
              (tok, calls[-1]["grant_type"], calls[-1]["refresh_token"], "client_secret" in calls[-1]),
              ("ghu_new", "refresh_token", "ghr_old", False))
        check("and the new pair is kept, with who logged in",
              (kept["access_token"], kept["refresh_token"], kept["login"], oct(os.stat(path).st_mode & 0o777)),
              ("ghu_new", "ghr_new", "astro3141", "0o600"))
        ghauth.write_token(path, dict(kept, expires_at=now + 60, refresh_expires_at=now - 1))
        try:
            ghauth.token(now=now, post=post)
            got = "no error"
        except ghauth.AuthError as e:
            got = "six months" in str(e)
        check("a refresh token past its six months: refused with the reason, not a request that fails later", got, True)
        ghauth.write_token(path, dict(kept, expires_at=now + 60, refresh_expires_at=now + 10**6))
        try:
            ghauth.token(now=now, post=lambda u, f: {"error": "bad_refresh_token"})
            got = "no error"
        except ghauth.AuthError as e:
            got = "bad_refresh_token" in str(e)
        check("GitHub refusing the refresh is an error that says so", got, True)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(d, ignore_errors=True)


# ================================================================================== candidate
def candidate_db():
    """A Foundation in the scanner's own transaction flow, implemented by devflow as a local
    candidate (flow_defs.transaction.candidate: local): no pull request, nothing pushed."""
    db = impl_db()

    def pol(p):
        p["flow_defs"]["transaction"]["candidate"] = "local"
        p["flow_defs"]["transaction"]["local_checks"] = {
            "scanner-quick": "true", "devflow-policy": "true", "scanner-publish": "true", "plan-ops": "true", "distro-smoke": "true"}
    edit_policy(db, pol, head_too=False)
    set_task(db, change_class="FOUNDATION", impacts=["contract_changed", "shared_plan_changed"], pr=None, implementer=None,
             scope_files=["items/kisa/U-54.yaml", "bindings/linux/u54.yaml", "collection-plan.tsv", "dist/collection-plan.tsv",
                          "tests/test_u54_ftp.py", ".devflow/tasks/T-0054.yaml", ".devflow/tasks/T-0054.research.md"])
    return db


def candidate_dir(db):
    """Where devflow keeps the fixture task's candidate (one repository under the scratch state root)."""
    root = os.path.join(stack_step.runtime()["paths"]["state_root"], "devflow", "candidates")
    for d in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        t = os.path.join(root, d, "T-0054")
        if os.path.isdir(t):
            return t
    return None


def controls_candidate():
    print("")
    print("candidate — a transaction Foundation is implemented locally: judged like a head, never pushed, published by a person")
    import candidate as cmod
    import subprocess as sp
    root = os.path.join(stack_step.runtime()["paths"]["state_root"], "devflow", "candidates")
    shutil.rmtree(root, ignore_errors=True)
    s = Sim(candidate_db())
    try:
        r = s.run_pass(good_model)
        check("gate 1 first: nothing is implemented before the person approves the scope",
              (r["act"].get("do"), phase(r), r["state"]["wait"]["reason"]), ("none", "prepare", "decision"))
        task_digest = json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json")))["task_digest"]
        s.edit(lambda db: approve(db, "gate1", task_digest))
        r = s.run_pass(good_model)
        db = s.db
        cdir = candidate_dir(db)
        cur = cmod.load(cdir) if cdir else None
        check("gate 1 approved: devflow prepares and implements, and the result is a local candidate, not a push",
              (r["act"].get("do"), r["deliver"].get("delivered"), branch_of(db), R(db)["pulls"], bool(cur)),
              ("prepare", "yes", None, {}, True))
        sha = (cur or {}).get("sha", "")
        check("the candidate is a real commit in the candidate repository, and the bundle carries exactly it",
              (len(sha) == 40, sp.run(["git", "-C", os.path.join(cdir, "repo"), "cat-file", "-t", sha], capture_output=True, text=True).stdout.strip(),
               os.path.isfile(cur["bundle"]), sha in sp.run(["git", "-C", os.path.join(cdir, "repo"), "bundle", "list-heads", cur["bundle"]], capture_output=True, text=True).stdout),
              (True, "commit", True, True))
        check("the candidate's tree holds what the implementer wrote, on top of the base",
              (cmod.file_at(os.path.join(cdir, "repo"), sha, "items/kisa/U-54.yaml") or "").startswith("id: U-54"), True)
        check("the published state waits for the operator's local verification, not for CI, with no status on the remote",
              (phase(r), verdict(r), r["state"]["wait"]["reason"], r["state"]["wait"]["owner"], R(db).get("statuses", {})),
              ("verify_review", None, "verification", "operator", {}))
        check("drive stops here: a candidate has no CI to wait for",
              drive_decide({"ok": True, "delivered": "yes", "wait": "verification", "owner": "operator", "next": "verify"})[0], False)

        r2 = s.run(review_mode="model")
        ev2 = json.load(open(os.path.join(r2["ws"], "devflow", "evaluation.json")))
        check("the next pass judges the candidate's sha as the head; its checks are NOT_RUN until verified; no review is spent",
              ((ev2.get("pr") or {}).get("head_sha"), (ev2.get("pr") or {}).get("candidate"), ev2["checks"]["pending"] != [],
               r2["evaluate"].get("need_models")), (sha, True, True, "no"))
        # the operator's verification of exactly this sha
        vdir = os.path.join(cdir, "verification"); os.makedirs(vdir, exist_ok=True)
        json.dump({"sha": "0" * 40, "at": "2026-10-26T00:00:00Z", "by": "op", "host": "mac",
                   "checks": {n: {"result": "PASS"} for n in ev2["checks"]["required"]}},
                  open(os.path.join(vdir, "0" * 40 + ".json"), "w"))
        r3 = s.run(review_mode="model")
        check("a verification of another sha is no verification of this candidate",
              json.load(open(os.path.join(r3["ws"], "devflow", "evaluation.json")))["checks"]["pending"] != [], True)
        json.dump({"sha": sha, "at": "2026-10-26T00:00:00Z", "by": "op", "host": "mac",
                   "checks": {n: {"result": "PASS", "why": "exit 0"} for n in ev2["checks"]["required"]}},
                  open(os.path.join(vdir, f"{sha}.json"), "w"))
        # the reviews are written under the policy as this scenario edited it (candidate: local)
        r4 = s.run(review_mode="model", receipt=lambda ws: model_receipt(ws, policy_digest=canonical_digest(s.db)))
        check("verified: the checks PASS from the operator's file, the model reviews run on the frozen candidate, and gate 2 binds the candidate's sha",
              (r4["state"]["checks"]["complete"], verdict(r4), phase(r4), r4["state"]["wait"]["need"]),
              (True, "PASS", "integrate", f"/devflow approve gate2 {sha[:12]}"))
        s.edit(lambda db: approve(db, "gate2", sha[:12]))
        r5 = s.run(review_mode="model")
        check("gate 2 approved: ready for the person who publishes A+B — nothing merged, nothing pushed",
              (verdict(r5), r5["integrate"].get("ready"), r5["state"]["wait"]["reason"], r5["state"]["wait"]["owner"],
               branch_of(s.db), R(s.db)["pulls"]), ("PASS", "yes", "publication", "human operator", None, {}))

        def publish_ab(db):
            rr = R(db)
            c = rr["branches"][build.CANONICAL]
            files = dict(rr["commits"][c]["files"])
            files["items/kisa/U-54.yaml"] = "id: U-54\nbaseline_status: confirmed\n"
            rr["commits"][sha] = {"parents": [c], "files": files, "author": "devflow-implementer"}
            files2 = dict(files)
            files2[".issue-orchestrator/rebaseline/3-" + sha + ".yaml"] = (
                "foundation_commit: " + sha + "\nfoundation_actor: devflow-implementer\nhuman_approval: {approver: astro3141}\n"
                "validation_evidence: {validated_sha: " + sha + ", exit_code: 0}\n"
                "independent_review: {reviewer: devflow-reviewer, session_id: s-1, reviewed_sha: " + sha + ", blocking_findings: []}\n")
            rr["commits"]["b" * 40] = {"parents": [sha], "files": files2, "author": "astro3141"}
            rr["branches"][build.CANONICAL] = "b" * 40
            rr["check_runs"]["b" * 40] = build.check_runs(["scanner-quick", "scanner-publish", "devflow-policy", "distro-smoke"])
        s.edit(publish_ab)
        s.edit(lambda db: set_task(db, transaction_commit=sha))
        r6 = s.run()
        check("published by the person as A+B with the record binding review and validation to exactly A: done",
              (verdict(r6), phase(r6)), ("PASS", "done"))
    finally:
        s.close()
        shutil.rmtree(root, ignore_errors=True)


def drive_decide(p, prev=None, waited=0, max_wait=3600):
    import importlib.util
    spec = importlib.util.spec_from_file_location("drive_ctl", os.path.join(STEPS, "drive.py"))
    drive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drive)
    return drive.decide(p, prev, waited, max_wait)


def dfcore_mark():
    import dfcore
    return dfcore.PR_MARK


if __name__ == "__main__":
    for group in (controls_flow, controls_block, controls_stale, controls_foundation, controls_evidence,
                  controls_shared, controls_effects, controls_rules, controls_tool, controls_implement,
                  controls_auth, controls_candidate):
        group()
    print("")
    print(f"{len(PASS_)}/{len(PASS_) + len(FAIL_)} controls")
    sys.exit(1 if FAIL_ else 0)
