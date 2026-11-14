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
  * what the work would change (scope_candidates — the hook's changed files) and what it only reads
    (reference_files — never the hook's) are told apart, and the issue body's scope section says the
    same: a protected file the work would change is refused for ORDINARY and cannot pass as a reference
  * the prose claims no state nobody verified; a claim word inside a code block is not a claim, and a
    negated one ("승인된 범위 아님", "not approved") says what is not so — each occurrence on its own
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
    files = dict(r["commits"][c]["files"], **{".devflow/templates/task.md": TEMPLATE,
                 "docs/implementation/linux-item-implementation-matrix.md": "| U-18 | 정식구현 |\n",
                 "items/kisa/U-18.yaml": "id: U-18\n"})
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


def scope_md(paths):
    return "\n<!-- devflow:scope -->\n" + ("".join(f"- `{p}` — changes\n" for p in paths) or "- (없음)\n") \
        + "<!-- /devflow:scope -->\n"


def drafter(text=None, report=None, fields=None, prose="## 해결할 문제\nU-02 비밀번호 복잡성.\n\n", touch=None, produce=True,
            scope=None):
    """What a drafter writes: draft/issue.md and devflow/draft.json in its workspace. The body states
    the report's scope candidates as its scope section, unless `scope` (the section, or "") is given."""
    def model(ws):
        os.makedirs(os.path.join(ws, "draft"), exist_ok=True)
        said = scope if scope is not None else scope_md((report if report is not None else ORD_REPORT).get("scope_candidates") or [])
        body = text if text is not None else prose + block(**(fields if fields is not None else ORD_BLOCK)) + said
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

        # --- what changes and what is only read (U-18: the implementation matrix is read, not changed)
        MATRIX = "docs/implementation/linux-item-implementation-matrix.md"
        U18 = dict(ORD_REPORT, scope_candidates=["items/kisa/U-18.yaml", "tests/test_u18_shadow_perm.py"],
                   reference_files=[MATRIX, "docs/TECHNICAL_DESIGN_v0.3.md"])
        U18_BLOCK = dict(ORD_BLOCK, id="T-U18", goal="align KISA U-18 with its criterion", items=["U-18"])
        U18_PROSE = "## 해결할 문제\nU-18. 후보 범위(승인된 범위 아님). 구현 매트릭스(`" + MATRIX + "`)는 참고만 한다.\n\n"
        g, c, rep, _, ws = run("KISA U-18 /etc/shadow", drafter(report=U18, fields=U18_BLOCK, prose=U18_PROSE))
        ct.check("draft U-18: the matrix as a reference is not a changed file — the hook passes, nothing is blocked",
                 (c.get("verdict"), rep.get("blocking"), rep.get("scope_candidates"), rep.get("reference_files")),
                 ("DRAFTED", [], U18["scope_candidates"], U18["reference_files"]))
        ct.check("draft U-18: the report says what the hook read and what it did not",
                 (has(rep, "notes", "reference files (read, not changed; not given to the hook): " + MATRIX),
                  "## 참고 파일 (읽기만, 변경 없음)" in open(os.path.join(ws, "draft", "report.md"), encoding="utf-8").read()),
                 (True, True))
        chg = dict(U18, scope_candidates=U18["scope_candidates"] + [MATRIX], reference_files=["docs/TECHNICAL_DESIGN_v0.3.md"])
        g, c, rep, _, _ = run("KISA U-18 /etc/shadow", drafter(report=chg, fields=U18_BLOCK, prose=U18_PROSE))
        ct.check("draft U-18: ORDINARY that would change the matrix is still refused by the hook",
                 (c.get("verdict"), has(rep, "blocking", "hook: an ordinary change may not touch authority documents")),
                 ("INVALID", True))
        hidden = dict(U18)
        g, c, rep, _, _ = run("KISA U-18 /etc/shadow", drafter(report=hidden, fields=U18_BLOCK, prose=U18_PROSE,
                                                                 scope=scope_md(U18["scope_candidates"] + [MATRIX])))
        ct.check("draft U-18: a change the body states cannot pass as a reference (the hook is not skipped)",
                 (c.get("verdict"), has(rep, "blocking", "the issue body's scope names ['" + MATRIX + "'], which scope_candidates does not (it lists them as reference files)")),
                 ("INVALID", True))
        g, c, rep, _, _ = run("KISA U-18 /etc/shadow", drafter(report=dict(chg, reference_files=[MATRIX]), fields=U18_BLOCK, prose=U18_PROSE))
        ct.check("draft U-18: a file in both lists is refused, and the hook still reads it as changed",
                 (has(rep, "blocking", "are both a scope candidate and a reference file"),
                  has(rep, "blocking", "hook: an ordinary change may not touch authority documents")), (True, True))
        for name, report, said, want in (
                ("a scope candidate the body does not state", U18, scope_md(["items/kisa/U-18.yaml"]),
                 "scope_candidates names ['tests/test_u18_shadow_perm.py'], which the issue body's scope does not"),
                ("no scope section", U18, "", "the issue body has 0 scope sections"),
                ("two scope sections", U18, scope_md(U18["scope_candidates"]) * 2, "the issue body has 2 scope sections"),
                ("a scope section never closed", U18, scope_md(U18["scope_candidates"]).replace("<!-- /devflow:scope -->", ""),
                 "scope markers: <!-- devflow:scope --> is never closed"),
                ("a close marker before its open", U18, "\n<!-- /devflow:scope -->" + scope_md(U18["scope_candidates"]),
                 "scope markers: <!-- /devflow:scope --> without"),
                ("a @1 body (no scope section): drafted again, not re-checked", U18, "", "a draft written before it is drafted again"),
                ("a reference not at the canonical commit", dict(U18, reference_files=["docs/nowhere.md"]), None,
                 "reference files not at the canonical commit: ['docs/nowhere.md']"),
                ("reference_files that is not a list of paths", dict(U18, reference_files="docs"), None,
                 "reference_files is not a list of paths")):
            g, c, rep, _, _ = run("KISA U-18 /etc/shadow", drafter(report=report, fields=U18_BLOCK, prose=U18_PROSE, scope=said))
            ct.check(f"draft U-18: {name} is INVALID", (c.get("verdict"), has(rep, "blocking", want)), ("INVALID", True))
        # the official criterion and the Foundation contracts are not changed under ORDINARY
        PDF = "docs/baselines/current/주요정보통신기반시설 기술적 취약점 분석·평가 방법 상세가이드.pdf"
        r = dict(U18, scope_candidates=["items/kisa/U-18.yaml", PDF], reference_files=[MATRIX])
        g, c, rep, _, _ = run("KISA U-18 /etc/shadow", drafter(report=r, fields=U18_BLOCK, prose=U18_PROSE))
        ct.check("draft U-18: an official criterion file in an ORDINARY scope is never DRAFTED — a person's decision "
                 "(the project's hook only flags criterion_changed)",
                 (c.get("verdict"), has(rep, "open", "decision: the scope would change the official criterion file(s)"),
                  has(rep, "notes", "criterion_changed")), ("NEEDS_INPUT", True, True))
        for name, path, want in (("a Foundation contract (TD)", "docs/TECHNICAL_DESIGN_v0.3.md", "hook: an ordinary change may not touch authority documents"),
                                 ("a frozen registry", "primitives.yaml", "hook: declared ORDINARY")):
            r = dict(U18, scope_candidates=["items/kisa/U-18.yaml", path], reference_files=[MATRIX])
            g, c, rep, _, _ = run("KISA U-18 /etc/shadow", drafter(report=r, fields=U18_BLOCK, prose=U18_PROSE))
            ct.check(f"draft U-18: changing {name} under ORDINARY is refused, and is a person's decision besides",
                     (c.get("verdict"), has(rep, "blocking", want), has(rep, "open", "decision: the scope would change the contract file(s)")),
                     ("INVALID", True, True))
        legacy = {k: v for k, v in ORD_REPORT.items() if k != "reference_files"}
        g, c, rep, _, _ = run("KISA U-02", drafter(report=legacy))
        ct.check("draft: a report without reference_files (devflow/draft@1) still reads — its references are none",
                 (c.get("verdict"), rep.get("reference_files"), rep.get("schema")), ("DRAFTED", [], "devflow/draft@2"))

        # --- a negated claim word says what is not so; each occurrence is read on its own
        for line in ("후보 범위(승인된 범위 아님): 아래 파일.", "승인되지 않은 초안이다.", "이 범위는 승인된 것이 아니다.",
                     "검증 완료되지 않음.", "이 문서는 초안이며, 승인된 범위가 아닙니다.", "The scope is not approved.", "This has not been verified."):
            g, c, rep, _, _ = run("KISA U-02", drafter(prose=f"## 해결할 문제\n{line}\n\n"))
            ct.check(f"draft: the negated {line!r} is not a claim", (c.get("verdict"), rep.get("blocking")), ("DRAFTED", []))
        for line in ("후보 범위(승인된 범위 아님), 그러나 D-1은 승인됨.", "Not approved, but verified by CI.",
                     "승인된 작업이며 아직 검증되지 않았다.", "D-1 승인됨 (승인된 범위 아님)", "범위는 승인됨.",
                     "승인된 작업은 문제가 없음.", "승인된 범위로 진행해도 문제없음.",
                     "This workflow will not change the approved scope."):
            g, c, rep, _, _ = run("KISA U-02", drafter(prose=f"## 해결할 문제\n{line}\n\n"))
            ct.check(f"draft: the claim in {line!r} is still INVALID", has(rep, "blocking", "claims a state nobody verified"), True)

        # --- the drafter's own failures
        g, c, rep, _, _ = run("KISA U-02", drafter(touch="primitives.yaml"))
        ct.check("draft: a drafter that changes the checkout gives INVALID", has(rep, "blocking", "changed the checkout"), True)
        g, c, rep, _, _ = run("KISA U-02", drafter(produce=False))
        ct.check("draft: a drafter that produced nothing gives INVALID", (c.get("verdict"), has(rep, "blocking", "produced no report")),
                 ("INVALID", True))
        # a repeat in the same workspace: the earlier report is gone before the drafter runs again
        rid = "ctl-" + secrets.token_hex(4)
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": rid}
        ws2 = os.path.join(ct.WS_ROOT, rid)
        sim.runs.append(ws2)
        ct.step("draft_gather.py", ["scanner-fixture", "KISA U-02", sim.name], env)
        drafter()(ws2)
        ct.step("draft_gather.py", ["scanner-fixture", "KISA U-02", sim.name], env)
        c = ct.step("draft_check.py", ["scanner-fixture", "True", "run-draft"], env)
        rep = json.load(open(os.path.join(ws2, "devflow", "draft_report.json"), encoding="utf-8"))
        ct.check("draft: a repeated gather clears the earlier report — an old draft is not this attempt's",
                 (os.path.exists(os.path.join(ws2, "devflow", "draft.json")), c.get("verdict"), has(rep, "blocking", "produced no report")),
                 (False, "INVALID", True))
        g, c, rep, _, _ = run("   ", drafter())
        ct.check("draft: an empty request is refused before anything is read", g.get("status"), "REFUSED")

        # --- the request as a hand-in file: what the stack's inputs cannot carry (any language, any length)
        def gather_file(request, name, content=None):
            rid = "ctl-" + secrets.token_hex(4)
            env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": rid}
            ws = os.path.join(ct.WS_ROOT, rid)
            sim.runs.append(ws)
            path = os.path.join(ct.HANDOFF_ROOT, name) if content is not None else None
            if path:
                open(path, "wb").write(content)
            try:
                g = ct.step("draft_gather.py", ["scanner-fixture", request, sim.name, name], env)
            finally:
                if path and os.path.exists(path):
                    os.remove(path)
            try:
                ctx = json.load(open(os.path.join(ws, "devflow", "draft_context.json"), encoding="utf-8"))
            except Exception:
                ctx = {}
            return g, ctx

        fname = "ctl-req-" + secrets.token_hex(4) + ".md"
        korean = "SRV-174를 다루는 #5번 이슈와 겹치는지 보고, KISA U-02 점검을 기존 프리미티브로 구현하는 작업 이슈를 만들어 주세요.\n범위: \"/etc/passwd\" 확인 (200자를 넘어도 됩니다) " + "가" * 300
        g, fctx = gather_file("", fname, ("\ufeff" + korean + "\n").encode("utf-8"))
        ct.check("draft: a request handed in as a file (Korean, punctuation, over 200) is read as written",
                 (g.get("status"), fctx.get("request") == korean, fctx.get("request_file")), ("OK", True, fname))
        strong = sorted(c.get("number") for c in fctx.get("candidates") or [] if c.get("strength") == "strong")
        ct.check("draft: a code or #n followed by a Korean particle (SRV-174를, #5번) still names it", strong, [5])
        g, fctx = gather_file("", fname, "#5abc 와 #5_test, my_SRV-174 와 SRV-174_x 의 새 점검".encode("utf-8"))
        strong = sorted(c.get("number") for c in fctx.get("candidates") or [] if c.get("strength") == "strong")
        ct.check("draft: #n or a code inside another identifier (#5abc, #5_test, my_SRV-174, SRV-174_x) names nothing",
                 (g.get("status"), strong), ("OK", []))
        g, _ = gather_file("KISA U-02", fname, b"KISA U-02")
        ct.check("draft: request and request_file together are refused", g.get("status"), "REFUSED")
        g, _ = gather_file("", "ctl-req-missing-" + secrets.token_hex(4) + ".md")
        ct.check("draft: a request_file that is not in the hand-in directory is refused", g.get("status"), "REFUSED")
        g, _ = gather_file("", "..")
        ct.check("draft: a request_file that escapes the hand-in directory is refused", g.get("status"), "REFUSED")
        g, _ = gather_file("", fname, b"\xff\xfe\x00bad")
        ct.check("draft: a request_file that is not UTF-8 is refused", g.get("status"), "REFUSED")
        g, _ = gather_file("", fname, "   \n".encode("utf-8"))
        ct.check("draft: an empty request_file is refused", g.get("status"), "REFUSED")

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
                           ("create_gist", ("d", {"a.md": "x"})), ("some_future_write", ())):
            try:
                getattr(ro, name)(*args)
                got.append((name, "ran"))
            except remote_mod.RemoteError:
                got.append((name, "refused"))
        ct.check("draft: the read-only remote refuses every write, and any unlisted name", {s for _, s in got}, {"refused"})
        ct.check("draft: … and records what was refused", len(ro.refused), 10)
        ct.check("draft: … while reads pass through", (len(ro.issues()), ro.issue(5)["title"]), (3, "SRV-174 Foundation"))
        ct.check("draft: … and the remote's file did not change", hashlib.sha256(open(p, "rb").read()).hexdigest() == h0, True)

    controls_draft_publish(ct)


def controls_draft_publish(ct):
    """publish: a secret gist of a checked draft, only when the registration asks for one."""
    print("\n[draft] publish — a secret gist of the checked draft, only when the registration asks")
    import secrets
    sim = ct.Sim(draft_db(ct))
    reg_dir = os.path.join(ct.stack_step.runtime()["paths"]["config_root"], "devflow", "projects")
    base = open(os.path.join(reg_dir, "scanner-fixture.yaml"), encoding="utf-8").read()
    regs = []

    def registration(mode):
        name = "ctl-pub-" + secrets.token_hex(4)
        path = os.path.join(reg_dir, f"{name}.yaml")
        body = base.replace("name: scanner-fixture", f"name: {name}")
        open(path, "w", encoding="utf-8").write(body.rstrip("\n") + "\n" + f"draft_publish: {mode}\n")
        regs.append(path)
        return name

    def drafted():
        rid = "ctl-" + secrets.token_hex(4)
        env = {**os.environ, "CONDUCTOR_SELF_RUN_ID": rid, "AGENTSTACK_RUN_ID": "ctl-run-" + rid[4:]}
        ws = os.path.join(ct.WS_ROOT, rid)
        sim.runs.append(ws)
        ct.step("draft_gather.py", ["scanner-fixture", "KISA U-02 비밀번호 복잡성 점검을 추가해줘", sim.name], env)
        drafter()(ws)
        c = ct.step("draft_check.py", ["scanner-fixture", "True", "run-draft"], env)
        return env, ws, c

    def gists():
        return sim.db.get("gists") or []

    try:
        env, ws, c = drafted()
        verdict = c.get("verdict")
        ct.check("draft publish: the draft to publish was checked", verdict in ("DRAFTED", "NEEDS_INPUT"), True)
        on = registration("gist")

        p = ct.step("draft_publish.py", [on, verdict, ""], env)
        ct.check("draft publish: a project that allows it publishes nothing a run did not ask for",
                 (p.get("status"), len(gists())), ("SKIPPED", 0))
        p = ct.step("draft_publish.py", [on, verdict], env)
        ct.check("draft publish: … nor with the request left out", (p.get("status"), len(gists())), ("SKIPPED", 0))
        p = ct.step("draft_publish.py", [on, verdict, "public"], env)
        ct.check("draft publish: publish=public is refused — there is no public gist", (p.get("status"), len(gists())), ("FAILED", 0))
        p = ct.step("draft_publish.py", ["scanner-fixture", verdict, "gist"], env)
        ct.check("draft publish: a run that asks, in a project that does not allow it: REFUSED",
                 (p.get("status"), "draft_publish: gist" in p.get("hint", ""), len(gists())), ("REFUSED", True, 0))
        off = registration("none")
        p = ct.step("draft_publish.py", [off, verdict, "gist"], env)
        ct.check("draft publish: … and draft_publish none is the same", (p.get("status"), len(gists())), ("REFUSED", 0))
        bad = registration("public")
        p = ct.step("draft_publish.py", [bad, verdict, "gist"], env)
        ct.check("draft publish: a registration mode but gist or none is refused", (p.get("status"), len(gists())), ("FAILED", 0))
        p = ct.step("draft_publish.py", [on, "", "gist"], env)
        ct.check("draft publish: no verdict (check did not run) publishes nothing", (p.get("status"), len(gists())), ("SKIPPED", 0))

        p = ct.step("draft_publish.py", [on, verdict, "gist"], env)
        g = gists()
        ct.check("draft publish: asked and allowed: one secret gist with the issue and the report",
                 (p.get("status"), len(g), bool(g) and g[0]["public"], sorted(g[0]["files"]) if g else []),
                 ("OK", 1, False, ["1-issue.md", "2-report.md"]))
        ct.check("draft publish: … and the answer names the files that left", p.get("error"), "published: 1-issue.md, 2-report.md")
        ct.check("draft publish: … carrying the files as the run left them",
                 bool(g) and g[0]["files"]["1-issue.md"] == open(os.path.join(ws, "draft", "issue.md"), encoding="utf-8").read()
                 and g[0]["files"]["2-report.md"] == open(os.path.join(ws, "draft", "report.md"), encoding="utf-8").read(), True)
        ct.check("draft publish: … named by the run, the project, the canonical and the verdict",
                 bool(g) and all(x in g[0]["description"] for x in (env["AGENTSTACK_RUN_ID"], on, verdict, "not an issue")), True)
        ct.check("draft publish: the report says no issue, PR or repository was written, and where the gist record is",
                 bool(g) and "이슈·PR·저장소에는 아무것도 쓰지 않았습니다" in g[0]["files"]["2-report.md"]
                 and "draft_publish.json" in g[0]["files"]["2-report.md"]
                 and "GitHub에는 아무것도 쓰지 않았습니다" not in g[0]["files"]["2-report.md"], True)
        rec = json.load(open(os.path.join(ws, "devflow", "draft_publish.json"), encoding="utf-8"))
        ct.check("draft publish: the link is kept in the run's workspace", (rec.get("url") == p.get("url"), rec.get("secret")), (True, True))
        again = ct.step("draft_publish.py", [on, verdict, "gist"], env)
        ct.check("draft publish: the same draft again is not published twice", (again.get("url") == p.get("url"), len(gists())), (True, 1))
        rep_before = open(os.path.join(ws, "devflow", "draft_report.json"), encoding="utf-8").read()

        # --- nothing that looks sensitive leaves
        issue_path = os.path.join(ws, "draft", "issue.md")
        clean = open(issue_path, encoding="utf-8").read()
        leaks = [("github token", "토큰 ghu_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"),
                 ("private key", "-----BEGIN OPENSSH PRIVATE KEY-----"),
                 ("credential assignment", "password: Hunter2Hunter2"),
                 ("aws access key", "key AKIA" + "ABCDEFGHIJKLMNOP"),
                 ("url with credentials", "https://admin:s3cretpass@scanner.example/"),
                 ("private network address", "수집 대상 10.20.30.40 에서")]
        for kind, line in leaks:
            open(issue_path, "w", encoding="utf-8").write(clean + "\n" + line + "\n")
            p = ct.step("draft_publish.py", [on, verdict, "gist"], env)
            secret_part = line.split()[-1]
            ct.check(f"draft publish: a {kind} in the draft refuses the publish, naming where — not the value",
                     (p.get("status"), kind in p.get("error", ""), "1-issue.md:" in p.get("error", ""),
                      secret_part in json.dumps(p, ensure_ascii=False), len(gists())),
                     ("REFUSED", True, True, False, 1))
        open(issue_path, "w", encoding="utf-8").write(clean)

        env2, ws2, c2 = drafted()
        sim.edit(lambda db: db.setdefault("faults", {}).update({"gists_denied": True}))
        p = ct.step("draft_publish.py", [on, c2.get("verdict"), "gist"], env2)
        ct.check("draft publish: a token that may not make gists: FAILED, and what to grant",
                 (p.get("status"), "Gists" in p.get("hint", ""), len(gists())), ("FAILED", True, 1))
        sim.edit(lambda db: db["faults"].pop("gists_denied"))
        sim.edit(lambda db: db.setdefault("faults", {}).update({"lose_response": ["create_gist"]}))
        p = ct.step("draft_publish.py", [on, c2.get("verdict"), "gist"], env2)
        ct.check("draft publish: a lost answer is FAILED and says the gist may exist — no retry",
                 (p.get("status"), "may have been made" in p.get("hint", ""), len(gists())), ("FAILED", True, 2))
        ct.check("draft publish: publishing changes nothing the check decided",
                 open(os.path.join(ws, "devflow", "draft_report.json"), encoding="utf-8").read() == rep_before, True)
        fresh = draft_db(ct)
        ct.check("draft publish: nothing but gists was written to the remote",
                 {k: v for k, v in sim.db.items() if k not in ("gists", "faults")} ==
                 {k: v for k, v in fresh.items() if k not in ("gists", "faults")}, True)
    finally:
        for r in regs:
            try:
                os.remove(r)
            except OSError:
                pass
        sim.close()
