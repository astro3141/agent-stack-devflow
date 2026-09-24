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
"""
import copy, hashlib, json, os, secrets, shutil, subprocess, sys, tempfile

PKG = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PKG, "fixtures"))
sys.path.insert(0, os.path.join(PKG, "lib"))
import build  # noqa: E402

PY = sys.executable
STEPS = os.path.join(PKG, "steps")
WS_ROOT = os.environ.get("AGENTSTACK_WORKSPACE_ROOT", "/ws")
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
        self.dir = tempfile.mkdtemp(prefix="devflow-ctl-")
        self.path = os.path.join(self.dir, "ctl.json")
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
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": rid, "DEVFLOW_HANDOFF": self.dir}
        ws = os.path.join(WS_ROOT, rid)
        r = {"ws": ws}
        r["evaluate"] = step("evaluate.py", [project, "3", "ctl", review_mode], env)
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

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)
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

    # the rework limit (OPERATING_POLICY: one automatic rework) escalates instead of looping
    s = Sim(build.scanner_pass())
    try:
        fail_publish = build.check_runs(["scanner-quick", "devflow-policy", "plan-ops", "distro-smoke"]) + build.check_runs(["scanner-publish"], "failure")
        s.edit(lambda db: set_runs(db, R(db)["pulls"]["7"]["head_sha"], fail_publish))
        r1 = s.run()
        new = s.edit(lambda db: build.push_new_head(db))
        s.edit(lambda db: set_runs(db, new, fail_publish))
        r2 = s.run()
        check("the first REQUEST_CHANGES returns to implement", verdict(r1), "REQUEST_CHANGES")
        check("the same failure again after the one allowed rework: NEEDS_DECISION, no further loop",
              (verdict(r2), r2["state"]["wait"]["reason"]), ("NEEDS_DECISION", "decision"))
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
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": rid, "DEVFLOW_HANDOFF": s.dir}
        step("evaluate.py", ["scanner-fixture", "3", "ctl", "external"], env)
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
def model_receipt(ws, members=None, context=None, write=True, stale=False, tamper=False, frame_from=None):
    """What steps/tasks.py writes for a fan-out: the review file and a receipt naming it. Without
    `members`, one member per model assignment evaluate made — whatever the policy's grouping."""
    ev = json.load(open(os.path.join(ws, "devflow", "evaluation.json")))
    frame = ev["frame"]
    if members is None:
        members = {a["reviewer"]: a["lenses"] for a in ev.get("assignments") or [] if a.get("by") == "model"}
    rc = {"context": context or frame["id"], "members": {}}
    for label, lenses in members.items():
        name = f"review_{label}.json"
        body = build.review_body(frame["head_sha"], lenses, basis=frame["basis"])
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
            {"id": 77, "user": "devflow", "body": '<!-- devflow:state {"history":{"model_calls":6}} -->'}))
        r = s.run(review_mode="model")
        ev = json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json")))
        check("the model budget spent: no model review, the assignment waits for a person",
              (r["evaluate"].get("need_models"), ev["assignments"][0]["by"], verdict(r)), ("no", "external", None))
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
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": rid, "DEVFLOW_HANDOFF": s.dir}
        step("evaluate.py", ["scanner-fixture", "3", "ctl", "external"], env)
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
    # outside the stack's PYTHONPATH a step cannot know its workspace or its egress: it stops
    env = {**os.environ, "PYTHONPATH": "", "CONDUCTOR_SELF_RUN_ID": "ctl-" + secrets.token_hex(4)}
    for name, args in (("evaluate.py", ["scanner-fixture", "3", "ctl", "external"]), ("adjudicate.py", [""]),
                       ("integrate.py", ["scanner-fixture"]), ("publish.py", ["scanner-fixture"]),
                       ("review_plan.py", ["claude", "claude", "direct", "devflow-reviewer"])):
        out = step(name, args, env)
        check(f"{name} without the stack's settings: TOOL_FAILURE, not a run on defaults",
              (out.get("status"), "settings" in str(out.get("error"))), ("TOOL_FAILURE", True))
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


if __name__ == "__main__":
    for group in (controls_flow, controls_block, controls_stale, controls_foundation, controls_evidence,
                  controls_shared, controls_effects, controls_rules, controls_tool):
        group()
    print("")
    print(f"{len(PASS_)}/{len(PASS_) + len(FAIL_)} controls")
    sys.exit(1 if FAIL_ else 0)
