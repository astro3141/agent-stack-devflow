"""Issue draft generator (workflow devflow-draft) — real steps, fixture GitHub, deterministic drafter.

Driven from controls.py (`controls_draft(controls)`). What it pins:
  * gather reads the canonical frame, the template, the criteria and every issue through a remote that
    refuses writes; the fixture remote's file is byte-for-byte the same after every scenario
  * existing work is found without a model (a shared item or task code, a named #n): a draft that
    ignores it, or calls itself new while saying an open issue already covers it, is refused
  * the task block is parsed by devflow's own parser and held to the policy's vocabulary; a task id
    already in use is refused
  * a change class needs evidence at the canonical commit; without it the class stays unresolved
    (open, for a person), and the project's hook refuses a class below the floor its paths imply
  * every fact has a source a person can open; every unknown is research, a contract gap or a decision
  * the prose claims no state nobody verified; a claim word inside a code block is not a claim
  * a drafter that changes the checkout, or produces nothing, gives INVALID — never a draft

Integration controls of the machinery, not a claim that a live model or live GitHub ran.
"""
import hashlib
import json
import os

TEMPLATE = "## 해결할 문제\n\n## 완료 기준\n\n```devflow\nid:\ntype:\ngoal:\n```\n"
FOUNDATION_TASK = """## 해결할 문제
SRV-174 running state.

```devflow
id: T-F-SVCRUN
type: feature
goal: service running state for SRV-174
change_class: FOUNDATION
items: [SRV-174]
implement: human
```
"""


def draft_db(ct):
    db = ct.build.scanner_pass()
    r = ct.R(db)
    c = r["branches"][ct.build.CANONICAL]
    files = dict(r["commits"][c]["files"], **{".devflow/templates/task.md": TEMPLATE})
    new = "c" + ct.build.sha(json.dumps([c, files], sort_keys=True))[:39]
    r["commits"][new] = {"parents": [c], "files": files, "author": "astro3141"}
    r["branches"][ct.build.CANONICAL] = new
    r["issues"]["5"] = {"title": "SRV-174 Foundation", "body": FOUNDATION_TASK, "labels": [], "state": "open",
                        "comments": [{"id": 51, "user": "astro3141", "body": "D9: decided — every DNS service counts"},
                                     {"id": 52, "user": "devflow", "body": "waiting: decision D9 · owner: a person\n\n<!-- devflow:state {\"machinery\": 1} -->"}]}
    r["issues"]["6"] = {"title": "old note", "body": "no task block here", "labels": [], "comments": [], "state": "closed"}
    return db


def block(**f):
    lines = []
    for k, v in f.items():
        if v is None:
            continue
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v}")
    return "```devflow\n" + "\n".join(lines) + "\n```\n"


ORD_BLOCK = dict(id="T-0002", type="feature", goal="implement KISA U-02 with the existing config_kv primitive",
                 change_class="ORDINARY", impacts=["item_changed"], items=["U-02"])
ORD_REPORT = {"title": "U-02 password complexity", "relation": "new", "related": [],
              "classification": {"change_class": "ORDINARY", "status": "candidate",
                                 "evidence": [{"path": "primitives.yaml", "why": "config_kv exists; no new primitive"}],
                                 "alternatives": []},
              "scope_candidates": ["items/kisa/U-02.yaml", "bindings/linux/u02.yaml"],
              "facts": [{"text": "the policy names the KISA guide as a criterion", "source": ".devflow/project.yaml"},
                        {"text": "U-54 is already a task", "source": "#3"}],
              "inferences": [{"text": "an item-only change", "from": ["primitives.yaml"]}],
              "unknowns": []}


def drafter(text=None, report=None, fields=None, prose="## 해결할 문제\nU-02 비밀번호 복잡성.\n\n", touch=None, produce=True):
    """What a drafter writes: draft/issue.md and devflow/draft.json in its workspace."""
    def model(ws):
        os.makedirs(os.path.join(ws, "draft"), exist_ok=True)
        body = text if text is not None else prose + block(**(fields if fields is not None else ORD_BLOCK))
        open(os.path.join(ws, "draft", "issue.md"), "w", encoding="utf-8").write(body)
        if touch:
            open(os.path.join(ws, "repo", touch), "w", encoding="utf-8").write("changed by the drafter\n")
        if not produce:
            return False
        json.dump(report if report is not None else ORD_REPORT,
                  open(os.path.join(ws, "devflow", "draft.json"), "w", encoding="utf-8"), ensure_ascii=False)
        return True
    return model


