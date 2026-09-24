"""Infra Scanner's devflow hook — the project's own judgement, behind devflow's hook protocol.

devflow (agent-stack packages/devflow) is the same for every project. What is particular to this
scanner — which paths are authority, which change is a Foundation, what a trace of an official
criterion must contain, what the collection Plan's operations are — is decided here, and only here.
devflow runs this file from the canonical branch, never from the candidate being judged.

Protocol devflow-hook@1: one JSON document on stdin {"protocol", "op", "policy", ...}, one JSON line
on stdout {"protocol": "devflow-hook@1", ...}. Standard library only (plus nothing): it runs inside
the agent-stack runtime, which does not have this repository's environment.

  classify              task + changed files → task_type, change_class, impacts, checks, lenses,
                        violations, decisions, gaps
  validate_trace        the task's trace document → problems (implementation must change),
                        decisions (a person must decide), gaps (the contract cannot express it)
  artifact_ops          collection-plan.tsv at canonical and at head → the operation difference,
                        compared with the expected set the trace declares
  materials             which criteria and contract files a reviewer should read
  transaction_evidence  a published Foundation's rebaseline record → whether it evidences an
                        independent review and validation of exactly that commit

What this hook does not do: decide that a mapping *is* correct. It checks that a trace is
complete, uses the scanner's real state names, keeps the four non-results apart, and does not
present a provisional reading as official. Whether the reading is right is the criteria-trace
review's question; a filled-in form is not a checked one.
"""
import fnmatch, json, re, sys

PROTOCOL = "devflow-hook@1"


def answer(**kw):
    print(json.dumps({"protocol": PROTOCOL, **kw}, ensure_ascii=False))


def under(path, prefixes):
    return any(path == p or path.startswith(p.rstrip("/") + "/") or fnmatch.fnmatch(path, p) for p in prefixes)


# ------------------------------------------------------------------------------ classify
RANK = {"ORDINARY": 0, "CRITERION_CORRECTION": 1, "FOUNDATION": 2}


