"""devflow step: gather what an issue draft must be grounded in — read only.

usage: draft_gather.py <project> <request> [<fixture>] [<request file>]

  project       a registration under config/devflow/projects/<project>.yaml
  request       the person's request, in their own words ("" when it comes as a file)
  fixture       fixture remote scenario name (only for a project whose remote is `fixture`)
  request file  the request as a hand-in by name (step.handoff: /work/handoff/<name>), UTF-8 —
                for a request the stack's inputs cannot carry (any language, any punctuation,
                longer than 200). Exactly one of request and request file.

Reads, through a remote that refuses every write (lib/remote.py ReadOnly):
  * the canonical branch's head, the project policy and its hook at that commit
  * the task template (policy `draft.template`, default .devflow/templates/task.md)
  * the official criteria and contract files the policy names (their blobs)
  * every issue, open and closed, with its ```devflow task block parsed by devflow's own parser
and finds, without a model, the existing issues the request may duplicate, update or follow.

Writes, under /ws/<run>/ and nowhere else:
  repo/                          the canonical tree, for the drafter to read
  draft/                         empty: the drafter writes draft/issue.md here
  devflow/policy/                the hook, at the canonical commit
  devflow/draft_context.json     everything above, with the canonical SHA and digests
  devflow/prompt_draft.md        the drafter's instructions

What a repeat does: "yes" — the same remote state gives the same context. Nothing on the remote
changes: a draft is not an issue, an approval or permission to run.
"""
REPEATABLE = "yes"
import json, os, re, shutil, sys, time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
import dfcore  # noqa: E402
import step  # noqa: E402 — the stack's step helper: run id, workspace, the answer line
import remote as remote_mod  # noqa: E402
import refs  # noqa: E402
import draftlib  # noqa: E402
from wstree import tree_map  # noqa: E402

RUN = step.run_id()
WS = dfcore.workspace()
PROXY = dfcore.egress_env()
D = os.path.join(WS, "devflow")
REPO = os.path.join(WS, "repo")
DRAFT = os.path.join(WS, "draft")
CACHE = dfcore.state_dir("tree-cache")
SAFE = re.compile(r"[A-Za-z0-9._-]{1,80}")
REQUEST_MAX = 20000
# the longest prompt a brokered call carries (agent-stack stack/profile_runner.py refuses past 200,000)
PROMPT_MAX = 190000


def emit(**kw):
    kw.setdefault("error", "")
    for k in ("prompt", "canonical"):
        kw.setdefault(k, "")
    kw.setdefault("issues", 0)
    kw.setdefault("candidates", 0)
    step.out(**kw)


def stage_fixture(name):
    """A fixture remote: the hand-in file itself (so a write would show there), or the package's copy."""
    dest = os.path.join(D, "remote.json")
    if not SAFE.fullmatch(name or ""):
        raise dfcore.ConfigError(f"fixture name {name!r} is not a plain name")
    shared = step.handoff(f"{name}.json")
    if os.path.isfile(shared):
        if os.path.lexists(dest):
            os.remove(dest)
        os.symlink(shared, dest)
        return
    src = os.path.join(PKG, "fixtures", "remotes", f"{name}.json")
    if not os.path.isfile(src):
        raise dfcore.ConfigError(f"no fixture remote {name!r}")
    shutil.copyfile(src, dest)


def fill(tpl, values):
    for k, v in values.items():
        tpl = tpl.replace("{" + k + "}", v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1))
    return tpl


def read_request_file(name):
    """The request handed in as a file, by name: (text, error)."""
    if not SAFE.fullmatch(name):
        return None, f"request_file {name!r} is not a plain name (letters, digits, . _ -)"
    try:
        path = step.handoff(name)
    except ValueError as e:
        return None, str(e)
    if not os.path.isfile(path):
        return None, f"no hand-in named {name!r}: put the request in the hand-in directory (/work/handoff/)"
    with open(path, "rb") as f:
        raw = f.read(REQUEST_MAX * 4 + 1)
    if len(raw) > REQUEST_MAX * 4:
        return None, f"the hand-in {name!r} is over {REQUEST_MAX} characters"
    try:
        return raw.decode("utf-8").lstrip("\ufeff"), ""
    except UnicodeDecodeError:
        return None, f"the hand-in {name!r} is not UTF-8 text"


