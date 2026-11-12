"""Pre-Gate1 proposal — real DevFlow steps, fixture GitHub, deterministic proposer output.

Driven from controls.py (`controls_proposal(controls)`). What it pins:
  * a proposal is drafted before the gate that binds it — also for `implement: human` — by
    devflow-proposer, on devflow-proposal/<task id>, as exactly two files (the proposal and devflow's
    record); the canonical branch, the task block and the issue are not touched
  * the gate binds the proposal's exact commit and blobs: an approval of the task digest, of an
    older proposal, or of a stale or scope-mismatched proposal does not count
  * a proposal is stale when what it was drafted from changes (task, policy, contracts, files it
    depends on, research answering its questions) — not when canonical moves elsewhere, and not
    when a person copies its scope into the task block
  * a person's revision request redrafts it once; a re-run with nothing changed drafts nothing
  * the proposer cannot change the checkout; a commit devflow did not push makes the branch INVALID
  * propose.max_rounds is its own limit, passed only by a person's grant
  * a project without `propose` behaves exactly as before (the rest of controls.py)

These are integration controls of the machinery, not a claim that a live model or live GitHub ran.
"""
import json
import os

import yaml

BRANCH = "devflow-proposal/t-0054"
MD = ".devflow/proposals/T-0054.md"
REC = ".devflow/proposals/T-0054.proposal.json"
SCOPE = ["collector/lib/core.sh", "evaluator/check_registry.py", "tests/test_service_running_state.py"]
DEPENDS = ["evaluator/check_registry.py", "docs/TECHNICAL_DESIGN_v0.3.md"]


def proposal_db(ct, max_rounds=3, implement="human"):
    """A Foundation with no pull request, in a project whose gate1 binds a proposal (the pilot)."""
    db = ct.impl_db()

    def pol(p):
        p["human_gates"]["FOUNDATION"][0]["binds"] = "proposal"
        p["propose"] = {"by": "agent", "required_for": ["FOUNDATION"], "max_rounds": max_rounds,
                        "path": ".devflow/proposals/{task}.md"}
    ct.edit_policy(db, pol, head_too=False)
    r = ct.R(db)
    r["commits"][r["branches"][ct.build.CANONICAL]]["files"]["collector/lib/core.sh"] = "pgrep -x \"${n}d\"\n"
    # a draft scope, as a person wrote it before any proposal (the scanner's hook requires one for a
    # Foundation until it reads `proposal_requested`)
    ct.set_task(db, change_class="FOUNDATION", impacts=["contract_changed"], implement=implement,
                scope_files=["collector/lib/core.sh"])
    return db


def move(ct, db, change):
    """A commit lands on canonical (no pull request in these scenarios)."""
    r = ct.R(db)
    c = r["branches"][ct.build.CANONICAL]
    files = dict(r["commits"][c]["files"], **change)
    new = "c" + ct.build.sha(json.dumps([c, files], sort_keys=True))[:39]
    r["commits"][new] = {"parents": [c], "files": files, "author": "astro3141"}
    r["branches"][ct.build.CANONICAL] = new
    return new


def proposer(scope=SCOPE, depends=DEPENDS, decisions=(), research=(), touch=None, text=None, report=True):
    """What a proposer writes: the proposal in its workspace, and its report."""
    def model(stage, ws):
        assert stage == "propose", stage
        os.makedirs(os.path.join(ws, "proposal"), exist_ok=True)
        prev = os.path.join(ws, "proposal", "proposal.md")
        before = open(prev, encoding="utf-8").read() if os.path.isfile(prev) else ""
        fb = json.load(open(os.path.join(ws, "devflow", "feedback.json"), encoding="utf-8"))
        body = text or ("# T-0054 proposal\n\n## Basis\n...\n## Design\nprocess names by alias\n"
                        + (f"\n## Revision\n{(fb.get('revision_request') or {}).get('text')}\n" if fb.get("revision_request") else "")
                        + (f"\n(previous: {len(before)} bytes)\n" if before else ""))
        open(prev, "w", encoding="utf-8").write(body)
        if touch:
            p = os.path.join(ws, "repo", touch)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w", encoding="utf-8").write("changed by the proposer\n")
        if not report:
            return False
        json.dump({"summary": "service running state by process alias", "scope_files": list(scope),
                   "depends_on": list(depends), "decisions": list(decisions), "research": list(research)},
                  open(os.path.join(ws, "devflow", "propose.json"), "w", encoding="utf-8"))
        return True
    return model