def classify(doc):
    pol, task, files = doc["policy"], doc.get("task") or {}, doc.get("changed_files") or []
    P = pol.get("paths") or {}
    impacts = {k: False for k in pol.get("impact_flags") or []}
    declared = {k: True for k in (task.get("impacts") or [])}
    auth = [f for f in files if under(f, P.get("authority_documents", []))]
    frozen = [f for f in files if under(f, P.get("frozen_registries", []))]
    owners = [f for f in files if under(f, P.get("semantic_owners", []))]
    if [f for f in files if under(f, P.get("items", []))]:
        impacts["item_changed"] = True
    if frozen or [f for f in owners if f.startswith("collector")]:
        impacts["contract_changed"] = True
    if [f for f in owners if f.startswith("evaluator")]:
        impacts["evaluator_changed"] = True
    if [f for f in files if under(f, P.get("builder", []))]:
        impacts["builder_changed"] = True
    if [f for f in files if under(f, P.get("shared_plan", []))]:
        impacts["shared_plan_changed"] = True
    if [f for f in files if under(f, P.get("criteria_sources", []))]:
        impacts["criterion_changed"] = True
    # what feeds the Plan affects it, whether or not the regenerated file was committed: the Builder
    # generates collection-plan.tsv from the bindings (scripts/build_submission.py
    # generate_collection_plan) and the Primitive registry. A collector or evaluator change does not.
    if impacts.get("item_changed") or "primitives.yaml" in frozen:
        impacts["shared_plan_changed"] = True
    for k in declared:
        impacts[k] = True
    # generic impacts devflow's common lenses understand
    if [f for f in files if f.startswith(".issue-orchestrator/") and f.endswith(".sh")] or \
       [f for f in files if f in ("scripts/verify-pr.sh", "scripts/validate_schema_contracts.py", "scripts/run_mutation_checks.py")]:
        impacts["control_changed"] = True
    if [f for f in files if f.startswith(".prompts/")]:
        impacts["judge_input"] = True
    if [f for f in files if f.startswith(".issue-orchestrator/rebaseline/")]:
        impacts["evidence_record"] = True
    if auth:
        impacts["normative_claim"] = True
    semantic = any(impacts.get(k) for k in ("item_changed", "contract_changed", "evaluator_changed", "criterion_changed"))
    impacts["semantic"] = semantic

    cls = str(task.get("change_class") or "")
    violations, decisions, gaps, problems = [], [], [], []
    if cls not in RANK:
        problems.append(f"change_class must be one of {sorted(RANK)} (task block)")
        cls = "ORDINARY"
    # the floor the paths imply; a lower declared class is never silently accepted
    floor = "FOUNDATION" if (frozen or owners) else "ORDINARY"
    if RANK[cls] < RANK[floor]:
        violations.append(f"declared {cls}, but the change touches semantic owners / frozen registries "
                          f"{sorted(frozen + owners)[:6]} — revert them, or return the task to preparation "
                          "for a Foundation analysis (a Foundation is never disguised as ordinary)")
    if auth and cls == "ORDINARY":
        violations.append(f"an ordinary change may not touch authority documents {auth[:6]}")
    if auth and cls != "ORDINARY":
        decisions.append(f"authority documents change ({auth[:6]}): a person approves the change of meaning")
    if cls == "FOUNDATION":
        scope = set(task.get("scope_files") or [])
        if not scope:
            problems.append("a Foundation task lists its exact approved files (scope_files)")
        else:
            outside = sorted(f for f in files if f not in scope and not f.startswith(".devflow/tasks/"))
            if outside:
                violations.append(f"files outside the approved Foundation scope: {outside[:8]}")
    if cls == "CRITERION_CORRECTION":
        impacts["criterion_changed"] = True
        if not task.get("affected_items"):
            problems.append("a criterion correction lists the items whose results change (affected_items)")
    if task.get("type") == "docs" and semantic:
        violations.append("declared as a docs task, but files that change the scanner's meaning are in the diff")

    na = {}
    if not semantic and not impacts.get("builder_changed") and not impacts.get("shared_plan_changed"):
        why = ("no item, contract, evaluator, criterion, builder or plan file changed — "
               f"changed: {files[:6]}")
        for c in ("scanner-publish", "plan-ops"):
            na[c] = why
    lenses = []
    if impacts.get("item_changed") or impacts.get("evaluator_changed") or impacts.get("contract_changed"):
        lenses.append("scanner-outcomes")
    if impacts.get("contract_changed") or impacts.get("evaluator_changed") or cls == "CRITERION_CORRECTION":
        lenses.append("scanner-merged-results")
    answer(task_type=task.get("type"), change_class=cls, impacts=impacts, required_checks=[],
           not_applicable=na, lenses=lenses, trace_required=semantic,
           shared_artifacts=["collection-plan"] if impacts.get("shared_plan_changed") else [],
           violations=violations, decisions=decisions, gaps=gaps, problems=problems,
           floor=floor)


# ------------------------------------------------------------------------------ trace
OUTCOMES = ("not_applicable", "target_absent", "collection_failed", "insufficient_evidence")
MAPPING_FIELDS = ("id", "condition", "source", "status", "evidence", "check", "item", "expected")


