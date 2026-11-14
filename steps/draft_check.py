"""devflow step: check an issue draft against the project's real rules — deterministic, no remote.

usage: draft_check.py <project> <produced> [<run id of the drafter's call>]

Reads what draft_gather fixed (devflow/draft_context.json, the canonical tree in repo/, the hook in
devflow/policy/) and what the drafter wrote (draft/issue.md, devflow/draft.json), and checks:

  * the ```devflow block parses with devflow's own parser, and its values are the policy's
  * what the work would change and what it only reads are told apart: scope_candidates go to the
    project's hook (classify, as the changed files) and reference_files never do; a file is never in
    both, every reference exists at the canonical commit, and the issue body's scope section names
    exactly the scope candidates — a file the body says will change cannot pass as a reference
  * the hook's problems, decisions and gaps are listed as open; its violations refuse the draft
  * a change class is in the block only with evidence that exists at the canonical commit
  * every fact has a source a person can open (a file at the canonical commit, or an issue)
  * every unknown is research, a contract gap or a decision
  * every issue found mechanically to be related is addressed, and "new" is not claimed for work an
    existing issue already covers
  * the prose claims no state nobody verified (confirmed, ready, approved, 확정, 준비 완료, ...);
    a negated mention in its own clause ("승인된 범위 아님", "not approved") says what is not so
  * the drafter changed nothing in the checkout

Answers `verdict`: INVALID (a check refused it), NEEDS_INPUT (a sound draft with open items for a
person), DRAFTED (nothing open). Never "ready": readiness is devflow's verdict on a real task.

Writes devflow/draft_report.json and draft/report.md in this run's workspace, and nothing else —
no GitHub call at all. What a repeat does: "yes" — the same files give the same report.
"""
REPEATABLE = "yes"
import json, os, re, sys

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
import dfcore  # noqa: E402
import step  # noqa: E402
import draftlib  # noqa: E402
from wstree import tree_map  # noqa: E402

WS = dfcore.workspace()
D = os.path.join(WS, "devflow")
REPO = os.path.join(WS, "repo")
DRAFT = os.path.join(WS, "draft")
SOURCE = re.compile(r"^(?P<path>[^:#\s][^:\s]*)(?::\d+(?:-\d+)?)?$")


def emit(**kw):
    kw.setdefault("error", "")
    for k in ("verdict", "report", "issue", "summary"):
        kw.setdefault(k, "")
    kw.setdefault("blocking", 0)
    kw.setdefault("open", 0)
    step.out(**kw)


def in_repo(path):
    p = os.path.normpath(os.path.join(REPO, path))
    return p.startswith(REPO + os.sep) and os.path.exists(p)


def source_ok(src, numbers):
    src = str(src or "").strip()
    m = re.fullmatch(r"#(\d+)", src)
    if m:
        return int(m.group(1)) in numbers
    m = SOURCE.match(src)
    return bool(m) and in_repo(m.group("path"))


