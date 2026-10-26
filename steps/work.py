"""devflow step: set up the implementer's workspace for this round.

usage: work.py <project> <do>          do: prepare | implement | rebase | research (act.py)

Writes, under /ws/<run>/:

  repo/             the tree the implementer changes: canonical's head for a new task, the pull
                    request's head for a rework; for a rebase, canonical's head with the change
                    brought onto it
  reference/<as>/   what the project names as reference material (policy `implement.references`),
                    text only, converted to UTF-8 — read, never run
  devflow/snapshot.json     the tree as it was handed over (what deliver compares against)
  devflow/work.json         base, parents, stage, what was brought over, the round
  devflow/prompt_prepare.md, prompt_implement.md   the model's instructions for this round

and counts the round in the task's history (state.json), so an implementation that fails still
spends one of `implement.max_rounds`.

Everything is read from the remote at the commits the evaluation fixed; nothing a previous run
left in its workspace is used. What a repeat does: "yes" — the same commits give the same tree.
"""
REPEATABLE = "yes"
import hashlib, json, os, shutil, sys, time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
import dfcore  # noqa: E402
import candidate as candidate_mod  # noqa: E402
import step  # noqa: E402 — the stack's step helper: run id, workspace, the answer line
import remote as remote_mod  # noqa: E402
from wstree import tree_map  # noqa: E402
import refs  # noqa: E402

RUN = step.run_id()
WS = dfcore.workspace()
PROXY = dfcore.egress_env()
D = os.path.join(WS, "devflow")
REPO = os.path.join(WS, "repo")
REF = os.path.join(WS, "reference")
CRIT = os.path.join(WS, "criteria")
CACHE = dfcore.state_dir("tree-cache")   # commit trees, kept across runs (requires.state)


def emit(**kw):
    kw.setdefault("error", "")
    step.out(**kw)


def cached_tree(rem, sha, only=None):
    return refs.cached_tree(rem, sha, CACHE, RUN, only=only)


copy_tree = refs.copy_tree


def references(rem, impl):
    """The project's reference material, as the model may read it (lib/refs.py)."""
    return refs.references(rem, impl, REF, CACHE, RUN)


def criteria(pol, task):
    """The official criterion's own words for this task, from the official files in the checkout
    (lib/refs.py, the project's hook op `criteria_text`)."""
    return refs.criteria_text(pol, os.path.join(D, "policy"), task, REPO, CRIT)


def feedback(rem, st, ev):
    """What sent the task back, for the implementer: the blockers as the verdict states them, and
    what each failing check said about itself on the head."""
    pr = st.get("pr") or {}
    fails = []
    results = ((st.get("checks") or {}).get("results")) or {}
    failed = [n for n, r in results.items() if r.get("result") == "FAIL"]
    if failed and pr.get("head_sha"):
        for run in rem.check_runs(pr["head_sha"]):
            if run["name"] in failed:
                fails.append({k: run.get(k) for k in ("name", "conclusion", "title", "summary", "url")})
    return {"verdict": st.get("verdict"), "head": pr.get("head_sha"), "blockers": st.get("blockers") or [],
            "notes": st.get("notes") or [], "failed_checks": fails,
            "reviews": st.get("reviews") or {}, "previous_round": (st.get("history") or {}).get("last_delivery")}


def research_dir(impl):
    """Where the project keeps research records: the directory its research lane writes."""
    for m in (impl.get("modes") or {}).values():
        for w in m.get("writes") or []:
            return w.rstrip("/*") + "/"
    return ""


def records(rdir):
    """The research records already on the tree the model starts from."""
    root = os.path.join(REPO, rdir) if rdir else ""
    if not root or not os.path.isdir(root):
        return "(none yet)"
    out = []
    for d, _, files in os.walk(root):
        for f in sorted(files):
            out.append("- " + os.path.relpath(os.path.join(d, f), REPO).replace(os.sep, "/"))
    return "\n".join(out) or "(none yet)"


def web_hosts(role):
    return refs.web_hosts(role, PKG)


