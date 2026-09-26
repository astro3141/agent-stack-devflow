"""devflow's judgement machinery — shared by every project, knowing none of them.

Not a step (see lib/remote.py). What lives here is the development workflow's own behaviour —
the part CONTRACT.md says belongs to a workflow and not to the platform — and it is written so that
one copy serves every project: whatever is particular to a project (its official criteria, how a
change is classified, what a check is called, which extra review lenses exist, what its shared
generated artifacts are) arrives as that project's **policy file and hook**, read from the project's
own repository at a pinned commit.

Four things are kept apart on purpose, because mixing them is how a workflow ends up calling a
crashed tool a rejection or a file on disk an approval:

  phase      where the task is: prepare → implement → verify_review → integrate → done
  execution  whether this run's machinery worked: OK | TOOL_FAILURE (with what failed)
  reviews    whether the required reviews exist for this exact frame: complete | incomplete |
             not_required
  verdict    the judgement about the work: PASS | REQUEST_CHANGES | CONTRACT_GAP | NEEDS_DECISION,
             or None — undecided, which is a legitimate answer while evidence is missing

A verdict is only ever computed from evidence bound to the current frame (repository, base and head
commit, workflow and policy digests, the blobs of the criteria in force). A process exit code, a
file that exists, or a model saying it is done is not evidence of anything by itself.
"""
import hashlib, json, os, re, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import remote as remote_mod  # noqa: E402

PHASES = ["prepare", "implement", "verify_review", "integrate", "done"]
VERDICTS = ["PASS", "REQUEST_CHANGES", "CONTRACT_GAP", "NEEDS_DECISION"]
POLICY_SCHEMA = "devflow/project@1"
HOOK_PROTOCOL = "devflow-hook@1"
STATE_MARK = "<!-- devflow:state"
REVIEW_FENCE = re.compile(r"```devflow-review\s*\n(.*?)\n```", re.S)
TASK_FENCE = re.compile(r"```devflow\s*\n(.*?)\n```", re.S)
STATE_RE = re.compile(r"<!-- devflow:state (\{.*?\}) -->", re.S)

# The implementer devflow runs: a principal of this package (principals.yaml), and the author of
# every commit it pushes. The commit author is a label, not a credential — an implementation is
# attributed to it only for commits the published state records devflow pushing.
IMPLEMENTER = "devflow-implementer"
IMPLEMENTER_EMAIL = "devflow-implementer@devflow.invalid"
IMPLEMENTER_AUTHORS = {IMPLEMENTER, IMPLEMENTER_EMAIL}
# The research lane's role (task types a project routes to `implement.modes`). Its commits carry the
# same author as the implementer's: both are devflow's own, recorded as pushed.
RESEARCHER = "devflow-researcher"
# Who reviews a research-lane record: given what its author was given — the same reference material,
# the criterion's own words, the same web hosts — so it can check the facts, not only their form. An
# identity of its own, so the review stays independent of the researcher.
RESEARCH_REVIEWER = "devflow-research-reviewer"
PR_MARK = "<!-- devflow:pr"


def task_branch(task):
    """The branch devflow implements a task on: devflow/<task id>, in git's safe characters."""
    return "devflow/" + re.sub(r"[^a-z0-9._-]+", "-", str(task.get("id", "")).lower()).strip("-.")

# CI conclusions, sorted by what they say. A failed check is a statement about the code; a
# cancelled or timed-out one is a statement about the machinery, and says nothing about the code.
CHECK_OK = {"success"}
CHECK_FAILED = {"failure"}
CHECK_TOOL = {"cancelled", "timed_out", "startup_failure", "action_required", "stale"}
CHECK_NOT_RUN = {"skipped", "neutral", None}