def validate_trace(doc):
    pol, text = doc["policy"], doc.get("trace_text")
    states = set(pol.get("verdict_states") or [])
    problems, decisions, gaps, notes = [], [], [], []
    if text is None:
        answer(problems=[f"the trace document {doc.get('trace_path')} is not in the change"], decisions=[], gaps=[])
        return
    try:
        import yaml                                        # present in the agent-stack runtime
        t = yaml.safe_load(text) or {}
    except Exception as e:
        answer(problems=[f"the trace document is not YAML: {type(e).__name__}"], decisions=[], gaps=[])
        return
    known_criteria = {c["id"] for c in (pol.get("authority") or {}).get("criteria") or []}
    maps = t.get("mappings") or []
    if not maps:
        problems.append("a change that affects meaning has no criterion mappings")
    for m in maps:
        mid = m.get("id", "?")
        miss = [k for k in MAPPING_FIELDS if not m.get(k)]
        if miss:
            problems.append(f"mapping {mid}: missing {miss}")
            continue
        src = m.get("source") or {}
        if src.get("criterion") not in known_criteria:
            problems.append(f"mapping {mid}: criterion {src.get('criterion')!r} is not an official criterion in the policy {sorted(known_criteria)}")
        if not (src.get("clause") and (src.get("page") or src.get("sheet"))):
            problems.append(f"mapping {mid}: the source names no clause and page/sheet")
        status = m.get("status")
        if status not in ("confirmed", "provisional", "unresolved"):
            problems.append(f"mapping {mid}: status must be confirmed / provisional / unresolved")
        elif status == "confirmed" and not m.get("status_basis"):
            problems.append(f"mapping {mid}: confirmed without the comparison it rests on (status_basis)")
        elif status == "provisional":
            decisions.append(f"mapping {mid} is provisional — confirm it against the official text, or record "
                             "that this change excludes it; a provisional reading is not a criterion")
        elif status == "unresolved":
            decisions.append(f"mapping {mid} is unresolved: {m.get('open_question') or '(no question recorded)'}")
        if m.get("expected") not in states:
            problems.append(f"mapping {mid}: expected {m.get('expected')!r} is not a scanner state {sorted(states)}")
        out = m.get("outcomes") or {}
        lack = [k for k in OUTCOMES if k not in out]
        if lack:
            problems.append(f"mapping {mid}: the outcomes {lack} are not mapped (keep the four apart)")
        for k, v in out.items():
            if v not in states and not str(v).startswith("GAP"):
                problems.append(f"mapping {mid}: outcome {k} → {v!r} is not a scanner state")
        if out.get("collection_failed") in ("PASS", "NA", "FAIL"):
            problems.append(f"mapping {mid}: a collection failure may not become {out['collection_failed']} "
                            "(CLAUDE.md: UNKNOWN or PARTIAL, never FAIL; never silently good)")
        if out.get("insufficient_evidence") in ("PASS", "NA"):
            problems.append(f"mapping {mid}: insufficient evidence may not become {out['insufficient_evidence']}")
        for k, v in out.items():
            if str(v).startswith("GAP"):
                gaps.append(f"mapping {mid}: the current states cannot express {k} ({v})")
        if m.get("gap"):
            gaps.append(f"mapping {mid}: {m['gap']}")
        if not m.get("reuse_basis") and m.get("check"):
            problems.append(f"mapping {mid}: reuses {m.get('check')} without saying why its meaning (not its name) fits")
    notes.append("structure and state names checked; whether each reading is right is the criteria-trace review's")
    answer(problems=problems, decisions=decisions, gaps=gaps, notes=notes, mappings=len(maps))