def main(argv):
    if len(argv) < 2:
        emit(status="TOOL_FAILURE", error="usage: draft_gather.py <project> <request> [<fixture>] [<request file>]")
        return 0
    project, request, fixture = argv[0], argv[1].strip(), (argv[2] if len(argv) > 2 else "")
    request_file = (argv[3] if len(argv) > 3 else "").strip()
    if request_file:
        if request:
            emit(status="REFUSED", error="both request and request_file were given: pass one")
            return 0
        text, err = read_request_file(request_file)
        if err:
            emit(status="REFUSED", error=err)
            return 0
        request = text.strip()
    if not request:
        emit(status="REFUSED", error="the request is empty: say what you want done (request=, or request_file= for a hand-in)")
        return 0
    if len(request) > REQUEST_MAX:
        emit(status="REFUSED", error=f"the request is over {REQUEST_MAX} characters")
        return 0
    os.makedirs(D, exist_ok=True)
    shutil.rmtree(DRAFT, ignore_errors=True)
    os.makedirs(DRAFT)
    # what an earlier attempt in this workspace left is not this attempt's draft (a resumed or
    # repeated run would otherwise judge an old report when the drafter produces nothing)
    for f in ("draft.json", "draft_report.json", "draft_context.json", "prompt_draft.md", "snapshot.json"):
        try:
            os.remove(os.path.join(D, f))
        except FileNotFoundError:
            pass
    try:
        reg = dfcore.registration(project)
        if reg["remote"] == "fixture":
            stage_fixture(fixture or reg.get("fixture") or "")
        rem = remote_mod.ReadOnly(dfcore.open_remote(reg, WS, PROXY))
        canonical = rem.branch_head(reg["canonical_branch"])
        pol = dfcore.load_policy(rem, reg, canonical)
        hook_digests = dfcore.materialize_hook(rem, pol, canonical, os.path.join(D, "policy"))
        crit = dfcore.criteria_blobs(rem, pol, canonical)
        tpath = (pol.get("draft") or {}).get("template") or draftlib.TEMPLATE_DEFAULT
        template = rem.file_at(canonical, tpath)
        issues = rem.issues()
        refs.copy_tree(refs.cached_tree(rem, canonical, CACHE, RUN), REPO)
        # the tree as handed over: draft_check refuses a draft whose drafter changed it
        json.dump(tree_map(REPO), open(os.path.join(D, "snapshot.json"), "w"), indent=0)
    except (dfcore.ConfigError, remote_mod.RemoteError) as e:
        emit(status="TOOL_FAILURE", error=f"{type(e).__name__}: {e}" + (f" ({e.hint})" if getattr(e, "hint", "") else ""))
        return 0

    rows = [draftlib.summarize(i) for i in issues]
    cands = draftlib.candidates(request, rows)
    strong = [c["number"] for c in cands if c["strength"] == "strong"]
    # what was said on a strong candidate after its body was written (decisions are often there),
    # and devflow's published state as people read it — its sentences, not its JSON marker
    discussion = {}
    try:
        for n in strong:
            discussion[n] = [dict(c, body=draftlib.readable(c.get("body"))) for c in rem.comments(n)]
    except remote_mod.RemoteError as e:
        emit(status="TOOL_FAILURE", error=f"RemoteError: {e}")
        return 0
    pol_doc = {k: v for k, v in pol.items() if not str(k).startswith("_")}
    ctx = {"schema": draftlib.DRAFT_SCHEMA, "run": RUN, "at": time.time(),
           "request": request, "request_digest": dfcore.sha256(request)[:16],
           "request_file": request_file,
           "project": project, "repository": reg["repository"], "canonical_branch": reg["canonical_branch"],
           "canonical": canonical, "policy": {"path": reg["policy_path"], "digest": pol["_digest"]},
           "workflow": dfcore.workflow_identity(), "hook": hook_digests,
           "template": {"path": tpath, "present": template is not None,
                        "digest": dfcore.sha256(template)[:16] if template is not None else None},
           "criteria": crit,
           "vocabulary": {k: pol_doc.get(k) or [] for k in ("task_types", "change_classes", "impact_flags")},
           "issues": rows, "candidates": cands,
           "discussion": {str(n): len(v) for n, v in discussion.items()},
           "remote_refused": list(rem.refused)}
    json.dump(ctx, open(os.path.join(D, "draft_context.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    gates = {c: [g.get("id") for g in gs] for c, gs in (pol_doc.get("human_gates") or {}).items()}
    values = {"REQUEST": request, "PROJECT": project, "REPOSITORY": reg["repository"],
              "CANONICAL": canonical, "CANONICAL_BRANCH": reg["canonical_branch"], "REPO": REPO,
              "TEMPLATE_PATH": tpath,
              "TEMPLATE": template if template is not None else "(the project has no task template at this path: "
                                                                 "use only the ```devflow block's fields below)",
              "VOCABULARY": ctx["vocabulary"], "GATES": gates,
              "CRITERIA": "\n".join(f"- {p}" + (" (absent)" if b == "absent" else "") for p, b in sorted(crit.items())) or "(none)",
              "ISSUES": [{k: r[k] for k in ("number", "state", "title", "task", "task_problem") if k in r} for r in rows],
              "CANDIDATES": cands or "(none found mechanically)",
              "CANDIDATE_BODIES": "\n\n".join(
                  f"### #{i['number']} ({i['state']}) {i.get('title') or ''}\n\n````markdown\n{draftlib.body_excerpt(i.get('body'))}\n````"
                  + draftlib.comments_excerpt(discussion.get(i["number"]) or [])
                  for i in issues if i["number"] in strong) or "(none)",
              "RULE_PATHS": [reg["policy_path"]] + list(pol_doc.get("rule_paths") or []),
              "POLICY_PATHS": pol_doc.get("paths") or "(the policy names no paths by role)",
              "SCOPE_OPEN": draftlib.SCOPE_OPEN, "SCOPE_CLOSE": draftlib.SCOPE_CLOSE,
              "ISSUE_FILE": os.path.join(DRAFT, "issue.md"), "OUT": os.path.join(D, "draft.json")}
    tpl = open(os.path.join(PKG, "prompts", "draft.md"), encoding="utf-8").read()
    text = fill(tpl, values)
    if len(text) > PROMPT_MAX:
        emit(status="TOOL_FAILURE", error=f"the drafter's prompt is {len(text)} characters, over {PROMPT_MAX} "
                                          "(a brokered call carries at most 200,000): name the issue in the request more narrowly")
        return 0
    prompt = os.path.join(D, "prompt_draft.md")
    open(prompt, "w", encoding="utf-8").write(text)
    emit(status="OK", prompt=prompt, canonical=canonical, issues=len(rows), candidates=len(cands))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