def sha256(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _yaml(text):
    import yaml
    return yaml.safe_load(text) or {}


# ------------------------------------------------------------------------------ identities
def workflow_identity():
    """The package's version and a digest of every file it runs with — so a verdict says which
    workflow made it, and a change to the workflow is a change to the frame."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(PKG):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", "fixtures", ".git"))
        for f in sorted(files):
            if f.endswith((".pyc",)):
                continue
            p = os.path.join(root, f)
            h.update(os.path.relpath(p, PKG).replace(os.sep, "/").encode())
            with open(p, "rb") as fh:
                h.update(fh.read())
    manifest = _yaml(open(os.path.join(PKG, "manifest.yaml"), encoding="utf-8").read())
    return {"name": manifest.get("name"), "version": str(manifest.get("version")),
            "digest": h.hexdigest()[:16]}


# ------------------------------------------------------------------------------ registration
REG_REQUIRED = ("name", "repository", "canonical_branch", "policy_path", "remote")


def registration(name, root=None):
    """A project's registration: which repository, which canonical branch, where its policy is.

    It lives beside this stack's other instance configuration (config/devflow/projects/). A value
    nobody has confirmed is written as `UNCONFIRMED:<what is known>` and listed in `unconfirmed`;
    such a registration may run against a fixture and is refused against the real remote.
    """
    root = root or os.environ.get("DEVFLOW_PROJECTS", "/work/config/devflow/projects")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", name or ""):
        raise ConfigError(f"a project name is [a-z][a-z0-9-] of 2 to 40, not {name!r}")
    path = os.path.join(root, f"{name}.yaml")
    if not os.path.isfile(path):
        have = sorted(f[:-5] for f in os.listdir(root) if f.endswith(".yaml")) if os.path.isdir(root) else []
        raise ConfigError(f"no project registration {path}", hint=f"registered: {have}")
    reg = _yaml(open(path, encoding="utf-8").read())
    missing = [k for k in REG_REQUIRED if not reg.get(k)]
    if missing:
        raise ConfigError(f"{path}: missing {missing}")
    reg["unconfirmed"] = sorted(set(reg.get("unconfirmed") or []) |
                                {k for k, v in reg.items() if isinstance(v, str) and v.startswith("UNCONFIRMED")})
    reg["_path"] = path
    reg["_digest"] = sha256(open(path, "rb").read())[:16]
    return reg


class ConfigError(Exception):
    def __init__(self, msg, hint=""):
        super().__init__(msg)
        self.hint = hint


def open_remote(reg, ws, proxy_env=None):
    kind = reg["remote"]
    if kind == "github" and reg["unconfirmed"]:
        raise ConfigError(f"project {reg['name']!r} has unconfirmed values {reg['unconfirmed']}; "
                          "it may run against a fixture, not against GitHub",
                          hint=f"confirm them in {reg['_path']}")
    fixture = None
    if kind == "fixture":
        fixture = os.path.join(ws, "devflow", "remote.json")
    repo = reg["repository"]
    if repo.startswith("UNCONFIRMED"):
        repo = reg.get("fixture_repository") or "fixture/project"
    return remote_mod.open_remote(kind, repo, fixture, proxy_env)


# ------------------------------------------------------------------------------ task
def parse_task(issue):
    """The task is the issue: the ```devflow fenced block in its body (the task template)."""
    m = TASK_FENCE.search(issue.get("body") or "")
    if not m:
        return None, "the issue has no ```devflow block (use the task template)"
    try:
        t = _yaml(m.group(1))
    except Exception as e:
        return None, f"the ```devflow block is not YAML: {type(e).__name__}"
    for k in ("id", "type", "goal"):
        if not t.get(k):
            return None, f"the task block has no {k!r}"
    t["issue"] = issue["number"]
    return t, ""


# ------------------------------------------------------------------------------ policy (approved)
def load_policy(rem, reg, ref):
    """The project policy, read from the canonical branch at `ref` — never from the candidate.

    A pull request that edits the policy, the hook or the review instructions is proposing a
    change to the rules; it is not the rules. So everything that decides how a task is judged is
    read at the base commit, and a candidate's edits to those paths are reported as a policy
    change that needs its own review (see classify()).
    """
    text = rem.file_at(ref, reg["policy_path"])
    if text is None:
        raise ConfigError(f"{reg['policy_path']} is not on {reg['canonical_branch']} at {ref[:12]}",
                          hint="the project's policy file must be committed to its canonical branch")
    pol = _yaml(text)
    if pol.get("schema") != POLICY_SCHEMA:
        raise ConfigError(f"policy schema is {pol.get('schema')!r}, this workflow reads {POLICY_SCHEMA!r}")
    pol["_ref"] = ref
    pol["_digest"] = sha256(text)[:16]
    return pol


def materialize_hook(rem, pol, ref, dest):
    """Copy the hook's files, at the approved ref, into this run's workspace. The hook runs from
    there — never from a checkout of the candidate."""
    hook = pol.get("hook") or {}
    os.makedirs(dest, exist_ok=True)
    got = {}
    for rel in hook.get("files") or []:
        text = rem.file_at(ref, rel)
        if text is None:
            raise ConfigError(f"hook file {rel} is not on the canonical branch at {ref[:12]}")
        p = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        got[rel] = sha256(text)[:16]
    return got


def run_hook(pol, hook_root, op, payload, timeout=120):
    """Call the project's hook: one JSON document in on stdin, one out on the last stdout line.

    A hook that crashes, times out or answers with something that is not the protocol is a tool
    failure (HookError) — never a verdict.
    """
    hook = pol.get("hook") or {}
    cmd = [sys.executable if c == "python" else c for c in (hook.get("command") or [])]
    if not cmd:
        raise HookError("the policy names no hook command")
    cmd = [os.path.join(hook_root, c) if c.endswith(".py") else c for c in cmd]
    # the hook is told the approved policy it runs under, so project facts live in one file
    doc = {"protocol": HOOK_PROTOCOL, "op": op,
           "policy": {k: v for k, v in pol.items() if not str(k).startswith("_")}, **payload}
    try:
        p = subprocess.run(cmd, input=json.dumps(doc, ensure_ascii=False), capture_output=True,
                           text=True, timeout=timeout, cwd=hook_root)
    except subprocess.TimeoutExpired:
        raise HookError(f"hook {op} timed out after {timeout}s")
    lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
    try:
        out = json.loads(lines[-1])
    except Exception:
        raise HookError(f"hook {op} exited {p.returncode} without a JSON answer: {(p.stderr or p.stdout)[-300:]}")
    if out.get("protocol") != HOOK_PROTOCOL:
        raise HookError(f"hook {op} answered in protocol {out.get('protocol')!r}")
    if out.get("error"):
        raise HookError(f"hook {op}: {out['error']}")
    return out


class HookError(Exception):
    pass


# ------------------------------------------------------------------------------ classification
def validate_classification(pol, c):
    """What the engine checks of a hook's classification — its shape, not its judgement."""
    problems = []
    classes = pol.get("change_classes") or ["ORDINARY"]
    if c.get("change_class") not in classes:
        problems.append(f"change_class {c.get('change_class')!r} is not one of {classes}")
    types = pol.get("task_types") or []
    if types and c.get("task_type") not in types:
        problems.append(f"task_type {c.get('task_type')!r} is not one of {types}")
    if not isinstance(c.get("impacts"), dict):
        problems.append("impacts is not a mapping")
    for k in ("required_checks", "lenses"):
        if not isinstance(c.get(k, []), list):
            problems.append(f"{k} is not a list")
    for chk, why in (c.get("not_applicable") or {}).items():
        if not str(why or "").strip():
            problems.append(f"not_applicable {chk!r} gives no reason")
    return problems


# ------------------------------------------------------------------------------ frame
def frame_of(reg, pol, wf, pr, canonical_head, criteria_blobs, hook_digests, task):
    """Everything a verdict is about. Any change to any of it and earlier evidence is stale."""
    core = {"project": reg["name"], "repository": reg["repository"],
            "canonical_branch": reg["canonical_branch"], "registration": reg["_digest"],
            "task": str(task.get("id")), "issue": task.get("issue"),
            "pr": pr["number"] if pr else None,
            "base_sha": pr["base_sha"] if pr else canonical_head,
            "head_sha": pr["head_sha"] if pr else None,
            "canonical_head": canonical_head,
            "workflow": wf, "policy": {"ref": pol["_ref"], "digest": pol["_digest"], "hook": hook_digests},
            "criteria": criteria_blobs}
    # canonical_head is recorded but not part of the id: the canonical branch moving does not make
    # a review of the same diff stale — unless it changes the criteria (criteria_blobs is) or the
    # change touches a shared artifact (integration checks that separately).
    ident = {k: v for k, v in core.items() if k != "canonical_head"}
    core["id"] = sha256(canon(ident))[:16]
    # what a review's judgement rests on: the blobs of every pinned criterion and contract. A review
    # names it, so an amended ADR or criterion on canonical makes a person's review stale too — not
    # only a model's, which is bound to the whole frame (a workflow update alone does not).
    core["basis"] = sha256(canon(criteria_blobs))[:12]
    return core


def criteria_blobs(rem, pol, ref):
    """The blob of every official-criteria and contract file the policy names, at the base."""
    out = {}
    for src in (pol.get("authority") or {}).get("criteria", []) + (pol.get("authority") or {}).get("contracts", []):
        for path in src.get("paths") or ([src["path"]] if src.get("path") else []):
            out[path] = rem.blob_id(ref, path) or "absent"
    return out


# ------------------------------------------------------------------------------ checks
def evaluate_checks(required, not_applicable, runs, head_sha, exceptions=None):
    """Required checks against the check runs on exactly this commit.

    Each required check gets one of the workflow's four results (개발 범용 워크플로 §5):
    PASS, FAIL, NOT_RUN or NOT_APPLICABLE. NOT_RUN is never a pass; it carries *why* — pending
    (not finished yet), tool (cancelled / timed out / failed to start: says nothing about the
    code), skipped (the check did not run). NOT_APPLICABLE needs a reason. An approved exception
    keeps the real result and adds the approval; it satisfies the gate for a NOT_RUN check and
    never turns a FAIL into anything else.

    A run on another commit is not evidence for this one, whatever it concluded. The newest run of
    a name counts (a re-run replaces a cancelled one); an earlier success does not rescue a later
    failure.
    """
    latest = {}
    for r in runs:
        if r.get("head_sha") != head_sha:
            continue
        prev = latest.get(r["name"])
        if prev is None or str(r.get("completed_at") or "~") >= str(prev.get("completed_at") or "~"):
            latest[r["name"]] = r
    exceptions = exceptions or {}
    out = {"results": {}, "passed": [], "failed": [], "tool": [], "pending": [], "not_run": [],
           "not_applicable": {}, "excepted": {}}
    for name in required:
        if name in (not_applicable or {}):
            out["not_applicable"][name] = not_applicable[name]
            out["results"][name] = {"result": "NOT_APPLICABLE", "why": not_applicable[name]}
            continue
        r = latest.get(name)
        if r is None or r.get("status") != "completed":
            res, bucket = {"result": "NOT_RUN", "why": "pending"}, "pending"
        elif r.get("conclusion") in CHECK_OK:
            res, bucket = {"result": "PASS"}, "passed"
        elif r.get("conclusion") in CHECK_FAILED:
            res, bucket = {"result": "FAIL"}, "failed"
        elif r.get("conclusion") in CHECK_TOOL:
            res, bucket = {"result": "NOT_RUN", "why": f"tool: {r.get('conclusion')}"}, "tool"
        else:
            res, bucket = {"result": "NOT_RUN", "why": f"skipped: {r.get('conclusion')}"}, "not_run"
        if r is not None:
            res["run"] = r.get("id")
        if res["result"] == "NOT_RUN" and name in exceptions:
            res["exception"] = exceptions[name]
            out["excepted"][name] = exceptions[name]
            bucket = "passed"
        out["results"][name] = res
        out[bucket].append(name)
    out["complete"] = not (out["failed"] or out["tool"] or out["pending"] or out["not_run"])
    return out


# ------------------------------------------------------------------------------ lenses
def load_lenses(pol, hook_root):
    """The common lens library, plus the project's own lenses (files at the approved ref)."""
    lib = _yaml(open(os.path.join(PKG, "lenses", "library.yaml"), encoding="utf-8").read())
    lenses = {l["id"]: dict(l, source="devflow") for l in lib.get("lenses", [])}
    base = lib.get("base") or {}
    for rel in (pol.get("reviews") or {}).get("lens_files") or []:
        extra = _yaml(open(os.path.join(hook_root, rel), encoding="utf-8").read())
        for l in extra.get("lenses", []):
            if l["id"] in lenses and lenses[l["id"]]["source"] == "devflow":
                raise ConfigError(f"project lens {l['id']!r} would replace a common lens; name it differently")
            lenses[l["id"]] = dict(l, source=rel)
    return base, lenses


def _applies(when, classification):
    """A lens's `when`: any of change_classes / impacts / task_types matches. Empty = never
    automatically (it can still be required by the hook by id)."""
    if not when:
        return False
    imp = classification.get("impacts") or {}
    if classification.get("change_class") in (when.get("change_classes") or []):
        return True
    if classification.get("task_type") in (when.get("task_types") or []):
        return True
    return any(imp.get(k) for k in (when.get("impacts") or []))


def select_lenses(base, lenses, classification):
    """The base review always; each specialist lens only when its condition holds, or the hook
    asks for it by id. The number of lenses says nothing about how many reviewers there are."""
    chosen = {"base": dict(base, id="base")}
    reasons = {"base": "always"}
    for lid, l in lenses.items():
        if _applies(l.get("when"), classification):
            chosen[lid], reasons[lid] = l, "condition"
    for lid in classification.get("lenses") or []:
        if lid not in lenses:
            raise ConfigError(f"the hook asks for lens {lid!r}, which no lens file defines")
        chosen.setdefault(lid, lenses[lid])
        reasons.setdefault(lid, "hook")
    return chosen, reasons


def assign_reviewers(chosen, pol, classification):
    """Group lenses into reviewer assignments. A lens that must be independent of the others
    (Foundation) always gets its own assignment. The rest go to one reviewer (`reviews.assignment:
    together`, the default) or to one reviewer each (`per_lens`), which a project chooses when one
    reviewer reading every lens's materials does not fit the execution profile's time limit — each
    reviewer then reads only what its lens lists. The number of reviewers is a cost and time choice,
    never a claim that more reviewers make a better review."""
    rv = pol.get("reviews") or {}
    independent = set(rv.get("independent_lenses") or [])
    if classification.get("change_class") in (rv.get("independent_for") or ["FOUNDATION"]):
        independent |= set(rv.get("independent_lenses_foundation") or ["foundation"])
    shared = sorted(l for l in chosen if l not in independent)
    if rv.get("assignment") == "per_lens":
        groups = [{"reviewer": f"r{i}", "lenses": [l], "independent": False} for i, l in enumerate(shared, 1)]
    else:
        groups = [{"reviewer": "r1", "lenses": shared, "independent": False}] if shared else []
    for i, l in enumerate(sorted(l for l in chosen if l in independent), len(groups) + 1):
        groups.append({"reviewer": f"r{i}", "lenses": [l], "independent": True})
    return groups


def review_model(r):
    """The model family a counted review came from — the provider of a model review (from the
    receipt, never from the text), or `human` for a person's."""
    who = r.get("reviewer") or {}
    return who.get("provider") or ("human" if who.get("kind", "human") == "human" else "model")


def distinct_models_needed(pol, task_type):
    """How many different model families must have completed each lens (`reviews.distinct_models`,
    by task type; 1 when the policy says nothing). Not independence — a separate identity is that —
    but a guard against one model being wrong the same way twice."""
    return max(1, int(((pol.get("reviews") or {}).get("distinct_models") or {}).get(task_type) or 1))


# ------------------------------------------------------------------------------ reviews
def parse_review(body):
    m = REVIEW_FENCE.search(body or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return {"_malformed": True}


def validate_review(r, frame, lens_defs, implementers):
    """Whether a review counts for this frame, and for which lenses.

    It counts only when it names this frame's head and policy digest, says who wrote it, and lists
    what it read. A lens whose required reading was not read is *unverified*, not failed: the
    review is kept, the lens is not complete, and nothing is invented to fill the gap.
    """
    why = []
    if r.get("_malformed"):
        return {"valid": False, "why": ["the devflow-review block is not JSON"]}
    if r.get("head_sha") != frame["head_sha"]:
        why.append(f"written for head {str(r.get('head_sha'))[:12]}, the head is {str(frame['head_sha'])[:12]}")
    if r.get("frame_id") and r.get("frame_id") != frame["id"]:
        why.append("written for another frame (policy, criteria or workflow changed since)")
    if r.get("policy_digest") != frame["policy"]["digest"]:
        why.append("written under another policy version")
    if r.get("basis_digest") != frame.get("basis"):
        why.append(f"written against other criteria or contracts (basis {r.get('basis_digest')!r}, "
                   f"now {frame.get('basis')!r})")
    who = r.get("reviewer") or {}
    if not who.get("identity"):
        why.append("does not say who reviewed")
    if why:
        return {"valid": False, "why": why}
    complete, unverified = [], {}
    for lid in r.get("lenses") or []:
        need = set((lens_defs.get(lid) or {}).get("read") or [])
        read = {m["ref"] for m in r.get("materials") or [] if m.get("read")}
        missing = sorted(need - read)
        answered = set((r.get("answers") or {}).get(lid, {}).keys())
        questions = [q["id"] for q in (lens_defs.get(lid) or {}).get("questions") or []]
        unanswered = [q for q in questions if q not in answered]
        if missing or unanswered or (r.get("unverified") or {}).get(lid):
            unverified[lid] = {"not_read": missing, "unanswered": unanswered,
                               "declared": (r.get("unverified") or {}).get(lid) or []}
        else:
            complete.append(lid)
    independent = is_independent(who, implementers)
    # `limits` — how far an answered lens reaches (what it relied on) — is shown, never a gap
    return {"valid": True, "complete": complete, "unverified": unverified,
            "limits": r.get("limits") or {},
            "independent": independent["independent"], "independence": independent,
            "findings": [f for f in r.get("findings") or []], "verdict": r.get("verdict"),
            "reviewer": who}


def is_independent(who, implementers):
    """Separation from the implementation: a different identity that did not author the change,
    and — for a model — a fresh session given only the frozen materials.

    A different model or vendor is *not* independence by itself: the same principal driving two
    vendors is one reviewer wearing two hats, and the rule below never looks at the vendor.
    """
    ident = str(who.get("identity") or "")
    reasons = []
    if not ident:
        reasons.append("no identity")
    if ident in set(implementers.get("identities") or []):
        reasons.append(f"{ident} is an implementer of this change")
    if who.get("principal") and who.get("principal") in set(implementers.get("principals") or []):
        reasons.append(f"principal {who.get('principal')} implemented this change")
    if who.get("session") and who.get("session") in set(implementers.get("sessions") or []):
        reasons.append("the review ran in the implementing session")
    if who.get("kind") == "model" and not who.get("fresh_context"):
        reasons.append("a model reviewer that was not started with only the frozen materials")
    return {"independent": not reasons, "why": reasons}


def finding_fingerprint(f):
    basis = f.get("basis") or {}
    return sha256(canon([f.get("lens"), f.get("claim", "").strip().lower(), basis.get("ref")]))[:12]


def classify_findings(findings):
    """A blocking finding must point at something: a criterion, a contract, an observation, a
    test. One that points at nothing is kept and shown, and does not block — a reviewer who could
    not verify something says so in `unverified`, rather than asserting a defect."""
    blocking, unsupported, advisory = [], [], []
    for f in findings:
        basis = f.get("basis") or {}
        if f.get("severity") == "blocking":
            (blocking if basis.get("ref") and basis.get("kind") else unsupported).append(f)
        else:
            advisory.append(f)
    return blocking, unsupported, advisory


# ------------------------------------------------------------------------------ adjudication
def adjudicate(ctx):
    """The state of a task, from what this run established. Deterministic; no model; no
    file-exists test; nothing an earlier attempt left behind.

    ctx (all computed by the steps from the remote, for the current frame):
      prepare   {gaps, decisions, problems}   the hook's and the engine's findings about the task
      gates     [{id, phase, approved, value}] human gates this change class carries
      pr        the pull request, or None
      checks    evaluate_checks() on the head, or None
      trace     the hook's trace validation, or None
      reviews   {required, results, independent_needed, independent_lenses}
      shared    {required, rebased, ops_match, detail}
      finalize  {post, delivery, passed_head, merge_commit}  — only for a merged pull request
      history   {rework, no_progress, last_blocking, max_rework, max_no_progress}
      tool      [errors]                      machinery that failed this run
      end_point integration | delivery

    Returns the phase (준비/구현/검증·리뷰/통합·출시/완료 = prepare/implement/verify_review/
    integrate/done), the progress (active / waiting / stopped) with the wait's reason, need, owner
    and resume condition, the execution status, the review status, the verdict (or None) and the
    blockers.
    """
    tool = list(ctx.get("tool") or [])
    blockers, notes = [], []
    prep = ctx.get("prepare") or {}
    hist = ctx.get("history") or {}

    def out(phase, verdict, review_status="not_required", wait=None, progress=None):
        return {"phase": phase, "verdict": verdict,
                "progress": progress or ("waiting" if wait else "active"), "wait": wait,
                "execution": {"status": "TOOL_FAILURE" if tool else "OK", "errors": tool},
                "reviews": {"status": review_status, **(ctx.get("_review_detail") or {})},
                "blockers": blockers, "notes": notes,
                "next": (wait or {}).get("need") or ("integrate" if phase == "integrate" else "")}

    def wait(reason, need, owner, resume):
        return {"reason": reason, "need": need, "owner": owner, "resume_when": resume}

    # 1. what preparation found stands: it is about the task, not about the code
    research = list(prep.get("research") or [])
    research_lines = [f"research needed: {q.get('id')} — {q.get('question')}" for q in research]
    if prep.get("decisions"):
        blockers.extend(f"decision: {d}" for d in prep["decisions"])
        blockers.extend(research_lines)
        return out("prepare", "NEEDS_DECISION", wait=wait(
            "decision", "answer the listed decisions on the issue", "decision owner",
            "the issue records the decision"))
    if prep.get("gaps"):
        blockers.extend(f"contract gap: {g}" for g in prep["gaps"])
        blockers.extend(research_lines)
        return out("prepare", "CONTRACT_GAP", wait=wait(
            "contract_gap", "analyse the gap in preparation, and whether a Foundation change is needed",
            "task owner", "the task is updated with the analysis"))
    if research:
        # a fact the item needs and no merged record establishes: not a verdict about anything —
        # the item waits for the research lane, and only for the questions it named
        blockers.extend(research_lines)
        ids = ", ".join(str(q.get("id")) for q in research)
        return out("prepare", None, wait=wait(
            "research", f"a research task that answers {ids}, merged under the project's research directory",
            "research lane", f"a merged research record lists {ids} under `answers:` (or `unanswered:`)"))
    if prep.get("problems"):
        blockers.extend(prep["problems"])
        return out("prepare", None, wait=wait(
            "task", "fix the task block or the project configuration", "task owner",
            "the task block is valid"))
    for g in ctx.get("gates") or []:
        if g.get("phase") == "prepare" and not g.get("approved"):
            blockers.append(f"human gate {g['id']} is not approved for {str(g.get('value'))[:12]}")
            return out("prepare", None, wait=wait(
                "decision", f"/devflow approve {g['id']} {str(g.get('value'))[:12]}",
                "an approver the policy names", "the approval comment is on the issue"))

    pr = ctx.get("pr")

    # 2. after integration: the checks that apply to the commit that landed
    fin = ctx.get("finalize")
    if pr and pr.get("merged") and fin is not None:
        post = fin.get("post") or {}
        if not fin.get("passed_head"):
            blockers.append(fin.get("why_no_pass") or "merged without a devflow PASS for any head")
            return out("integrate", "NEEDS_DECISION", wait=wait(
                "decision", "the merge happened outside the workflow: review the merged change or revert",
                "merge owner", "a decision is recorded"))
        if fin["passed_head"] != pr.get("head_sha"):
            blockers.append(f"merged at head {str(pr.get('head_sha'))[:12]}, but PASS was for {fin['passed_head'][:12]}")
            return out("integrate", "NEEDS_DECISION", wait=wait(
                "decision", "what was merged is not what was judged: re-review the merged head or revert",
                "merge owner", "a decision is recorded"))
        if post.get("failed"):
            blockers.extend(f"post-merge check FAIL on {str(fin.get('merge_commit'))[:12]}: {c}" for c in post["failed"])
            return out("integrate", "NEEDS_DECISION", wait=wait(
                "verification", "the integrated commit fails verification: fix forward or revert",
                "merge owner", "the canonical head passes the post-merge checks"))
        if post.get("tool"):
            tool.extend(f"post-merge check {c} ended without a result about the code" for c in post["tool"])
        if not post.get("complete"):
            notes.append(f"post-merge checks not complete on {str(fin.get('merge_commit'))[:12]}: "
                         f"{post.get('pending', []) + post.get('tool', []) + post.get('not_run', [])}")
            return out("integrate", "PASS", wait=wait(
                "environment" if post.get("tool") else "verification",
                "post-merge verification and artifact on the merge commit", "CI",
                "the post-merge checks complete"))
        if ctx.get("end_point") == "delivery" and not (fin.get("delivery") or {}).get("complete"):
            return out("integrate", "PASS", wait=wait(
                "delivery", "deliver and confirm, as the task's scope says", "release owner",
                "delivery evidence is on the merge commit"))
        return out("done", "PASS", progress="stopped")

    # 3. does an implementation exist? (a head holding only preparation is not one)
    if not pr or not pr.get("head_sha") or pr.get("head_sha") == pr.get("base_sha") or ctx.get("prepare_only"):
        flow = ctx.get("flow") or {}
        return out("implement", None, wait=wait(
            "implementation",
            flow.get("need") or "open a pull request and name it in the task block (pr: <n>)",
            flow.get("owner") or "implementer",
            flow.get("resume_when") or "the task names an open pull request"))
    if pr.get("state") != "open":
        blockers.append("the pull request is closed without merge")
        return out("implement", "NEEDS_DECISION", wait=wait(
            "decision", "reopen it, or record why the task ends here", "task owner", "a decision is recorded"))

    # 4. evidence about the code
    checks = ctx.get("checks") or {}
    results = checks.get("results") or {}
    tool.extend(f"check {c} ended without a result about the code ({results[c]['why']})"
                for c in checks.get("tool") or [])
    semantic_fail = [f"required check FAIL on head: {c}" for c in checks.get("failed") or []]
    semantic_fail.extend(f"violation: {v}" for v in ctx.get("violations") or [])
    skipped = [f"required check NOT_RUN on head ({results[c]['why']}): {c}" for c in checks.get("not_run") or []]
    trace = ctx.get("trace")
    if trace is not None:
        semantic_fail.extend(f"trace: {p}" for p in trace.get("problems") or [])
    shared = ctx.get("shared") or {}
    if shared.get("required") and shared.get("rebased") and shared.get("ops_match") is False:
        semantic_fail.append(f"shared artifact: the operations differ from the expected set: {shared.get('detail')}")

    rv = ctx.get("reviews") or {}
    required = list(rv.get("required") or [])
    valid = [r for r in rv.get("results") or [] if r.get("valid")]
    if ctx.get("lane") == "research":
        # on a research record, what a reviewer declares it could not verify holds the lens only
        # through a blocking finding that names what it endangers; declared alone, it is a limit.
        # Material not read and questions not answered still leave the lens incomplete.
        eased = []
        for i, r in enumerate(valid):
            soft = {lid for lid, u in (r.get("unverified") or {}).items()
                    if isinstance(u, dict) and u.get("declared") and not u.get("not_read") and not u.get("unanswered")}
            if soft:
                eased += [f"{lid}: {d}" for lid in sorted(soft) for d in r["unverified"][lid]["declared"]]
                valid[i] = dict(r, complete=sorted(set(r["complete"]) | soft),
                                unverified={l: u for l, u in r["unverified"].items() if l not in soft})
        if eased:
            notes.append(f"{len(eased)} declared unverified scope(s) on the record stay noted as limits: "
                         + "; ".join(e[:160] for e in eased[:3]))
    covered, unverified = set(), {}
    need_models = int(rv.get("distinct_models") or 1)
    by_lens = {}
    for r in valid:
        for l in r["complete"]:
            by_lens.setdefault(l, set()).add(review_model(r))
        for lid, u in (r.get("unverified") or {}).items():
            unverified.setdefault(lid, u)
    covered = {l for l, models in by_lens.items() if len(models) >= need_models}
    short = {l: sorted(m) for l, m in by_lens.items() if l in required and len(m) < need_models}
    if short:
        notes.append(f"lenses completed by fewer than {need_models} different models: {short}")
    missing = [l for l in required if l not in covered and l not in unverified]
    unverified = {l: u for l, u in unverified.items() if l not in covered}
    indep_lenses = set(rv.get("independent_lenses") or [])
    independent_ok = (not rv.get("independent_needed") or
                      indep_lenses <= {l for r in valid if r["independent"] for l in r["complete"]})
    findings = [dict(f, lens=f.get("lens") or (r["complete"] or ["base"])[0],
                     by=(r.get("reviewer") or {}).get("identity"))
                for r in valid for f in r["findings"]]
    if ctx.get("lane") == "research":
        # a research record goes round again only for what its items need: a blocking finding that
        # names no question or item it endangers is shown as advisory
        loose = [f for f in findings if f.get("severity") == "blocking" and not str(f.get("affects") or "").strip()]
        if loose:
            notes.append(f"{len(loose)} blocking finding(s) on the research record name nothing they endanger "
                         "(`affects`): shown as advisory")
        findings = [dict(f, severity="advisory") if f in loose else f for f in findings]
    blocking, unsupported, advisory = classify_findings(findings)
    if ctx.get("lane") == "research" and advisory:
        notes.append(f"{len(advisory)} advisory finding(s) on the record stay noted; they do not hold it")
    fps = sorted({finding_fingerprint(f) for f in blocking})
    ctx["_review_detail"] = {"required": required, "covered": sorted(covered), "missing": missing,
                             "unverified": unverified, "independent_ok": independent_ok,
                             "blocking": fps, "unsupported": len(unsupported)}
    review_status = ("complete" if not (missing or unverified or not independent_ok)
                     else "incomplete") if required else "not_required"
    if unsupported:
        notes.append(f"{len(unsupported)} blocking claim(s) without a basis are shown and do not block")
    reviewer_verdicts = {r.get("verdict") for r in valid}

    # 5. the verdict with the strongest basis first. A violation found with a basis is
    #    REQUEST_CHANGES even while other lenses are open: the implementer can act on it now.
    if semantic_fail or blocking:
        blockers.extend(semantic_fail)
        blockers.extend(f"finding [{f.get('lens')}] {f.get('claim')} (basis {f['basis'].get('kind')}:{f['basis'].get('ref')})"
                        for f in blocking)
        cond = sorted(set(fps) | {sha256(s)[:12] for s in semantic_fail})
        same = set(cond) <= set(hist.get("last_blocking") or [])
        ctx["_blocking_now"] = cond
        # progress is measured between heads: judging the same head again is not another round
        this_head = str((ctx.get("pr") or {}).get("head_sha") or "")
        if this_head and hist.get("last_rc_head") == this_head:
            ctx["_no_progress"] = int(hist.get("no_progress") or 0)
        else:
            ctx["_no_progress"] = int(hist.get("no_progress") or 0) + 1 if same else 0
        if ctx["_no_progress"] >= int(hist.get("max_no_progress") or 3):
            notes.append(f"the same blocking condition has come back {ctx['_no_progress']} time(s) without progress")
            return out("verify_review", "NEEDS_DECISION", review_status, wait(
                "decision", "a person decides between the implementation and the finding; no further automatic round",
                "decision owner", "a decision is recorded, or new evidence is added"))
        max_rework = hist.get("max_rework")
        granted = int(ctx.get("rework_granted") or 0)
        if max_rework is not None and int(hist.get("rework") or 0) >= int(max_rework) + granted:
            notes.append(f"the automatic rework limit ({max_rework}"
                         + (f", plus {granted} granted" if granted else "") + ") is reached")
            head = str((ctx.get("pr") or {}).get("head_sha") or "")[:12]
            # the findings stand: running out of automatic rounds is an operating limit, not a
            # decision about the change — the verdict stays what the evidence says
            return out("verify_review", "REQUEST_CHANGES", review_status, wait(
                "budget", "the automatic rework rounds are spent and the blocking findings stand: a person "
                f"fixes them, or grants one more round with `/devflow approve rework {head}`",
                "task owner", "a new head commit, or a grant"))
        if granted:
            notes.append(f"{granted} automatic rework round(s) granted by a person beyond the project's limit")
        return out("implement", "REQUEST_CHANGES", review_status, wait(
            "implementation", "fix the listed items and push", "implementer", "a new head commit"))
    if "CONTRACT_GAP" in reviewer_verdicts:
        blockers.extend(f"a review reports a contract gap: {(r.get('reviewer') or {}).get('identity')}"
                        for r in valid if r.get("verdict") == "CONTRACT_GAP")
        return out("prepare", "CONTRACT_GAP", review_status, wait(
            "contract_gap", "return to preparation: analyse the gap and whether a Foundation change is needed",
            "task owner", "the task is updated"))
    if "NEEDS_DECISION" in reviewer_verdicts:
        blockers.append("a reviewer asks for a decision")
        return out("verify_review", "NEEDS_DECISION", review_status, wait(
            "decision", "answer the reviewer's decision on the issue", "decision owner", "a decision is recorded"))
    if tool:
        return out("verify_review", None, review_status, wait(
            "environment", "the machinery failed and nothing was judged: fix it or run it again",
            "operator", "the failed check or call runs to a result"))
    if checks.get("pending"):
        notes.append(f"checks pending on head: {checks['pending']}")
        return out("verify_review", None, review_status, wait(
            "verification", "CI on the head commit", "CI", "the required checks complete"))
    if skipped:
        blockers.extend(skipped)
        return out("verify_review", None, review_status, wait(
            "verification", "make the skipped required checks run, or record an approved exception",
            "task owner", "every required check has a result"))
    if shared.get("required") and not shared.get("rebased"):
        blockers.append("shared artifact: the change is not on the latest canonical head")
        return out("implement", None, review_status, wait(
            "implementation", "update onto the canonical head and regenerate the shared artifact",
            "implementer", "the head contains the canonical head"))
    if missing or unverified or not independent_ok:
        if missing:
            blockers.append(f"reviews missing for this head: {missing}")
        if unverified:
            blockers.append(f"lenses with unverified scope: {sorted(unverified)}")
        if not independent_ok:
            blockers.append("the independent review (an identity separate from the implementer, on this head) is missing")
        head = str((pr or {}).get("head_sha") or "")[:12]
        if ctx.get("reviews_budget_spent"):
            notes.append(f"model reviews stopped: {ctx['reviews_budget_spent']}")
            return out("verify_review", None, review_status, wait(
                "budget", "the model review budget or round limit is spent for this task: a person reviews "
                f"this head, or grants one more model round with `/devflow approve reviews {head}`",
                "reviewer", "the reviews are on this head, or a grant"))
        return out("verify_review", None, review_status, wait(
            "review", "complete the required reviews for this head — a person, or, when the model "
            f"budget or round limit is spent, `/devflow approve reviews {head}` for one more model round",
            "reviewer", "the reviews are on this head"))
    if shared.get("required") and shared.get("ops_match") is not True:
        blockers.append("shared artifact: the operation comparison has no result")
        return out("verify_review", None, review_status, wait(
            "verification", "compare the artifact's operations", "operator", "the comparison runs"))
    for g in ctx.get("gates") or []:
        if g.get("phase") == "integrate" and not g.get("approved"):
            blockers.append(f"human gate {g['id']} is not approved for {str(g.get('value'))[:12]}")
            return out("integrate", "PASS", review_status, wait(
                "decision", f"/devflow approve {g['id']} {str(g.get('value'))[:12]}",
                "an approver the policy names", "the approval comment is on the issue"))
    return out("integrate", "PASS", review_status)


# ------------------------------------------------------------------------------ published state
def render_state(state):
    """The state as it is published on the issue: readable text for a person, and a machine copy
    in an HTML comment that the next run reads back (history, not authority)."""
    v = state.get("verdict") or "undecided"
    f = state.get("frame") or {}
    wf, pol = f.get("workflow") or {}, f.get("policy") or {}
    lines = [f"**devflow** · phase `{state['phase']}` ({state.get('progress', '')}) · verdict **{v}**"
             f" · execution `{state['execution']['status']}` · reviews `{state['reviews']['status']}`",
             f"frame `{f.get('id', '')}` · head `{str(f.get('head_sha'))[:12]}` · base `{str(f.get('base_sha'))[:12]}`"
             f" · canonical `{str(f.get('canonical_head'))[:12]}` · workflow {wf.get('version', '')}/{wf.get('digest', '')}"
             f" · policy `{pol.get('digest', '')}` @ `{str(pol.get('ref'))[:12]}`"
             f" · basis `{f.get('basis', '')}` (a review names head, policy_digest and basis_digest)"]
    for name, r in sorted(((state.get("checks") or {}).get("results") or {}).items()):
        extra = f" — {r['why']}" if r.get("why") else ""
        extra += f" (exception: {r['exception']})" if r.get("exception") else ""
        lines.append(f"- check `{name}`: {r['result']}{extra}")
    for b in state.get("blockers") or []:
        lines.append(f"- blocker: {b}")
    for n in state.get("notes") or []:
        lines.append(f"- note: {n}")
    for e in state["execution"].get("errors") or []:
        lines.append(f"- tool: {e}")
    w = state.get("wait")
    if w:
        lines.append(f"\nwaiting ({w['reason']}): {w['need']} · owner: {w['owner']} · resumes when: {w['resume_when']}")
    machine = {k: state[k] for k in ("schema", "phase", "progress", "verdict", "wait", "execution", "reviews",
                                     "frame", "history", "ops", "accepted_reviews", "run") if k in state}
    return "\n".join(lines) + f"\n\n{STATE_MARK} {canon(machine)} -->"


def read_state(comments):
    """The last published state on the thread, or {}. History, not authority: every verdict is
    recomputed from evidence; only counters (rounds, budget) and completed-operation keys carry."""
    for c in reversed(comments):
        m = STATE_RE.search(c.get("body") or "")
        if m:
            try:
                return dict(json.loads(m.group(1)), _comment_id=c["id"])
            except Exception:
                continue
    return {}


def op_key(frame, op, *parts):
    return sha256(canon([frame["project"], frame["task"], op, frame.get("head_sha"), *parts]))[:16]
