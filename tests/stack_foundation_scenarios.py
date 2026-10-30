"""Real DevFlow steps inside agent-stack; fixture GitHub and deterministic model outputs.

Run with the stack's PYTHONPATH/registration, as for controls.py. These are integration controls,
not a claim that a live model, live GitHub approvals or production publication was exercised.
"""
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GUARD = ".issue-orchestrator/authority-guard.sh"
RECORD = ".issue-orchestrator/foundation-pr/{task}.json"
WRITER = ".issue-orchestrator/promote.py"

HOOK = '''import json, sys
d = json.load(sys.stdin)
o = {"protocol": "devflow-hook@1"}
if d["op"] == "classify":
    o.update(task_type="feature", change_class="FOUNDATION", impacts={}, floor="FOUNDATION",
             required_checks=[], lenses=[], violations=[], decisions=[], gaps=[], problems=[],
             trace_required=False, shared_artifacts=[])
elif d["op"] == "materials":
    o.update(criteria=[], contracts=[], implementation=[])
print(json.dumps(o))
'''

PROMOTE = '''import json, os, re
from pathlib import Path
e = os.environ
g = Path(".issue-orchestrator/authority-guard.sh")
a = e["DEVFLOW_FOUNDATION_COMMIT"]
g.write_text(re.sub(r'(?m)^EXPECTED_BASE="[0-9a-f]{40}"$', 'EXPECTED_BASE="'+a+'"', g.read_text()))
r = {"schema": e["DEVFLOW_PROMOTION_SCHEMA"], "task": e["DEVFLOW_TASK_ID"],
     "issue": int(e["DEVFLOW_ISSUE"]), "repository": e["DEVFLOW_REPOSITORY"],
     "canonical_branch": e["DEVFLOW_CANONICAL_BRANCH"], "canonical_base": e["DEVFLOW_CANONICAL_BASE"],
     "foundation_commit": a, "task_digest": e["DEVFLOW_TASK_DIGEST"],
     "proposal_digest": e["DEVFLOW_PROPOSAL_DIGEST"],
     "approved_files": json.loads(e["DEVFLOW_APPROVED_FILES"]),
     "evidence_files": json.loads(e["DEVFLOW_EVIDENCE_FILES"]),
     "promotion_files": json.loads(e["DEVFLOW_PROMOTION_FILES"])}
p = Path(e["DEVFLOW_PROMOTION_RECORD"])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(r, sort_keys=True))
'''


HOOK_BLOBS = '''import json, sys
d = json.load(sys.stdin)
o = {"protocol": "devflow-hook@1"}
if d["op"] == "classify":
    row = (d.get("blobs") or {}).get("collector/lib/core.sh") or {}
    given = row.get("base") is not None and row.get("head") is not None
    code = lambda t: [l for l in t.split("\\n") if l.strip() and not l.lstrip().startswith("#")]
    if given and code(row["base"]) == code(row["head"]):
        evidence, note = ["non_executable_change"], "collector/lib/core.sh: comment or blank lines only"
    elif given:
        evidence, note = ["criteria_mapping"], "collector/lib/core.sh: executable code changed"
    else:
        evidence, note = ["criteria_mapping"], "collector/lib/core.sh: base or head text not given"
    violations = []
    if d.get("pr") is None and "collector/lib/core.sh" in (d.get("changed_files") or []) and not given \\
            and (d["policy"].get("hook") or {}).get("blob_paths"):
        violations = ["delivery gave the hook no base text for collector/lib/core.sh"]
    o.update(task_type="feature", change_class="FOUNDATION", impacts={}, floor="FOUNDATION",
             required_checks=[], lenses=[], violations=violations, decisions=[], gaps=[], problems=[],
             trace_required="criteria_mapping" in evidence, required_evidence=evidence, notes=[note],
             shared_artifacts=[])
elif d["op"] == "materials":
    o.update(criteria=[], contracts=[], implementation=[])
elif d["op"] == "validate_trace":
    o.update(problems=["a change that affects meaning has no criterion mappings"], decisions=[], gaps=[])
print(json.dumps(o))
'''


