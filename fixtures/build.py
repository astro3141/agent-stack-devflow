"""Fixture remotes for devflow: a scanner-shaped repository on a remote that is one JSON file.

usage:
  build.py write                       regenerate fixtures/remotes/*.json from this file
  build.py merge <remote.json> <pr>    a person merges the pull request (and CI reports on the
                                       merge commit) — what happens between two runs in real life
  build.py push <remote.json> <pr>     the implementer pushes a new head to the pull request

Every fixture repository carries the *snapshot* of the infra scanner's devflow policy in
fixtures/scanner-policy/ (taken from infra-scanner-io's devflow/adopt-workflow branch; SOURCE says
which commit). A fixture is evidence about devflow's machinery, never about the scanner.

Used as a library by controls.py, which builds each risk scenario by changing one thing in
`scanner_pass()`.
"""
import copy, hashlib, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
POLICY_DIR = os.path.join(HERE, "scanner-policy")
REPO = "fixture/infra-scanner"
CANONICAL = "recovery/phase-5.3-b1"


def _read(rel):
    return open(os.path.join(POLICY_DIR, rel), encoding="utf-8").read()


def policy_files():
    return {p: _read(p) for p in (".devflow/project.yaml", ".devflow/lenses.yaml", ".devflow/hooks/scanner_hook.py")}


def policy_digest():
    return hashlib.sha256(_read(".devflow/project.yaml").encode()).hexdigest()[:16]


PLAN_HEAD = "#!collection-plan\t1\t{n}"
OPS = [("op-000000000001", "config_kv", "1", "/etc/login.defs", "key=PASS_MAX_DAYS"),
       ("op-000000000002", "file_metadata", "1", "/etc/hosts", "-"),
       ("op-000000000003", "service_state", "1", "telnet", "-")]
NEW_OP = ("op-0000000000f4", "service_state", "1", "vsftpd", "-")


def plan(ops):
    return "\n".join([PLAN_HEAD.format(n=len(ops))] + ["\t".join(o) for o in ops]) + "\n"


TRACE = """task: T-0054
mappings:
  - id: C1
    condition: the unencrypted FTP service is disabled (양호) / enabled (취약)
    source: {criterion: kisa-2026, clause: U-54, page: 131, quote: "암호화되지 않은 FTP 서비스가 비활성화된 경우 양호"}
    status: confirmed
    status_basis: compared with the PDF p.131 text on 2026-09-25 by rev-person
    evidence: [service.state]
    primitive: service_state
    check: service_active_requires_review
    reuse_basis: the check's fixed meaning (active → REVIEW) matches a condition whose FTPS exception is not observed
    binding: bindings/linux/u54.yaml
    item: U-54
    expected: REVIEW
    outcomes: {not_applicable: NA, target_absent: NA, collection_failed: UNKNOWN, insufficient_evidence: UNKNOWN}
    fixtures: [tests/test_u54_ftp.py]
expected_plan_ops:
  collection-plan:
    added: [{primitive: service_state, target: vsftpd, params: "-"}]
    removed: []
count_assertions:
  - {file: tests/test_u54_ftp.py, from: 3, to: 4, because: C1 adds one service_state operation}
"""

TASK = """## 해결할 문제
U-54 unencrypted FTP.

```devflow
id: T-0054
type: feature
goal: implement KISA U-54 with the existing service_state primitive
change_class: ORDINARY
impacts: [item_changed]
items: [U-54]
pr: 7
implementer:
  identities: [impl-bot]
```
"""


def sha(text):
    return hashlib.sha1(text.encode()).hexdigest()


def base_files():
    f = policy_files()
    f.update({"collection-plan.tsv": plan(OPS), "dist/collection-plan.tsv": plan(OPS),
              "items/kisa/U-01.yaml": "id: U-01\n", "CLAUDE.md": "# Infra Scanner\n",
              "primitives.yaml": "primitives: []\n",
              "docs/baselines/current/주요정보통신기반시설 기술적 취약점 분석·평가 방법 상세가이드.pdf": "PDF-KISA-2026",
              "docs/baselines/current/전자금융기반시설 보안 취약점 평가기준(제2026-1호)_2026-03개정.xlsx": "XLSX-EFIN",
              "docs/SPECIFICATION_v0.3.md": "# Spec\n", "evidence_keys.yaml": "keys: []\n",
              # the rest of what the policy pins as contracts (authority.contracts)
              "docs/TECHNICAL_DESIGN_v0.3.md": "# TD\n", "docs/TD_tail.md": "# TD tail\n",
              "docs/adr/collection-plan-transitional-architecture.md": "# ADR plan\n",
              "docs/adr/existing-primitive-semantic-parity.md": "# ADR parity\n",
              "evaluator/check_registry.py": "CHECK_REGISTRY = {}\n",
              "evaluator/applicability_registry.py": "APPLICABILITY_REGISTRY = {}\n"})
    return f


def head_files(base):
    f = dict(base)
    ops = OPS + [NEW_OP]
    f.update({"items/kisa/U-54.yaml": "id: U-54\nbaseline_status: confirmed\n",
              "bindings/linux/u54.yaml": "produces: [service.state]\n",
              "collection-plan.tsv": plan(ops), "dist/collection-plan.tsv": plan(ops),
              "tests/test_u54_ftp.py": "assert len(ops) == 4\n",
              ".devflow/tasks/T-0054.yaml": TRACE})
    return f


CHECKS = ["scanner-quick", "devflow-policy", "scanner-publish", "plan-ops"]


def check_runs(names=CHECKS, conclusion="success", at="2026-09-25T10:00:00Z"):
    return [{"name": n, "status": "completed", "conclusion": conclusion, "completed_at": at, "id": i + 1}
            for i, n in enumerate(names)]