# ------------------------------------------------------------------------------ plan
def plan_ops(text):
    """collection-plan.tsv: a `#!collection-plan <ver> <count>` header, then one operation per
    line: op-id, primitive, version, target, params."""
    head, ops = None, {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if line.startswith("#!"):
            head = cols
            continue
        if len(cols) >= 5:
            ops[cols[0]] = {"primitive": cols[1], "version": cols[2], "target": cols[3], "params": cols[4]}
    declared = int(head[2]) if head and len(head) > 2 and head[2].isdigit() else None
    return ops, declared


def key(o):
    return f"{o.get('primitive')}|{o.get('target')}|{o.get('params', '-')}"


def artifact_ops(doc):
    base, bdecl = plan_ops(doc.get("base_text"))
    head, hdecl = plan_ops(doc.get("head_text"))
    added = sorted(key(head[i]) for i in set(head) - set(base))
    removed = sorted(key(base[i]) for i in set(base) - set(head))
    problems = []
    if hdecl is not None and hdecl != len(head):
        problems.append(f"the header says {hdecl} operations, the file has {len(head)}")
    try:
        import yaml
        t = yaml.safe_load(doc.get("task_doc") or "") or {}
    except Exception:
        t = {}
    exp = ((t.get("expected_plan_ops") or {}).get(doc["artifact"]["id"])) if t else None
    if exp is None:
        answer(match=False, actual={"added": added, "removed": removed, "count": [len(base), len(head)]},
               expected=None, diff="the trace declares no expected_plan_ops for this artifact")
        return
    e_add = sorted(key(o) for o in exp.get("added") or [])
    e_rem = sorted(key(o) for o in exp.get("removed") or [])
    diff = {"unexpected_added": sorted(set(added) - set(e_add)), "missing_added": sorted(set(e_add) - set(added)),
            "unexpected_removed": sorted(set(removed) - set(e_rem)), "missing_removed": sorted(set(e_rem) - set(removed))}
    # a count assertion moves only as the expected operations say — never to match the output
    for ca in t.get("count_assertions") or []:
        if not ca.get("because"):
            problems.append(f"count assertion in {ca.get('file')}: no reason from the expected operations")
        if ca.get("from") != len(base) or ca.get("to") != len(head):
            problems.append(f"count assertion in {ca.get('file')}: {ca.get('from')}→{ca.get('to')} "
                            f"but the Plan went {len(base)}→{len(head)}")
    match = not any(diff.values()) and not problems
    answer(match=match, actual={"added": added, "removed": removed, "count": [len(base), len(head)]},
           expected={"added": e_add, "removed": e_rem}, diff={**diff, "problems": problems})


# ------------------------------------------------------------------------------ materials
def materials(doc):
    """What a reviewer reads besides the diff. The official PDF / xlsx are binary; what stands in
    for them is the extract BASELINE_MANIFEST.md records as equivalent to the current file (and, in
    CI, tests/test_devflow_trace_quotes.py proving each trace quote is in the official file) — an
    extract is never authority by itself (Profile §2)."""
    task = doc.get("task") or {}
    impacts = ((doc.get("classification") or {}).get("impacts")) or {}
    crit = []
    for item in task.get("items") or []:
        if re.fullmatch(r"U-\d{2}", str(item)):
            crit += [f"items/_raw_pdf/{item}.yaml", f"items/kisa/{item}.yaml"]
        elif re.fullmatch(r"SRV-\d{3}", str(item)):
            crit += [f"items/_raw_efin_excel/{item}.yaml", f"items/efin/{item}.yaml"]
    crit.append("docs/baselines/BASELINE_MANIFEST.md")
    # the contracts the policy pins, as text a reviewer can read (the binary criteria are not)
    auth = (doc.get("policy") or {}).get("authority") or {}
    contracts = [p for src in auth.get("contracts") or [] for p in src.get("paths") or []
                 if p.endswith((".md", ".yaml", ".py"))]
    # the code a verdict of this change runs through: how Evidence is collected and resolved
    impl = []
    if any(impacts.get(k) for k in ("item_changed", "contract_changed", "evaluator_changed")):
        impl += ["evaluator/evaluator.py", "evaluator/binding_resolver.py",
                 "collector/lib/core.sh", "collector/lib/adapters.sh"]
    if impacts.get("shared_plan_changed") or impacts.get("builder_changed"):
        impl += ["scripts/build_submission.py", "evaluator/binding_resolver.py"]
    answer(criteria=crit, contracts=contracts, implementation=list(dict.fromkeys(impl)))


# ------------------------------------------------------------------------------ transaction
def transaction_evidence(doc):
    """A published Foundation (A+B) evidences review and validation through its rebaseline record
    (OPERATING_POLICY "Rebaseline record"). The record's fields are checked against each other and
    against A — which is what verify-rebaseline.sh does too; like it, this cannot establish that the
    approval in the record is genuine."""
    a, text = doc.get("commit"), doc.get("record_text")
    if not text:
        answer(valid=False, problems=[f"no rebaseline record at {doc.get('record_path')} on canonical"])
        return
    try:
        import yaml
        r = yaml.safe_load(text) or {}
    except Exception as e:
        answer(valid=False, problems=[f"the rebaseline record is not YAML: {type(e).__name__}"])
        return
    p = []
    rv, vd = r.get("independent_review") or {}, r.get("validation_evidence") or {}
    if r.get("foundation_commit") != a:
        p.append("the record is for another commit")
    if rv.get("reviewed_sha") != a:
        p.append("the independent review is not bound to A")
    if not rv.get("reviewer") or rv.get("reviewer") == r.get("foundation_actor"):
        p.append("the reviewer is not separate from the Foundation actor")
    if not rv.get("session_id"):
        p.append("the review names no session")
    if rv.get("blocking_findings"):
        p.append(f"the review has blocking findings: {rv['blocking_findings']}")
    if vd.get("validated_sha") != a or vd.get("exit_code") != 0:
        p.append("validation is not a passing run on A")
    if not (r.get("human_approval") or {}).get("approver"):
        p.append("no human approval is recorded")
    answer(valid=not p, problems=p, reviewer=rv.get("reviewer"), actor=r.get("foundation_actor"))


OPS = {"classify": classify, "validate_trace": validate_trace, "artifact_ops": artifact_ops,
       "materials": materials, "transaction_evidence": transaction_evidence}

if __name__ == "__main__":
    try:
        d = json.loads(sys.stdin.read())
        if d.get("protocol") != PROTOCOL:
            answer(error=f"protocol {d.get('protocol')!r}")
        elif d.get("op") not in OPS:
            answer(error=f"unknown op {d.get('op')!r}")
        else:
            OPS[d["op"]](d)
    except Exception as e:
        answer(error=f"{type(e).__name__}: {e}")