def database(ct, hook=HOOK, blob_paths=None):
    db = ct.impl_db()
    def policy(p):
        if blob_paths is not None:
            p.setdefault("hook", {})["blob_paths"] = blob_paths
        p["flows"] = {"FOUNDATION": "foundation_pr"}
        p["flow_defs"] = {"foundation_pr": {"kind": "pull_request", "promotion": {
            "command": ["python3", WRITER], "baseline_path": GUARD,
            "record_path": RECORD, "writes": [GUARD, RECORD], "scope_mode": "exact",
            "approval_paths": [GUARD, WRITER], "evidence_paths": [".devflow/tasks/{task}.yaml"]}}}
        p["human_gates"]["FOUNDATION"] = [
            {"id": "gate1", "phase": "prepare", "binds": "proposal"},
            {"id": "gate2", "phase": "integrate", "binds": "head", "exact": True}]
        p["checks"] = {"always": ["foundation-ci"]}
        p["post_merge"] = {"checks": ["foundation-ci"]}
        p["implement"].update(regenerate=[], generated=[], references=[])
    ct.edit_policy(db, policy, head_too=False)
    ct.set_task(db, change_class="FOUNDATION", scope_files=["collector/lib/core.sh"],
                impacts=[], items=[], pr=None)
    repo = ct.R(db)
    base = repo["branches"][ct.build.CANONICAL]
    repo["commits"][base]["files"].update({GUARD: 'EXPECTED_BASE="' + 'c' * 40 + '"\n',
        WRITER: PROMOTE, ".devflow/hooks/scanner_hook.py": hook, "collector/lib/core.sh": "old\n"})
    repo["post_merge_check_runs"] = ct.build.check_runs(["foundation-ci"])
    # FixtureRemote's tree cache, like Git, keys immutable objects by SHA. Other controls change
    # fixture policy in-place; give this different canonical tree its own content-derived identity.
    unique = ct.dfcore.sha256(ct.dfcore.canon(repo["commits"][base]["files"]))[:40]
    repo["commits"][unique] = copy.deepcopy(repo["commits"][base])
    repo["branches"][ct.build.CANONICAL] = unique
    return db