def fill(tpl, values):
    for k, v in values.items():
        tpl = tpl.replace("{" + k + "}", v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1))
    return tpl


def main(argv):
    project, do = argv[0], argv[1]
    st = json.load(open(os.path.join(D, "state.json"), encoding="utf-8"))
    ev = json.load(open(os.path.join(D, "evaluation.json"), encoding="utf-8"))
    pol = ev.get("policy_doc") or {}
    impl = pol.get("implement") or {}
    task = ev["task"]
    reg = dfcore.registration(project)
    rem = candidate_mod.wrap(dfcore.open_remote(reg, WS, PROXY), reg, task, ev)   # a local candidate's tree, when one stands
    canonical = ev["canonical_head"]
    pr = st.get("pr")
    branch = dfcore.task_branch(task)

    # the round counts from here, whatever becomes of it
    hist = st.setdefault("history", {})
    hist["implement_rounds"] = int(hist.get("implement_rounds") or 0) + 1
    json.dump(st, open(os.path.join(D, "state.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    work = {"stage": do, "branch": branch, "canonical": canonical, "round": hist["implement_rounds"],
            "at": time.time(), "brought": []}
    if do == "prepare":
        # a new branch from canonical, or — after a person answered what preparation asked — the
        # pull request that holds that preparation
        at = pr["head_sha"] if pr else canonical
        work.update(base=at, parent=at, extra_parents=[], new_branch=not pr)
        copy_tree(cached_tree(rem, at), REPO)
    elif do == "implement":
        work.update(base=pr["head_sha"], parent=pr["head_sha"], extra_parents=[])
        copy_tree(cached_tree(rem, pr["head_sha"]), REPO)
    elif do == "research":
        at = pr["head_sha"] if pr else canonical
        work.update(base=at, parent=at, extra_parents=[], new_branch=not pr, mode=task.get("type"))
        copy_tree(cached_tree(rem, at), REPO)
    elif do == "rebase":
        # canonical's tree plus the change: every file the change touched, as the change has it,
        # except the generated ones (they are regenerated). A file canonical also changed since
        # the change was made cannot be brought over by copying — that is for a person.
        gen = set(impl.get("generated") or [])
        mine = rem.changed_files(pr["base_sha"], pr["head_sha"])
        mb = mine.get("merge_base") or pr["base_sha"]
        theirs = set(rem.changed_files(mb, canonical)["files"])
        clash = sorted(f for f in mine["files"] if f in theirs and f not in gen)
        if clash:
            st["blockers"] = list(st.get("blockers") or []) + [
                f"rebase: canonical changed the same files since this change began: {clash[:5]}"]
            st.update(phase="implement", progress="waiting", wait={
                "reason": "implementation", "need": "bring the change onto the canonical head by hand (the same files changed on both sides)",
                "owner": "a person", "resume_when": "the head contains the canonical head"})
            st["next"] = st["wait"]["need"]
            # no automatic round can resolve this: devflow stops implementing until the head moves
            hist["manual_until_new_head"] = pr["head_sha"]
            json.dump(st, open(os.path.join(D, "state.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            emit(status="CONFLICT", stage=do, workspace=WS,
                 error=f"canonical changed the same files since this change began: {clash[:5]}")
            return 0
        copy_tree(cached_tree(rem, canonical), REPO)
        work.update(base=canonical, parent=pr["head_sha"], extra_parents=[canonical])
        snap = tree_map(REPO)
        json.dump(snap, open(os.path.join(D, "snapshot.json"), "w"), indent=0)
        head_tree = cached_tree(rem, pr["head_sha"])
        for f in mine["files"]:
            if f in gen:
                continue
            src, dst = os.path.join(head_tree, f), os.path.join(REPO, f)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            elif os.path.isfile(dst):
                os.remove(dst)
            work["brought"].append(f)
        json.dump(work, open(os.path.join(D, "work.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        emit(status="OK", stage=do, workspace=WS, prompt_prepare="", prompt_implement="", prompt_research="",
             round=work["round"])
        return 0
    else:
        emit(status="TOOL_FAILURE", stage=do, workspace=WS, error=f"unknown stage {do!r}")
        return 0

    json.dump(tree_map(REPO), open(os.path.join(D, "snapshot.json"), "w"), indent=0)
    refs = references(rem, impl)
    work["references"] = refs
    crit_files, crit_notes = criteria(pol, task)
    work["criteria"] = {"files": crit_files, "notes": crit_notes}
    trace_path = (pol.get("trace") or {}).get("path", ".devflow/tasks/{task}.yaml").format(task=task["id"])
    research_path = str(impl.get("research_path") or ".devflow/tasks/{task}.research.md").format(task=task["id"])
    instr = {}
    for kind in ("prepare", "implement"):
        p = (impl.get("instructions") or {}).get(kind)
        instr[kind] = (rem.file_at(canonical, p) if p else None) or "(the project gives no further instructions)"
    for kind, m in (impl.get("modes") or {}).items():
        p = m.get("instructions")
        instr[kind] = (rem.file_at(canonical, p) if p and kind == task.get("type") else None) or "(the project gives no further instructions)"
    fb = feedback(rem, st, ev) if (do == "implement" or (do == "research" and pr)) else None
    mode = (impl.get("modes") or {}).get(task.get("type")) or {}
    rdir = research_dir(impl)
    values = {"TASK": task, "REPO": REPO, "WS": WS, "BRANCH": branch, "CANONICAL": canonical,
              "CANONICAL_BRANCH": reg["canonical_branch"], "TRACE_PATH": trace_path,
              "RESEARCH_PATH": research_path,
              "REFERENCES": "\n".join(f"- {r['path']} — {r['ref']} @ {str(r['sha'])[:12]}, {r['files']} text files. {r['note']}"
                                      for r in refs if r.get("sha")) or "(none)",
              "RULE_PATHS": [reg["policy_path"]] + list(pol.get("rule_paths") or []),
              "GENERATED": list(impl.get("generated") or []),
              "REGENERATE": [" ".join(c) for c in impl.get("regenerate") or []],
              "FEEDBACK": fb or {}, "CLASSIFICATION": ev.get("classification") or {},
              "WEB_HOSTS": web_hosts(dfcore.RESEARCHER if do == "research" else dfcore.IMPLEMENTER),
              "RESEARCH_DIR": rdir or "(the project keeps no research records)",
              "RESEARCH_RECORDS": records(rdir),
              "RECORD_PATH": str(mode.get("path") or "docs/research/{task}.md").format(task=task["id"].lower()),
              "WRITES": list(mode.get("writes") or []),
              "CRITERIA": "\n".join([f"- {p}" for p in crit_files] + [f"- note: {n}" for n in crit_notes]) or "(none)"}
    prompts = {}
    kinds = ["prepare", "implement"] if do == "prepare" else ([task["type"]] if do == "research" else ["implement"])
    for kind in kinds:
        tpl = open(os.path.join(PKG, "prompts", f"{kind}.md"), encoding="utf-8").read()
        out = "research.json" if do == "research" else f"{kind}.json"
        body = fill(tpl, {**values, "PROJECT_INSTRUCTIONS": instr[kind], "OUT": os.path.join(D, out)})
        p = os.path.join(D, f"prompt_{kind}.md")
        open(p, "w", encoding="utf-8").write(body)
        prompts[kind] = p
    if fb is not None:
        json.dump(fb, open(os.path.join(D, "feedback.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(work, open(os.path.join(D, "work.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    emit(status="OK", stage=do, workspace=WS, prompt_prepare=prompts.get("prepare", ""),
         prompt_implement=prompts.get("implement", ""),
         prompt_research=prompts.get(task.get("type"), "") if do == "research" else "", round=work["round"])
    return 0


if __name__ == "__main__":
    # a crash is still an answer: every declared key, status TOOL_FAILURE and the reason (step.main)
    sys.exit(step.main(lambda: main(sys.argv[1:]), stage="", workspace="", prompt_prepare="", prompt_implement="", prompt_research="", round=0, error="", failure_key="error"))