def controls_draft(ct):
    print("\n[draft] a request → a checked draft of a task issue; reads only, posts nothing")
    import secrets
    sim = ct.Sim(draft_db(ct))
    digest = lambda: hashlib.sha256(open(sim.path, "rb").read()).hexdigest()
    before = digest()

    def run(request, model):
        rid = "ctl-" + secrets.token_hex(4)
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": rid}
        ws = os.path.join(ct.WS_ROOT, rid)
        sim.runs.append(ws)
        g = ct.step("draft_gather.py", ["scanner-fixture", request, sim.name], env)
        produced = bool(model(ws)) if g.get("status") == "OK" else False
        c = ct.step("draft_check.py", ["scanner-fixture", str(produced), "run-draft"], env) if g.get("status") == "OK" else {}
        rep = {}
        try:
            rep = json.load(open(os.path.join(ws, "devflow", "draft_report.json"), encoding="utf-8"))
        except Exception:
            pass
        ctx = {}
        try:
            ctx = json.load(open(os.path.join(ws, "devflow", "draft_context.json"), encoding="utf-8"))
        except Exception:
            pass
        return g, c, rep, ctx, ws

    def has(rep, key, text):
        return any(text in x for x in rep.get(key) or [])

    try:
        # --- an ordinary request, drafted soundly
        g, c, rep, ctx, ws = run("KISA U-02 비밀번호 복잡성 점검을 추가해줘", drafter())
        ct.check("draft: gather reads the frame, the template and every issue", (g.get("status"), g.get("issues"),
                 ctx.get("template", {}).get("present"), ctx.get("canonical") == ct.R(sim.db)["branches"][ct.build.CANONICAL]),
                 ("OK", 3, True, True))
        ct.check("draft: the prompt carries the template and the existing issues",
                 all(s in open(g["prompt"], encoding="utf-8").read() for s in ("## 완료 기준", "T-F-SVCRUN", "T-0054")), True)
        ct.check("draft: an issue with no task block is listed with why", [r.get("task_problem", "")[:20] for r in ctx["issues"] if r["number"] == 6],
                 ["the issue has no ```"])
        ct.check("draft: a sound ordinary draft is DRAFTED (never 'ready')", (c.get("status"), c.get("verdict"), rep.get("blocking")),
                 ("OK", "DRAFTED", []))
        ct.check("draft: the report names the canonical SHA and the parsed task", (rep.get("canonical") == ctx["canonical"],
                 (rep.get("task") or {}).get("id")), (True, "T-0002"))
        ct.check("draft: the human report is in the workspace", os.path.isfile(os.path.join(ws, "draft", "report.md")), True)

        # --- existing work
        g, c, rep, ctx, _ = run("U-54 FTP 점검을 다시 만들어줘", drafter())
        ct.check("draft: a shared item code finds the existing issue without a model",
                 [(x["number"], x["strength"]) for x in ctx.get("candidates") or []][:1], [(3, "strong")])
        ct.check("draft: a draft that ignores a strong candidate is INVALID",
                 (c.get("verdict"), has(rep, "blocking", "#3 was found mechanically")), ("INVALID", True))
        upd = dict(ORD_REPORT, relation="update", related=[{"number": 3, "relation": "update", "why": "same item U-54, open"}])
        g, c, rep, _, _ = run("U-54 FTP 점검을 다시 만들어줘", drafter(fields=dict(ORD_BLOCK, id="T-0054"), report=upd))
        ct.check("draft: proposing an update of the existing issue is accepted (its id may be reused)",
                 (c.get("verdict"), rep.get("blocking")), ("DRAFTED", []))
        dup_new = dict(ORD_REPORT, related=[{"number": 3, "relation": "duplicate", "why": "same item"}])
        g, c, rep, _, _ = run("U-54 FTP 점검을 다시 만들어줘", drafter(report=dup_new))
        ct.check("draft: 'new' while an open issue is said to be duplicated is INVALID",
                 has(rep, "blocking", "relation is new, yet open issue(s) [3]"), True)
        g, c, rep, _, _ = run("#5 관련 후속 작업", drafter(fields=dict(ORD_BLOCK, id="T-0054")))
        prompt = open(g["prompt"], encoding="utf-8").read()
        ct.check("draft: a strong candidate's body, discussion and devflow's published sentences reach the drafter — not its JSON",
                 ("SRV-174 running state." in prompt, "D9: decided" in prompt, "waiting: decision D9" in prompt, "machinery" in prompt),
                 (True, True, True, False))
        ct.check("draft: a named #n is a strong candidate; a new task reusing an existing id is INVALID",
                 (has(rep, "blocking", "#5 was found mechanically"), has(rep, "blocking", "already the task of ['#3']")), (True, True))
        unrel = dict(ORD_REPORT, related=[{"number": 5, "relation": "unrelated", "why": "different item"}])
        g, c, rep, _, _ = run("#5 관련 후속 작업", drafter(report=unrel))
        ct.check("draft: a strong candidate called unrelated is open for a person", (c.get("verdict"), has(rep, "open", "#5 shares")),
                 ("NEEDS_INPUT", True))
        dupe = {"title": "dup", "relation": "duplicate", "related": [{"number": 3, "relation": "duplicate", "why": "U-54 is #3"}],
                "classification": {"change_class": None, "status": "unresolved", "evidence": [], "alternatives": []},
                "facts": [], "inferences": [], "unknowns": []}
        g, c, rep, _, _ = run("U-54 FTP 점검", drafter(text="#3 이 이미 같은 일을 합니다.\n", report=dupe))
        ct.check("draft: a duplicate needs no task block", (c.get("verdict"), rep.get("blocking")), ("DRAFTED", []))

        # --- the task block, by devflow's parser and the policy's vocabulary
        for name, fields, want in (("an unknown type", dict(ORD_BLOCK, type="chore"), "type 'chore' is not one of"),
                                   ("a missing goal", dict(ORD_BLOCK, goal=None), "task block: the task block has no 'goal'"),
                                   ("an unknown impact", dict(ORD_BLOCK, impacts=["vibes"]), "impacts ['vibes'] are not among")):
            g, c, rep, _, _ = run("KISA U-02 비밀번호 복잡성", drafter(fields=fields))
            ct.check(f"draft: {name} in the block is INVALID", (c.get("verdict"), has(rep, "blocking", want)), ("INVALID", True))
        g, c, rep, _, _ = run("KISA U-02", drafter(text="no block at all\n"))
        ct.check("draft: a new task without a block is INVALID", has(rep, "blocking", "has 0 ```devflow blocks"), True)

        # --- classes need evidence; the hook's floor holds
        noev = dict(ORD_REPORT, classification={"change_class": "ORDINARY", "status": "candidate", "evidence": [], "alternatives": []})
        g, c, rep, _, _ = run("KISA U-02", drafter(report=noev))
        ct.check("draft: a class in the block without evidence is INVALID", has(rep, "blocking", "without evidence"), True)
        ghost = dict(ORD_REPORT, classification={"change_class": "ORDINARY", "status": "candidate",
                                                 "evidence": [{"path": "no/such.py", "why": "x"}], "alternatives": []})
        g, c, rep, _, _ = run("KISA U-02", drafter(report=ghost))
        ct.check("draft: evidence that is not at the canonical commit is INVALID", has(rep, "blocking", "not at the canonical commit"), True)
        unres = dict(ORD_REPORT, classification={"change_class": None, "status": "unresolved", "evidence": [],
                                                 "alternatives": ["FOUNDATION if a new primitive is needed"]})
        g, c, rep, _, _ = run("KISA U-02", drafter(report=unres, fields=dict(ORD_BLOCK, change_class=None)))
        ct.check("draft: an unresolved class stays out of the block and is open for a person",
                 (c.get("verdict"), has(rep, "open", "change class unresolved: FOUNDATION if"), rep.get("blocking")), ("NEEDS_INPUT", True, []))
        floor = dict(ORD_REPORT, scope_candidates=["primitives.yaml", "items/kisa/U-02.yaml"])
        g, c, rep, _, _ = run("KISA U-02", drafter(report=floor))
        ct.check("draft: ORDINARY over a frozen registry is refused by the project's hook",
                 (c.get("verdict"), has(rep, "blocking", "hook: declared ORDINARY")), ("INVALID", True))
        fnd = dict(floor, classification={"change_class": "FOUNDATION", "status": "candidate",
                                          "evidence": [{"path": "primitives.yaml", "why": "a frozen registry changes"}], "alternatives": []})
        g, c, rep, _, _ = run("KISA U-02", drafter(report=fnd, fields=dict(ORD_BLOCK, change_class="FOUNDATION", impacts=["contract_changed"])))
        ct.check("draft: a Foundation with no scope_files is open (the hook's problem), not refused",
                 (c.get("verdict"), has(rep, "open", "hook: a Foundation task lists its exact approved files")), ("NEEDS_INPUT", True))

        # --- facts, inferences, unknowns
        for name, facts, want in (("no source", [{"text": "x"}], "fact without a source"),
                                  ("a missing file", [{"text": "x", "source": "nowhere.md"}], "fact without a source"),
                                  ("an issue that does not exist", [{"text": "x", "source": "#99"}], "fact without a source")):
            g, c, rep, _, _ = run("KISA U-02", drafter(report=dict(ORD_REPORT, facts=facts)))
            ct.check(f"draft: a fact with {name} is INVALID", has(rep, "blocking", want), True)
        g, c, rep, _, _ = run("KISA U-02", drafter(report=dict(ORD_REPORT, facts=[{"text": "x", "source": "primitives.yaml:1"}])))
        ct.check("draft: a fact sourced path:line is accepted", rep.get("blocking"), [])
        g, c, rep, _, _ = run("KISA U-02", drafter(report=dict(ORD_REPORT, inferences=[{"text": "a guess"}])))
        ct.check("draft: an inference that names nothing it rests on is INVALID", has(rep, "blocking", "inference that says nothing"), True)
        unk = [{"text": "does PAM read X on Rocky 8", "kind": "research", "why": "w"},
               {"text": "TD says nothing of Y", "kind": "contract_gap", "why": "w"},
               {"text": "is Z an exception", "kind": "decision", "why": "w"}]
        g, c, rep, _, _ = run("KISA U-02", drafter(report=dict(ORD_REPORT, unknowns=unk)))
        ct.check("draft: research, contract gap and decision are told apart, open for a person",
                 (c.get("verdict"), [o.split(":")[0] for o in rep.get("open") or []]), ("NEEDS_INPUT", ["research", "contract_gap", "decision"]))
        g, c, rep, _, _ = run("KISA U-02", drafter(report=dict(ORD_REPORT, unknowns=[{"text": "?", "kind": "guess"}])))
        ct.check("draft: an unknown of no known kind is INVALID", has(rep, "blocking", "unknown whose kind"), True)

        # --- no claim of an unverified state
        for word in ("구현 준비 완료입니다.", "The design is confirmed.", "분류가 확정되었다."):
            g, c, rep, _, _ = run("KISA U-02", drafter(prose=f"## 해결할 문제\n{word}\n\n"))
            ct.check(f"draft: the claim {word!r} is INVALID", has(rep, "blocking", "claims a state nobody verified"), True)
        g, c, rep, _, _ = run("KISA U-02", drafter(prose="## 해결할 문제\nU-02.\n\n```yaml\nbaseline_status: confirmed\n```\n\n"))
        ct.check("draft: a claim word inside a code block is not a claim", rep.get("blocking"), [])
        g, c, rep, _, _ = run("KISA U-02", drafter(prose="## 해결할 문제\n공식 기준 (`items/kisa/U-01.yaml`, `baseline_status: confirmed`):\n\n"))
        ct.check("draft: a claim word quoted as inline code is not a claim", rep.get("blocking"), [])
        g, c, rep, _, _ = run("KISA U-02", drafter(prose="## 해결할 문제\n`U-02` is confirmed.\n\n"))
        ct.check("draft: … while the prose around inline code is still read", has(rep, "blocking", "claims a state nobody verified"), True)

        # --- the drafter's own failures
        g, c, rep, _, _ = run("KISA U-02", drafter(touch="primitives.yaml"))
        ct.check("draft: a drafter that changes the checkout gives INVALID", has(rep, "blocking", "changed the checkout"), True)
        g, c, rep, _, _ = run("KISA U-02", drafter(produce=False))
        ct.check("draft: a drafter that produced nothing gives INVALID", (c.get("verdict"), has(rep, "blocking", "produced no report")),
                 ("INVALID", True))
        g, c, rep, _, _ = run("   ", drafter())
        ct.check("draft: an empty request is refused before anything is read", g.get("status"), "REFUSED")

        ct.check("draft: gather asked the remote for no write in any scenario", ctx.get("remote_refused"), [])
        ct.check("draft: the fixture remote is byte-for-byte unchanged after every scenario", digest() == before, True)
    finally:
        sim.close()

    # --- the read-only remote itself
    import remote as remote_mod
    db = draft_db(ct)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "r.json")
        json.dump(db, open(p, "w", encoding="utf-8"))
        h0 = hashlib.sha256(open(p, "rb").read()).hexdigest()
        ro = remote_mod.ReadOnly(remote_mod.FixtureRemote(p, ct.build.REPO))
        got = []
        for name, args in (("add_comment", (3, "x")), ("edit_comment", (1, "x")), ("set_labels", (3, ["x"])),
                           ("create_pull", ("a", "b", "t", "b")), ("push_commit", ("b", "p", {}, "m", "a")),
                           ("merge", (7, "s", "merge")), ("create_ref", ("r", "s")), ("set_status", ("s", "c", "success", "d")),
                           ("some_future_write", ())):
            try:
                getattr(ro, name)(*args)
                got.append((name, "ran"))
            except remote_mod.RemoteError:
                got.append((name, "refused"))
        ct.check("draft: the read-only remote refuses every write, and any unlisted name", {s for _, s in got}, {"refused"})
        ct.check("draft: … and records what was refused", len(ro.refused), 9)
        ct.check("draft: … while reads pass through", (len(ro.issues()), ro.issue(5)["title"]), (3, "SRV-174 Foundation"))
        ct.check("draft: … and the remote's file did not change", hashlib.sha256(open(p, "rb").read()).hexdigest() == h0, True)