def ev_of(r):
    return json.load(open(os.path.join(r["ws"], "devflow", "evaluation.json"), encoding="utf-8"))


def comment(ct, db, body, user="astro3141"):
    c = ct.R(db)["issues"]["3"]["comments"]
    c.append({"id": 900 + len(c), "user": user, "body": body})


def controls_proposal(ct):
    print("")
    print("proposal — drafted before the gate that binds it; the gate approves its exact, current version")
    check, R, build = ct.check, ct.R, ct.build

    # ---------------------------------------------------------------- the main path
    s = ct.Sim(proposal_db(ct))
    try:
        canon0 = R(s.db)["branches"][build.CANONICAL]
        body0 = R(s.db)["issues"]["3"]["body"]
        r = s.run_pass(proposer())
        check("a Foundation whose gate1 binds a proposal: devflow drafts it first, even with implement: human",
              (r["act"].get("do"), (r.get("deliver") or {}).get("delivered")), ("propose", "yes"))
        db = s.db
        head1 = R(db)["branches"].get(BRANCH)
        c1 = R(db)["commits"].get(head1) or {}
        check("the proposal is a commit by devflow-proposer on devflow-proposal/<task>, on the canonical commit",
              (c1.get("author"), c1.get("parents")), ("devflow-proposer", [canon0]))
        added = sorted(k for k in c1.get("files", {}) if R(db)["commits"][canon0]["files"].get(k) != c1["files"][k])
        check("it holds exactly the proposal and devflow's record — nothing else changes",
              added, sorted([MD, REC]))
        check("canonical, the task block and the pull requests are untouched",
              (R(db)["branches"][build.CANONICAL], R(db)["issues"]["3"]["body"] == body0, R(db)["pulls"]),
              (canon0, True, {}))
        rec = json.loads(c1["files"][REC])
        check("the record binds the canonical base, the inputs and the scope; drafted_by is the proposer",
              (rec["canonical_base"], rec["scope_files"], rec["drafted_by"], bool(rec["inputs"]["depends_on"])),
              (canon0, sorted(SCOPE), "devflow-proposer", True))
        check("implementation rounds are not spent on a proposal",
              (r["state"]["history"].get("implement_rounds"), r["state"]["history"]["proposal"]["rounds"]), (None, 1))

        r = s.run_pass(proposer())
        e = ev_of(r)
        check("a re-run with nothing changed drafts nothing: FRESH, no new commit",
              (e["proposal"]["status"], r["act"].get("do"), R(s.db)["branches"][BRANCH]), ("FRESH", "none", head1))
        check("the task block's draft scope differs: gate1 waits for the scope, not for an approval",
              (ct.phase(r), r["state"]["wait"]["reason"], e["gates"][0]["approved"]), ("prepare", "task", False))

        s.edit(lambda db: comment(ct, db, f"/devflow approve gate1 {e['task_digest']}"))
        s.edit(lambda db: ct.set_task(db, scope_files=SCOPE))
        r = s.run_pass(proposer())
        e = ev_of(r)
        check("copying the proposal's scope into the task block does not make the proposal stale",
              (e["proposal"]["status"], r["act"].get("do"), R(s.db)["branches"][BRANCH]), ("FRESH", "none", head1))
        check("an approval of the task digest alone is not an approval of the proposal",
              (e["gates"][0]["approved"], r["state"]["wait"]["need"]),
              (False, f"/devflow approve gate1 {e['proposal_digest']}"))
        digest1 = e["proposal_digest"]

        s.edit(lambda db: comment(ct, db, f"/devflow approve gate1 {digest1}", user="impl-bot"))
        r = s.run_pass(proposer())
        check("someone who is not an approver cannot approve it", ev_of(r)["gates"][0]["approved"], False)
        s.edit(lambda db: comment(ct, db, f"/devflow approve gate1 {digest1}"))
        r = s.run_pass(proposer())
        check("the approver's approval of the exact proposal digest releases gate1",
              (ev_of(r)["gates"][0]["approved"], ct.phase(r) != "prepare"), (True, True))

        s.edit(lambda db: move(ct, db, {"items/kisa/U-02.yaml": "id: U-02\n"}))
        r = s.run_pass(proposer())
        e = ev_of(r)
        check("canonical moving elsewhere keeps the proposal and its approval",
              (e["proposal"]["status"], e["gates"][0]["approved"], e["proposal_digest"]), ("FRESH", True, digest1))

        s.edit(lambda db: move(ct, db, {"evaluator/check_registry.py": "CHECK_REGISTRY = {'x': 1}\n"}))
        r = s.run_pass(proposer())
        e = ev_of(r)
        check("a file the proposal depends on changes: STALE, the old approval no longer counts",
              (e["proposal"]["status"], e["gates"][0]["approved"], ct.phase(r)), ("STALE", False, "prepare"))
        check("and it is drafted again, on top of the previous proposal (its history kept)",
              (r["act"].get("do"), R(s.db)["commits"][R(s.db)["branches"][BRANCH]]["parents"]), ("propose", [head1]))
        head2 = R(s.db)["branches"][BRANCH]
        r = s.run_pass(proposer())
        e = ev_of(r)
        check("the redraft has a new digest; approving the old one approves nothing",
              (e["proposal"]["status"], e["proposal_digest"] != digest1, e["gates"][0]["approved"]), ("FRESH", True, False))

        s.edit(lambda db: comment(ct, db, f"/devflow revise proposal {head1[:12]}\nold one"))
        s.edit(lambda db: comment(ct, db, f"/devflow revise proposal {head2[:12]}\nadd the xinetd case", user="someone"))
        r = s.run_pass(proposer())
        check("a revision request about an older proposal, or from a non-approver, asks nothing",
              (ev_of(r)["proposal"]["status"], r["act"].get("do")), ("FRESH", "none"))
        s.edit(lambda db: comment(ct, db, f"/devflow revise proposal {head2[:12]}\nadd the xinetd case"))
        r = s.run_pass(proposer())
        fb = json.load(open(os.path.join(r["ws"], "devflow", "feedback.json"), encoding="utf-8"))
        head3 = R(s.db)["branches"][BRANCH]
        check("an approver's revision request of the current proposal redrafts it, with the request given to the proposer",
              (r["act"].get("do"), (fb.get("revision_request") or {}).get("text"), "add the xinetd case" in R(s.db)["commits"][head3]["files"][MD]),
              ("propose", "add the xinetd case", True))
        r = s.run_pass(proposer())
        check("the request is answered once: the next pass drafts nothing",
              (ev_of(r)["proposal"]["status"], r["act"].get("do"), R(s.db)["branches"][BRANCH]), ("FRESH", "none", head3))
        log = r["state"]["history"]["proposal"]
        check("the state keeps every proposal devflow pushed, and why",
              (log["pushed"], [x.get("ok") for x in log["log"]]), ([head1, head2, head3], [True, True, True]))

        s.edit(lambda db: ct.set_task(db, goal="Foundation for SRV-174 — reworded"))
        r = s.run_pass(proposer())
        check("the task block changing (not only its scope) makes it stale — and three rounds are spent, so a person decides",
              (ev_of(r)["proposal"]["changes"][:1], r["act"].get("do"), r["state"]["wait"]["reason"]),
              (["the task block changed"], "none", "decision"))
    finally:
        s.close()

    # ---------------------------------------------------------------- after the change lands
    s = ct.Sim(proposal_db(ct))
    try:
        s.run_pass(proposer())
        s.edit(lambda db: ct.set_task(db, scope_files=SCOPE))
        e = ev_of(s.run_pass(proposer()))
        s.edit(lambda db: comment(ct, db, f"/devflow approve gate1 {e['proposal_digest']}"))

        def publish(db):
            rr = R(db)
            c = rr["branches"][build.CANONICAL]
            files = dict(rr["commits"][c]["files"])
            files["evaluator/check_registry.py"] = "CHECK_REGISTRY = {'running': 1}\n"
            rr["commits"]["a" * 40] = {"parents": [c], "files": files, "author": "impl-bot"}
            rr["branches"][build.CANONICAL] = "a" * 40
        s.edit(publish)
        s.edit(lambda db: ct.set_task(db, transaction_commit="a" * 40))
        r = s.run_pass(proposer())
        e = ev_of(r)
        check("a Foundation that changes a file its proposal depends on does not make its own approval stale by landing",
              (e["proposal"]["status"], e["gates"][0]["approved"], r["act"].get("do")), ("FRESH", True, "none"))
    finally:
        s.close()

    # ---------------------------------------------------------------- the proposer's rights
    # As declared for Preloop (principals.yaml). Each allow is `has(args.path) &&
    # args.path.matches('<pattern>')`, first match wins; Python's re.search stands in for CEL's
    # matches() here (both RE2-compatible anchored patterns). Preloop's own evaluator was run over
    # the same cases when this was written (PR #9): the result was the same.
    import re as _re
    rules = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                             "principals.yaml"), encoding="utf-8"))["principals"]["devflow-proposer"]["tool_rules"]
    shape = _re.compile(r"^has\(args\.path\) && args\.path\.matches\('(\^/ws/[^']+\$)'\)$")

    def allowed(tool, path):
        for r in rules[tool]:
            if r["action"] == "deny" and not r.get("condition_expression"):
                return False
            m = shape.match(r.get("condition_expression") or "")
            if r.get("condition_type") != "cel" or not m:
                return None             # a rule this control does not understand: fail the check
            if _re.search(m.group(1), path):
                return r["action"] == "allow"
        return False
    w = "/ws/3f9a1c2b"
    want = {f"{w}/proposal/proposal.md": (True, True), f"{w}/devflow/propose.json": (True, False),
            f"{w}/repo/evaluator/evaluator.py": (False, False), f"{w}/repo/docs/proposal/proposal.md": (False, False),
            f"{w}/proposal/proposal.md/../../repo/x.py": (False, False), f"{w}/devflow/propose.json/../../repo/x.py": (False, False),
            f"{w}/repo/../proposal/proposal.md": (False, False), "/ws/../etc/proposal/proposal.md": (False, False),
            f"{w}/proposal/proposal.md.sh": (False, False), "/tmp/x/proposal/proposal.md": (False, False),
            "proposal/proposal.md": (False, False), f"{w}/devflow/state.json": (False, False)}
    got = {p: (allowed("write_file", p), allowed("edit_file", p)) for p in want}
    check("the proposer may write exactly its proposal and its report — no traversal, no checkout, nothing outside /ws",
          [p for p in want if got[p] != want[p]], [])

    s = ct.Sim(proposal_db(ct))
    try:
        r = s.run_pass(proposer(touch="evaluator/check_registry.py"))
        check("a proposer that changes the checkout: refused, nothing pushed",
              ((r.get("deliver") or {}).get("delivered"), R(s.db)["branches"].get(BRANCH),
               any("changed the checkout" in b for b in r["state"]["blockers"])), ("no", None, True))
        r = s.run_pass(proposer(scope=[]))
        check("a report with no scope is refused", ((r.get("deliver") or {}).get("delivered"), R(s.db)["branches"].get(BRANCH)),
              ("no", None))
        r = s.run_pass(proposer(scope=["../outside"]))
        check("a scope path outside the repository is refused", (r.get("deliver") or {}).get("delivered"), "no")
    finally:
        s.close()

    s = ct.Sim(proposal_db(ct))
    try:
        s.run_pass(proposer())

        def foreign(db):
            rr = R(db)
            h = rr["branches"][BRANCH]
            files = dict(rr["commits"][h]["files"], **{MD: "rewritten by someone\n"})
            rr["commits"]["f" * 40] = {"parents": [h], "files": files, "author": "devflow-proposer"}
            rr["branches"][BRANCH] = "f" * 40
        s.edit(foreign)
        r = s.run_pass(proposer())
        e = ev_of(r)
        check("a commit devflow did not push on the proposal branch: INVALID, nothing is drafted over it",
              (e["proposal"]["status"], r["act"].get("do"), e["gates"][0]["approved"]), ("INVALID", "none", False))
    finally:
        s.close()

    # ---------------------------------------------------------------- its own round limit
    s = ct.Sim(proposal_db(ct, max_rounds=1))
    try:
        s.run_pass(proposer())
        s.edit(lambda db: move(ct, db, {"docs/TECHNICAL_DESIGN_v0.3.md": "# TD v2\n"}))
        r = s.run_pass(proposer())
        e = ev_of(r)
        check("past propose.max_rounds a stale proposal waits for a person's grant",
              (r["act"].get("do"), r["state"]["wait"]["reason"],
               r["state"]["wait"]["need"].startswith(f"`/devflow approve propose {e['proposal']['inputs_digest']}`")),
              ("none", "decision", True))
        s.edit(lambda db: comment(ct, db, f"/devflow approve propose {e['proposal']['inputs_digest']}"))
        r = s.run_pass(proposer())
        check("the grant buys one more drafting round", r["act"].get("do"), "propose")
        r = s.run_pass(proposer())
        check("and only one: the spent grant is kept, and nothing more is drafted",
              (r["act"].get("do"), ev_of(r)["proposal"]["cap"], r["state"]["history"]["proposal"]["grants_spent"]),
              ("none", 2, [e["proposal"]["inputs_digest"]]))
        s.edit(lambda db: move(ct, db, {"docs/TECHNICAL_DESIGN_v0.3.md": "# TD v3\n"}))
        r = s.run_pass(proposer())
        check("a stale proposal after the spent grant waits for a new one (spent rounds still counted)",
              (r["act"].get("do"), r["state"]["wait"]["reason"], ev_of(r)["proposal"]["cap"]), ("none", "decision", 2))
    finally:
        s.close()

    # A grant is for the inputs it names. One given for inputs A and not yet used does not pay for a
    # round once the inputs became B; B needs a grant of its own.
    s = ct.Sim(proposal_db(ct, max_rounds=1))
    try:
        s.run_pass(proposer())
        s.edit(lambda db: move(ct, db, {"docs/TECHNICAL_DESIGN_v0.3.md": "# TD A\n"}))
        a = ev_of(s.run_pass(proposer()))["proposal"]["inputs_digest"]
        s.edit(lambda db: comment(ct, db, f"/devflow approve propose {a}"))
        s.edit(lambda db: move(ct, db, {"docs/TECHNICAL_DESIGN_v0.3.md": "# TD B\n"}))
        r = s.run_pass(proposer())
        b = ev_of(r)["proposal"]["inputs_digest"]
        check("a grant for inputs A, unused when the inputs became B, does not draft B",
              (a != b, r["act"].get("do"), r["state"]["wait"]["reason"],
               r["state"]["wait"]["need"].startswith(f"`/devflow approve propose {b}`")),
              (True, "none", "decision", True))
        check("and it is not spent by that pass", r["state"]["history"]["proposal"].get("grants_spent") or [], [])
        s.edit(lambda db: comment(ct, db, f"/devflow approve propose {b}"))
        r = s.run_pass(proposer())
        check("a grant for B drafts B, and is the one spent",
              (r["act"].get("do"), (r.get("deliver") or {}).get("delivered"), r["state"]["history"]["proposal"]["grants_spent"]),
              ("propose", "yes", [b]))
        s.edit(lambda db: comment(ct, db, f"/devflow approve propose {b}"))
        s.edit(lambda db: move(ct, db, {"docs/TECHNICAL_DESIGN_v0.3.md": "# TD C\n"}))
        r = s.run_pass(proposer())
        check("grants for B, however many, do not draft the next inputs C",
              (r["act"].get("do"), r["state"]["wait"]["reason"]), ("none", "decision"))
    finally:
        s.close()

    # ---------------------------------------------------------------- what it asks
    s = ct.Sim(proposal_db(ct))
    try:
        q = {"id": "Q-T-0054-P1", "question": "which process names answer DNS on el8?", "why": "G3",
             "scope": "el8", "known": "-", "need": "package index", "accept": "names with sources"}
        s.run_pass(proposer(research=[q]))
        s.edit(lambda db: ct.set_task(db, scope_files=SCOPE))
        r = s.run_pass(proposer(research=[q]))
        check("a proposal that asks research waits for it, unapprovable",
              (r["state"]["wait"]["reason"], ev_of(r)["gates"][0]["approved"], r["act"].get("do")), ("research", False, "none"))
        s.edit(lambda db: move(ct, db, {"docs/research/dns.md": "# DNS\nanswers: Q-T-0054-P1\n"}))
        r = s.run_pass(proposer())
        check("the merged answer makes it stale: drafted again with the answer",
              (ev_of(r)["proposal"]["status"], r["act"].get("do")), ("STALE", "propose"))
    finally:
        s.close()

    s = ct.Sim(proposal_db(ct))
    try:
        s.run_pass(proposer(decisions=["count dnsmasq as a DNS service? yes / no"]))
        s.edit(lambda db: ct.set_task(db, scope_files=SCOPE))
        r = s.run_pass(proposer())
        check("a proposal that leaves a decision: NEEDS_DECISION, unapprovable",
              (ct.verdict(r), ev_of(r)["gates"][0]["approved"]), ("NEEDS_DECISION", False))
    finally:
        s.close()

    # ---------------------------------------------------------------- optional elsewhere
    db = ct.impl_db()
    ct.edit_policy(db, lambda p: p.__setitem__("propose", {"by": "agent", "required_for": ["FOUNDATION"]}), head_too=False)
    s = ct.Sim(db)
    try:
        r = s.run_pass(ct.good_model)
        check("an ordinary task is not made to propose: prepared and implemented as before",
              (r["act"].get("do"), ev_of(r)["proposal"]["active"]), ("prepare", False))
    finally:
        s.close()
    db = ct.impl_db()
    ct.edit_policy(db, lambda p: p.__setitem__("propose", {"by": "agent", "required_for": ["FOUNDATION"]}), head_too=False)
    ct.set_task(db, proposal=True)
    s = ct.Sim(db)
    try:
        r = s.run_pass(proposer())
        check("an ordinary task that asks for a proposal where no gate binds one: a task problem, nothing drafted",
              (r["act"].get("do"), r["state"]["wait"]["reason"],
               any("no prepare-phase gate" in b for b in r["state"]["blockers"])), ("none", "task", True))
    finally:
        s.close()
    db = ct.impl_db()
    ct.edit_policy(db, lambda p: (p.__setitem__("propose", {"by": "agent", "required_for": ["FOUNDATION"]}),
                                  p["human_gates"].__setitem__("ORDINARY", [{"id": "plan", "phase": "prepare", "binds": "proposal"}])),
                   head_too=False)
    ct.set_task(db, proposal=True)
    s = ct.Sim(db)
    try:
        r = s.run_pass(proposer())
        check("an ordinary task may opt in where the project gives it a gate that binds a proposal",
              (r["act"].get("do"), (r.get("deliver") or {}).get("delivered")), ("propose", "yes"))
    finally:
        s.close()