def scope_lists(rep):
    """scope_candidates and reference_files as lists of paths, and what is wrong with their shape.
    A report without reference_files (devflow/draft@1) has none."""
    out, errs = [], []
    for key in ("scope_candidates", "reference_files"):
        v = rep.get(key)
        if v is None:
            v = []
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            errs.append(f"{key} is not a list of paths")
            v = [x for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []
        out.append([os.path.normpath(x.strip()) for x in v])
    return out[0], out[1], errs


def check(ctx, issue_md, rep, pol):
    blocking, open_, notes = [], [], []
    numbers = {r["number"]: r for r in ctx["issues"]}
    voc = ctx["vocabulary"]

    rel = rep.get("relation")
    if rel not in draftlib.RELATIONS:
        blocking.append(f"relation {rel!r} is not one of {list(draftlib.RELATIONS)}")
    related = rep.get("related") or []
    if not isinstance(related, list):
        blocking.append("related is not a list")
        related = []
    said = {}
    for r in related:
        n = r.get("number") if isinstance(r, dict) else None
        if n not in numbers:
            blocking.append(f"related names #{n}, which is not an issue of this repository")
            continue
        said[n] = r.get("relation")
        if not str(r.get("why") or "").strip():
            blocking.append(f"related #{n} gives no reason")
    for c in ctx["candidates"]:
        if c["strength"] != "strong":
            continue
        if c["number"] not in said:
            blocking.append(f"#{c['number']} was found mechanically ({'; '.join(c['why'])}) and the draft does not address it")
        elif said[c["number"]] == "unrelated":
            open_.append(f"#{c['number']} shares {'; '.join(c['why'])} but the draft calls it unrelated — a person confirms")
    if rel == "new":
        covers = [n for n, v in said.items() if v in ("duplicate", "update") and numbers[n]["state"] == "open"]
        if covers:
            blocking.append(f"relation is new, yet open issue(s) {covers} are said to be duplicated or updated by it")
    if rel in ("update", "duplicate") and not [n for n, v in said.items() if v == rel]:
        blocking.append(f"relation is {rel}, but no related issue is named with relation {rel}")

    # the task block, by devflow's own parser and the project's vocabulary
    task = None
    fences = dfcore.TASK_FENCE.findall(issue_md)
    if rel != "duplicate" or fences:
        if len(fences) != 1:
            blocking.append(f"the draft has {len(fences)} ```devflow blocks; a task issue has exactly one")
        else:
            task, why = dfcore.parse_task({"body": issue_md, "number": 0})
            if task is None:
                blocking.append(f"task block: {why}")
    cls = rep.get("classification") or {}
    scope, refs, shape_err = scope_lists(rep)
    blocking += shape_err
    if task:
        if voc.get("task_types") and task.get("type") not in voc["task_types"]:
            blocking.append(f"type {task.get('type')!r} is not one of {voc['task_types']}")
        if task.get("change_class") is not None and voc.get("change_classes") and task["change_class"] not in voc["change_classes"]:
            blocking.append(f"change_class {task['change_class']!r} is not one of {voc['change_classes']}")
        imp = task.get("impacts") or []
        bad = [i for i in (imp if isinstance(imp, list) else [imp]) if i not in (voc.get("impact_flags") or [])]
        if bad:
            blocking.append(f"impacts {bad} are not among {voc.get('impact_flags')}")
        if rel in ("new", "follow_up"):
            same = [r["number"] for r in ctx["issues"] if (r.get("task") or {}).get("id") == task.get("id")]
            if same:
                blocking.append(f"task id {task.get('id')!r} is already the task of {['#%d' % n for n in same]}")
        # a class only with evidence (no class confirmed without it)
        ev = [e for e in (cls.get("evidence") or []) if isinstance(e, dict)]
        missing = [e.get("path") for e in ev if not in_repo(str(e.get("path") or ""))]
        if task.get("change_class"):
            if cls.get("status") != "candidate" or not ev:
                blocking.append(f"change_class {task['change_class']} is in the block without evidence "
                                "(classification.status candidate, with files that decide it)")
            elif missing:
                blocking.append(f"the class's evidence names files not at the canonical commit: {missing[:5]}")
            elif cls.get("change_class") != task["change_class"]:
                blocking.append(f"the block says {task['change_class']}, the report {cls.get('change_class')!r}")
        else:
            open_.append("change class unresolved: " + ("; ".join(map(str, cls.get("alternatives") or [])) or "no alternatives given"))
        # the project's own reading of it — of what the work would change, never of what it only reads
        payload_task = dict(task, issue=0)
        try:
            got = dfcore.run_hook(pol, os.path.join(D, "policy"), "classify", {
                "task": payload_task, "changed_files": scope, "authors": [], "pr": None,
                "canonical_head": ctx["canonical"], "blobs": {},
                "proposal_requested": dfcore.proposal_mode(pol, payload_task, task.get("change_class"))[0]})
        except dfcore.HookError as e:
            return None, f"HookError: {e}"
        shape = dfcore.validate_classification(pol, got)
        if task.get("change_class"):
            blocking += [f"hook: {v}" for v in got.get("violations") or []]
            open_ += [f"hook: {p}" for p in got.get("problems") or []]
        else:
            # the hook reads a missing class as a problem; that is the unresolved class already listed
            open_ += [f"hook: {p}" for p in got.get("problems") or [] if "change_class" not in p]
            notes += [f"hook (as ORDINARY, for reference): {v}" for v in got.get("violations") or []]
        open_ += [f"hook decision: {d}" for d in got.get("decisions") or []]
        open_ += [f"hook gap: {g}" for g in got.get("gaps") or []]
        notes += [f"hook: {n}" for n in got.get("notes") or []]
        if task.get("change_class"):
            open_ += [f"hook classification shape: {p}" for p in shape]
        notes.append("impacts the hook reads from the scope candidates: "
                     + (", ".join(sorted(k for k, v in (got.get("impacts") or {}).items() if v)) or "(none)"))

    # what changes and what is only read
    if task:
        stated, marks = draftlib.scope_sections(issue_md)
        blocking += [f"the issue body's scope markers: {p}" for p in marks]
        if len(stated) != 1:
            blocking.append(f"the issue body has {len(stated)} scope sections ({draftlib.SCOPE_OPEN} … "
                            f"{draftlib.SCOPE_CLOSE}); a task issue states its change scope in exactly one"
                            + (" (the section is {}: a draft written before it is drafted again, not re-checked)"
                               .format(draftlib.DRAFT_SCHEMA) if not stated and not marks else ""))
        else:
            said_scope = {os.path.normpath(p) for p in stated[0]}
            body_only = sorted(said_scope - set(scope))
            json_only = sorted(set(scope) - said_scope)
            if body_only:
                blocking.append(f"the issue body's scope names {body_only[:6]}, which scope_candidates does not"
                                + (" (it lists them as reference files)" if set(body_only) & set(refs) else ""))
            if json_only:
                blocking.append(f"scope_candidates names {json_only[:6]}, which the issue body's scope does not")
    both = sorted(set(scope) & set(refs))
    if both:
        blocking.append(f"{both[:6]} are both a scope candidate and a reference file — a file the work would "
                        "change is a scope candidate, and the hook reads it as one")
    # an official criterion or a contract the policy names, in the scope, is a person's decision
    # whatever class the draft proposes and whatever the project's hook says of it (the hook may only
    # flag it): a draft never changes the meaning of the work by itself
    auth = pol.get("authority") or {}
    for role, srcs in (("official criterion", auth.get("criteria") or []), ("contract", auth.get("contracts") or [])):
        named = {os.path.normpath(p) for s_ in srcs for p in (s_.get("paths") or ([s_["path"]] if s_.get("path") else []))}
        hit = sorted(f for f in scope if f in named)
        if hit:
            open_.append(f"decision: the scope would change the {role} file(s) {hit[:6]} the policy names — "
                         "only a person decides that, whatever the class")
    gone = [r for r in refs if not in_repo(r)]
    if gone:
        blocking.append(f"reference files not at the canonical commit: {gone[:6]}")
    if refs:
        notes.append("reference files (read, not changed; not given to the hook): " + ", ".join(refs[:12]))

    # facts, inferences, unknowns
    for f in rep.get("facts") or []:
        if not isinstance(f, dict) or not source_ok(f.get("source"), numbers):
            blocking.append(f"fact without a source a person can open: {str((f or {}).get('text') if isinstance(f, dict) else f)[:120]!r} "
                            f"(source {(f or {}).get('source') if isinstance(f, dict) else None!r})")
    for i in rep.get("inferences") or []:
        if not isinstance(i, dict) or not i.get("from"):
            blocking.append(f"inference that says nothing of what it rests on: {str(i)[:120]!r}")
    for u in rep.get("unknowns") or []:
        if not isinstance(u, dict) or u.get("kind") not in draftlib.UNKNOWN_KINDS:
            blocking.append(f"unknown whose kind is not one of {list(draftlib.UNKNOWN_KINDS)}: {str(u)[:120]!r}")
        else:
            open_.append(f"{u['kind']}: {u.get('text')}")

    for c in draftlib.claims(issue_md):
        blocking.append(f"the draft claims a state nobody verified ({c['word']!r}): {c['line']!r}")
    return {"blocking": blocking, "open": open_, "notes": notes, "task": task}, ""


def report_md(ctx, rep, res, verdict):
    L = [f"# Issue draft — {verdict}", "",
         "초안입니다. 이슈도, 승인도, 실행 권한도 아닙니다. GitHub의 이슈·PR·저장소에는 아무것도 쓰지 않았습니다. "
         "비공개 Gist로 공유했는지는 이 실행의 게시 기록(devflow/draft_publish.json)에 있습니다.", "",
         f"- 요청 digest `{ctx['request_digest']}` · 프로젝트 `{ctx['project']}` ({ctx['repository']})",
         f"- canonical `{ctx['canonical']}` ({ctx['canonical_branch']}) · policy `{ctx['policy']['digest']}` · "
         f"workflow {ctx['workflow']['version']}/{ctx['workflow']['digest']}",
         f"- 템플릿 `{ctx['template']['path']}` ({'digest ' + str(ctx['template']['digest']) if ctx['template']['present'] else '없음'})",
         f"- 제목: {rep.get('title') or '(없음)'} · 관계: `{rep.get('relation')}`", "",
         "## 관련 이슈", ""]
    said = {r.get("number"): r for r in rep.get("related") or [] if isinstance(r, dict)}
    mech = {c["number"]: c for c in ctx["candidates"]}
    for n in sorted(set(said) | set(mech), key=lambda x: (x is None, x)):
        r, c = said.get(n) or {}, mech.get(n)
        L.append(f"- #{n} — 초안: `{r.get('relation', '(언급 없음)')}` {r.get('why') or ''}"
                 + (f" · 기계 탐지: {c['strength']} ({'; '.join(c['why'])})" if c else ""))
    if not said and not mech:
        L.append("- (없음)")
    cls = rep.get("classification") or {}
    L += ["", "## 분류", "", f"- change_class: `{cls.get('change_class')}` · 상태 `{cls.get('status')}`"]
    L += [f"  - 근거 {e.get('path')}: {e.get('why')}" for e in cls.get("evidence") or [] if isinstance(e, dict)]
    L += [f"  - 대안: {a}" for a in cls.get("alternatives") or []]
    scope, refs, _ = scope_lists(rep)
    L += ["", "## 변경 후보 (hook이 분류한 파일)", ""] + ([f"- `{p}`" for p in scope] or ["- (없음)"])
    L += ["", "## 참고 파일 (읽기만, 변경 없음)", ""] + ([f"- `{p}`" for p in refs] or ["- (없음)"])
    L += ["", "## 사실 (출처)", ""] + [f"- {f.get('text')} — `{f.get('source')}`" for f in rep.get("facts") or [] if isinstance(f, dict)]
    L += ["", "## 추론", ""] + [f"- {i.get('text')} ← {i.get('from')}" for i in rep.get("inferences") or [] if isinstance(i, dict)]
    L += ["", "## 검증 결과", ""]
    L += [f"- ✗ {b}" for b in res["blocking"]] or ["- 차단 항목 없음"]
    L += [f"- 참고: {n}" for n in res["notes"]]
    L += ["", "## 미해결 (사람·Research·계약)", ""] + ([f"- {o}" for o in res["open"]] or ["- 없음"])
    L += ["", f"초안 본문: `{os.path.join(DRAFT, 'issue.md')}`", ""]
    return "\n".join(L)


def main(argv):
    project = argv[0] if argv else ""
    produced = (argv[1] if len(argv) > 1 else "").lower() == "true"
    try:
        ctx = json.load(open(os.path.join(D, "draft_context.json"), encoding="utf-8"))
    except Exception as e:
        emit(status="TOOL_FAILURE", error=f"no draft context ({type(e).__name__}): draft_gather did not run")
        return 0
    if ctx.get("project") != project:
        emit(status="TOOL_FAILURE", error=f"the context is for {ctx.get('project')!r}, not {project!r}")
        return 0
    pol_text = open(os.path.join(REPO, ctx["policy"]["path"]), encoding="utf-8").read() \
        if in_repo(ctx["policy"]["path"]) else None
    if pol_text is None or dfcore.sha256(pol_text)[:16] != ctx["policy"]["digest"]:
        emit(status="TOOL_FAILURE", error="the policy in the checkout is not the one draft_gather read")
        return 0
    pol = dfcore._yaml(pol_text)

    res = {"blocking": [], "open": [], "notes": []}
    rep, issue_md = {}, ""
    snap = os.path.join(D, "snapshot.json")
    if os.path.isfile(snap) and json.load(open(snap)) != tree_map(REPO):
        res["blocking"].append("the drafter changed the checkout (repo/), which is read only")
    stray = sorted(f for f in os.listdir(DRAFT) if f not in ("issue.md", "report.md")) if os.path.isdir(DRAFT) else []
    if stray:
        res["notes"].append(f"files in draft/ besides issue.md are ignored: {stray[:5]}")
    try:
        rep = json.load(open(os.path.join(D, "draft.json"), encoding="utf-8")) if produced else None
        if not isinstance(rep, dict):
            raise ValueError("not a JSON object")
    except Exception as e:
        res["blocking"].append(f"the drafter produced no report ({'not produced' if not produced else type(e).__name__})")
        rep = {}
    try:
        issue_md = open(os.path.join(DRAFT, "issue.md"), encoding="utf-8").read()
    except OSError:
        res["blocking"].append("the drafter wrote no draft/issue.md")
    if rep and issue_md:
        got, err = check(ctx, issue_md, rep, pol)
        if got is None:
            emit(status="TOOL_FAILURE", error=err)
            return 0
        for k in ("blocking", "open", "notes"):
            res[k] += got[k]
        res["task"] = got["task"]
    verdict = "INVALID" if res["blocking"] else ("NEEDS_INPUT" if res["open"] else "DRAFTED")
    out = {"schema": draftlib.DRAFT_SCHEMA, "verdict": verdict, "canonical": ctx["canonical"],
           "request_digest": ctx["request_digest"], "policy": ctx["policy"], "workflow": ctx["workflow"],
           "relation": rep.get("relation"), "related": rep.get("related") or [],
           "candidates": ctx["candidates"], "task": res.get("task"),
           "scope_candidates": scope_lists(rep)[0], "reference_files": scope_lists(rep)[1], **{k: res[k] for k in ("blocking", "open", "notes")},
           "issue": os.path.join(DRAFT, "issue.md") if issue_md else ""}
    path = os.path.join(D, "draft_report.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    md = os.path.join(DRAFT, "report.md")
    open(md, "w", encoding="utf-8").write(report_md(ctx, rep, res, verdict))
    emit(status="OK", verdict=verdict, report=md, issue=out["issue"], blocking=len(res["blocking"]),
         open=len(res["open"]), summary=f"{verdict}: {len(res['blocking'])} blocking, {len(res['open'])} open")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