def controls_foundation_pr(ct):
    print("\nFoundation PR — first delivery, review rework, Gate2, human merge and frozen approval basis")
    s = ct.Sim(database(ct))
    def ev(r):
        return json.load(open(os.path.join(r["ws"], "devflow/evaluation.json")))
    def head():
        prior = ct.dfcore.read_state(ct.R(s.db)["issues"]["3"]["comments"], ct.remote_mod.FixtureRemote(s.path, ct.build.REPO).writer())
        n = prior["history"]["pr"]
        return n, ct.R(s.db)["pulls"][str(n)]["head_sha"]
    def review(r, n, sha, reject=False):
        evaluation = ev(r)
        finding = {"lens": "base", "severity": "blocking", "claim": "product needs a revision",
                   "evidence": "collector/lib/core.sh", "basis": {"kind": "requirement", "ref": "task"}}
        body = ct.build.review_body(sha, evaluation["reviews"]["required"],
            digest=evaluation["policy"]["digest"], basis=evaluation["frame"]["basis"],
            verdict="REQUEST_CHANGES" if reject else "PASS", findings=[finding] if reject else [])
        s.edit(lambda db: ct.R(db)["reviews"].setdefault(str(n), []).append({
            "id": 800+n, "user": "rev-person", "commit_id": sha, "state": "COMMENTED", "body": body}))
    def model(stage, ws):
        if stage == "prepare":
            ct.write(ws, ".devflow/tasks/T-0054.yaml", "task: T-0054\n")
            ct.report(ws, stage, {"proceed": True, "summary": "Foundation scope", "decisions": [], "gaps": []})
        else:
            ct.write(ws, "collector/lib/core.sh", "fixed revision\n")
            ct.report(ws, stage, {"summary": "Foundation fix", "tests": []})
        return True
    try:
        r = s.run()
        digest = ev(r)["proposal_digest"]
        ct.check("Foundation implementation waits for Gate1", ct.phase(r), "prepare")
        s.edit(lambda db: ct.approve(db, "gate1", digest))
        r = s.run_pass(model)
        ct.check("Gate1 releases existing work/implement/deliver steps", r.get("deliver", {}).get("delivered"), "yes")
        if r.get("deliver", {}).get("delivered") != "yes":
            raise AssertionError(json.dumps(r, default=str)[-4000:])
        n1, b1 = head()
        repo = ct.R(s.db)
        c = repo["branches"][ct.build.CANONICAL]
        a1 = repo["commits"][b1]["parents"][0]
        ct.check("first product A is a direct child of canonical C", repo["commits"][a1]["parents"], [c])
        s.edit(lambda db: ct.set_runs(db, b1, ct.build.check_runs(["foundation-ci"])))
        r = s.run()
        review(r, n1, b1, reject=True)
        r = s.run_pass(model)
        ct.check("blocking independent review drives an automatic rework", r["adjudicate"].get("verdict"), "REQUEST_CHANGES")
        ct.check("rework publishes a new candidate", r.get("deliver", {}).get("delivered"), "yes")
        if r.get("deliver", {}).get("delivered") != "yes":
            raise AssertionError(json.dumps(r, default=str)[-4000:])
        n2, b2 = head()
        repo = ct.R(s.db)
        a2 = repo["commits"][b2]["parents"][0]
        ct.check("rework A2 is a direct child of C, never B1", repo["commits"][a2]["parents"], [c])
        ct.check("rework uses a new PR and closes the exact old candidate", (n2 != n1, repo["pulls"][str(n1)]["state"]), (True, "closed"))
        s.edit(lambda db: ct.set_runs(db, b2, ct.build.check_runs(["foundation-ci"])))
        r = s.run()
        ct.check("old review does not authorize B2", r["state"]["reviews"]["independent_ok"], False)
        review(r, n2, b2)
        r = s.run()
        ct.check("Gate2 instruction binds full B2 SHA", r["state"]["wait"]["need"], f"/devflow approve gate2 {b2}")
        # A premature human merge is detected even though the old state recorded a PASS waiting
        # for gate2. Its integration result must not be mistaken for an approved publication.
        premature = ct.Sim(copy.deepcopy(s.db))
        try:
            ct.remote_mod.FixtureRemote(premature.path, ct.build.REPO).merge(n2, b2, "merge")
            p = premature.run()
            ct.check("merge without Gate2 never finishes", (ct.verdict(p), ct.phase(p)), ("NEEDS_DECISION", "integrate"))
        finally:
            premature.close()
        s.edit(lambda db: ct.approve(db, "gate2", b2))
        r = s.run()
        ct.check("Gate2 leaves publication to the human merge owner", r.get("integrate", {}).get("ready"), "yes")
        merged = ct.remote_mod.FixtureRemote(s.path, ct.build.REPO).merge(n2, b2, "merge")
        s.edit(lambda db: ct.R(db)["pulls"][str(n2)].__setitem__("base_sha", merged))
        r = s.run()
        ct.check("normal A+B -> Gate2 -> M -> post-merge ends done", (ct.verdict(r), ct.phase(r)), ("PASS", "done"))
        ct.check("post-merge Gate1 digest remains the approved value", ev(r)["proposal_digest"], digest)
        ct.check("post-merge approval ref is pre-merge C", ev(r)["approval_ref"], c)
        def later(db):
            repo = ct.R(db)
            files = {**repo["commits"][merged]["files"], "README.md": "later ordinary publication\n"}
            sha = ct.dfcore.sha256(ct.dfcore.canon(files))[:40]
            repo["commits"][sha] = {"parents": [merged], "files": files, "author": "human"}
            repo["branches"][ct.build.CANONICAL] = sha
        s.edit(later)
        r = s.run()
        ct.check("later canonical movement preserves this completed transaction's approval", (ct.phase(r), ev(r)["proposal_digest"]), ("done", digest))
    finally:
        s.close()