def lens_questions():
    import yaml
    lib = yaml.safe_load(open(os.path.join(PKG, "lenses", "library.yaml"), encoding="utf-8"))
    proj = yaml.safe_load(_read(".devflow/lenses.yaml"))
    out = {"base": [q["id"] for q in lib["base"]["questions"]]}
    for l in lib["lenses"] + proj["lenses"]:
        out[l["id"]] = [q["id"] for q in l["questions"]]
    return out


def basis_of(files):
    """The basis digest devflow computes for a canonical tree: the blob of every criterion and
    contract its policy pins (dfcore.frame_of), with the fixture remote's blob ids (sha1 of text)."""
    import yaml
    sys.path.insert(0, os.path.join(PKG, "lib"))
    import dfcore
    pol = yaml.safe_load(files[".devflow/project.yaml"])
    auth = pol.get("authority") or {}
    blobs = {}
    for src in (auth.get("criteria") or []) + (auth.get("contracts") or []):
        for p in src.get("paths") or []:
            blobs[p] = sha(files[p]) if p in files else "absent"
    return dfcore.sha256(dfcore.canon(blobs))[:12]


def review_body(head, lenses, reads=("diff", "task", "trace", "criteria", "contracts", "artifact_ops"),
                verdict="PASS", findings=(), unverified=None, digest=None, basis=None):
    qs = lens_questions()
    doc = {"head_sha": head, "policy_digest": digest or policy_digest(),
           "basis_digest": basis or basis_of(base_files()), "lenses": list(lenses),
           "materials": [{"ref": r, "read": True} for r in reads],
           "answers": {l: {q: "checked against the materials" for q in qs[l]} for l in lenses},
           "findings": list(findings), "unverified": unverified or {}, "verdict": verdict}
    return "Review.\n\n```devflow-review\n" + json.dumps(doc, ensure_ascii=False, indent=1) + "\n```\n"


LENSES_PASS = ["base", "criteria-trace", "shared-artifact", "scanner-outcomes"]


def scanner_pass():
    """An ordinary item change (U-54 on an existing Primitive), with its PR, green checks on the
    head, a trace, the expected Plan operation, and a complete review by a separate person."""
    base, head = base_files(), None
    hf = head_files(base)
    c0, h1 = "c0" + sha(json.dumps(base, sort_keys=True))[:38], "h1" + sha(json.dumps(hf, sort_keys=True))[:38]
    return {"repos": {REPO: {
        "default_branch": CANONICAL,
        "branches": {CANONICAL: c0},
        "commits": {c0: {"parents": [], "files": base, "author": "astro3141"},
                    h1: {"parents": [c0], "files": hf, "author": "impl-bot"}},
        "pulls": {"7": {"number": 7, "state": "open", "merged": False, "merge_commit_sha": None,
                        "head_sha": h1, "head_ref": "7-implement-u54", "base_ref": CANONICAL,
                        "base_sha": c0, "author": "impl-bot", "body": "", "comments": []}},
        "issues": {"3": {"title": "U-54", "body": TASK, "labels": [], "comments": [], "state": "open"}},
        "check_runs": {h1: check_runs()},
        "reviews": {"7": [{"id": 101, "user": "rev-person", "commit_id": h1, "state": "COMMENTED",
                           "body": review_body(h1, LENSES_PASS)}]},
        "statuses": {}, "refs": {},
        "post_merge_check_runs": check_runs(["scanner-quick", "scanner-publish", "devflow-policy"]),
        "features": {"merge_queue": False, "protection": {"rules": [], "classic": False}}}},
        "faults": {}}


def ids(db):
    r = db["repos"][REPO]
    return r, r["pulls"]["7"]["head_sha"], r["branches"][CANONICAL]


def push_new_head(db, change=None):
    """The implementer pushes: a new head commit on the pull request, with no checks and no reviews
    on it yet."""
    r, h, _ = ids(db)
    files = dict(r["commits"][h]["files"])
    files.update(change or {"items/kisa/U-54.yaml": "id: U-54\nbaseline_status: confirmed\n# v2\n"})
    new = "h2" + sha(json.dumps(files, sort_keys=True))[:38]
    r["commits"][new] = {"parents": [h], "files": files, "author": "impl-bot"}
    r["pulls"]["7"]["head_sha"] = new
    return new


def move_canonical(db, change=None):
    """Another change lands on canonical — for a shared artifact, the head is no longer on top of it."""
    r, _, c = ids(db)
    files = dict(r["commits"][c]["files"])
    files.update(change or {"items/kisa/U-02.yaml": "id: U-02\n"})
    new = "c1" + sha(json.dumps(files, sort_keys=True))[:38]
    r["commits"][new] = {"parents": [c], "files": files, "author": "astro3141"}
    r["branches"][CANONICAL] = new
    return new


def write_all():
    out = os.path.join(HERE, "remotes")
    os.makedirs(out, exist_ok=True)
    json.dump(scanner_pass(), open(os.path.join(out, "scanner-pass.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"wrote {out}/scanner-pass.json (policy digest {policy_digest()})")


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(PKG, "lib"))
    a = sys.argv[1:]
    if a[:1] == ["write"]:
        write_all()
    elif a[:1] in (["merge"], ["push"]) and len(a) == 3:
        import remote
        path = os.path.realpath(a[1])
        db = json.load(open(path, encoding="utf-8"))
        if a[0] == "push":
            new = push_new_head(db)
            json.dump(db, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print(json.dumps({"pushed": new}))
        else:
            rem = remote.FixtureRemote(path, REPO)
            pr = rem.pull(a[2])
            print(json.dumps({"merged": rem.merge(a[2], pr["head_sha"], "merge"), "head": pr["head_sha"]}))
    else:
        print(__doc__)
        sys.exit(2)