def controls_foundation_blobs(ct):
    """work fetches the tree as an archive (no .git): deliver must still give the hook the base
    text, from the remote; evaluate validates the trace only when the hook asks for
    criteria_mapping; a hook given no text asks for it as before."""
    print("\nFoundation PR — the hook reads what changed inside the file, at delivery and at evaluation")
    def ev(r):
        return json.load(open(os.path.join(r["ws"], "devflow/evaluation.json")))
    def model_for(head_text):
        def model(stage, ws):
            if stage == "prepare":
                ct.write(ws, ".devflow/tasks/T-0054.yaml", "task: T-0054\nmappings: []\n")
                ct.report(ws, stage, {"proceed": True, "summary": "Foundation scope", "decisions": [], "gaps": []})
            else:
                ct.write(ws, "collector/lib/core.sh", head_text)
                ct.report(ws, stage, {"summary": "Foundation change", "tests": []})
            return True
        return model
    def through(db, head_text):
        s = ct.Sim(db)
        try:
            r = s.run()
            s.edit(lambda d: ct.approve(d, "gate1", ev(r)["proposal_digest"]))
            r = s.run_pass(model_for(head_text))
            delivered = r.get("deliver", {}).get("delivered")
            if delivered != "yes":
                return delivered, r.get("deliver", {}).get("failure"), None, None
            prior = ct.dfcore.read_state(ct.R(s.db)["issues"]["3"]["comments"], ct.remote_mod.FixtureRemote(s.path, ct.build.REPO).writer())
            b = ct.R(s.db)["pulls"][str(prior["history"]["pr"])]["head_sha"]
            s.edit(lambda d: ct.set_runs(d, b, ct.build.check_runs(["foundation-ci"])))
            r = s.run()
            return delivered, None, ev(r), r
        finally:
            s.close()
    delivered, failure, e, r = through(database(ct, HOOK_BLOBS, ["collector"]), "old\n# the first Foundation through the lane\n")
    ct.check("comment-only change: delivery gives the hook the base text from the remote (no .git in the tree)", (delivered, failure), ("yes", None))
    if e:
        ct.check("comment-only change: the hook asks no criterion mapping", e.get("required_evidence"), ["non_executable_change"])
        ct.check("comment-only change: the trace is not validated, so an empty one does not fail the head", e.get("trace"), None)
        ct.check("comment-only change: the hook's reading is shown with the state",
                 any("comment or blank lines only" in n for n in r["state"].get("notes") or []), True)
        ct.check("comment-only change: the head is not sent back for rework", ct.verdict(r) != "REQUEST_CHANGES", True)
    delivered, failure, e, r = through(database(ct, HOOK_BLOBS, ["collector"]), "new\n")
    ct.check("executable change: delivered, and the hook asks for the criterion mapping", (delivered, (e or {}).get("required_evidence")), ("yes", ["criteria_mapping"]))
    if e:
        ct.check("executable change: the empty trace fails the head as before", ct.verdict(r), "REQUEST_CHANGES")
    delivered, failure, e, r = through(database(ct, HOOK_BLOBS, None), "old\n# comment\n")
    ct.check("no blob_paths: the hook gets no text and asks for the mapping as before", (delivered, (e or {}).get("required_evidence")), ("yes", ["criteria_mapping"]))
    if e:
        ct.check("no blob_paths: unknown is never unchanged — the empty trace fails the head", ct.verdict(r), "REQUEST_CHANGES")


if __name__ == "__main__":
    import controls
    controls_foundation_pr(controls)
    controls_foundation_blobs(controls)
    print(f"{len(controls.PASS_)}/{len(controls.PASS_)+len(controls.FAIL_)} Foundation integration controls")
    raise SystemExit(bool(controls.FAIL_))
