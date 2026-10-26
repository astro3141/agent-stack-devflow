#!/usr/bin/env python3
"""Verify a devflow Foundation candidate on the operator's machine, and record the result for devflow.

usage: verify_candidate.py --project <name> --task <task id> --checkout <path to your clone>
                           [--state-root <agent-stack state root>] [--only <check>[,<check>]]

What it does (RUNBOOK §Foundation candidates):
  1. reads the candidate devflow built:  <state_root>/devflow/candidates/<owner__repo>/<task>/current.json
  2. fetches its bundle into your checkout (`git fetch <bundle> <sha>`) and refuses unless the commit
     it received has exactly the candidate's sha — what you verify is what was judged;
  3. checks out that sha in a temporary worktree and runs the project's local checks: the policy's
     `flow_defs.<flow>.local_checks` (name → command), read from that worktree's own policy file;
  4. writes verification/<sha>.json beside the candidate — the only source of check results devflow
     accepts for a candidate (lib/candidate.py) — with one log file per check.

It never pushes, never touches the remote, and never changes your checkout's branches: the fetched
commit is reachable only through the temporary worktree, which is removed at the end.
"""
import argparse, json, os, platform, shutil, subprocess, sys, time, getpass


def sh(args, cwd=None, timeout=None, log=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"$ {' '.join(args) if isinstance(args, list) else args}\n{r.stdout}{r.stderr}\n(exit {r.returncode})\n")
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--checkout", required=True, help="your clone of the project (the base commit must be in it)")
    ap.add_argument("--state-root", default=os.path.expanduser("~/agent-stack-test/state"))
    ap.add_argument("--only", default="", help="comma-separated check names to run (default: all local_checks)")
    a = ap.parse_args()

    cand_root = os.path.join(a.state_root, "devflow", "candidates")
    dirs = [d for d in os.listdir(cand_root) if os.path.isdir(os.path.join(cand_root, d, a.task))] if os.path.isdir(cand_root) else []
    if len(dirs) != 1:
        sys.exit(f"candidate directory for task {a.task!r} not found (or ambiguous) under {cand_root}: {dirs}")
    cdir = os.path.join(cand_root, dirs[0], a.task)
    cur = json.load(open(os.path.join(cdir, "current.json"), encoding="utf-8"))
    sha, bundle = cur["sha"], cur["bundle"]
    if not os.path.isabs(bundle) or not os.path.isfile(bundle):
        # the bundle path was written inside the container (/work/...); map it to this machine
        bundle = os.path.join(cdir, os.path.basename(bundle))
    if not os.path.isfile(bundle):
        sys.exit(f"bundle not found: {bundle}")
    print(f"candidate {sha} (round {cur.get('round')}, base {str(cur.get('base'))[:12]}) — bundle {os.path.basename(bundle)}")

    co = os.path.abspath(a.checkout)
    r = sh(["git", "-C", co, "bundle", "verify", bundle])
    if r.returncode != 0:
        sys.exit(f"the bundle does not apply to this checkout (is the base {str(cur.get('base'))[:12]} fetched?):\n{r.stderr}")
    r = sh(["git", "-C", co, "fetch", "-q", bundle, sha])
    if r.returncode != 0:
        sys.exit(f"git fetch from the bundle failed:\n{r.stderr}")
    r = sh(["git", "-C", co, "cat-file", "-t", sha])
    if r.stdout.strip() != "commit":
        sys.exit(f"the fetched object is not the candidate commit {sha}: {r.stdout}{r.stderr}")
    print(f"fetched: commit {sha} is now in {co} (reachable only through the worktree below)")

    wt = os.path.join(cdir, "verify-worktree")
    sh(["git", "-C", co, "worktree", "remove", "--force", wt])
    shutil.rmtree(wt, ignore_errors=True)
    r = sh(["git", "-C", co, "worktree", "add", "--detach", wt, sha])
    if r.returncode != 0:
        sys.exit(f"worktree failed:\n{r.stderr}")
    try:
        head = sh(["git", "-C", wt, "rev-parse", "HEAD"]).stdout.strip()
        if head != sha:
            sys.exit(f"worktree is at {head}, not at the candidate {sha}")
        import yaml
        reg_path = None
        for cand in (os.path.join(a.state_root, "..", "config", "devflow", "projects", f"{a.project}.yaml"),):
            if os.path.isfile(cand):
                reg_path = cand
        policy_path = ".devflow/project.yaml"
        if reg_path:
            policy_path = (yaml.safe_load(open(reg_path, encoding="utf-8")) or {}).get("policy_path") or policy_path
        pol = yaml.safe_load(open(os.path.join(wt, policy_path), encoding="utf-8")) or {}
        flows = pol.get("flows") or {}
        task_class = None
        # the task's class is in the issue; every transaction flow's local_checks are the same set here
        checks = {}
        for fname, fdef in (pol.get("flow_defs") or {}).items():
            if (fdef or {}).get("candidate") == "local":
                checks.update((fdef.get("local_checks") or {}))
        if not checks:
            sys.exit("the policy names no flow_defs.<flow>.local_checks for a local candidate")
        only = [c for c in a.only.split(",") if c] if a.only else list(checks)
        vdir = os.path.join(cdir, "verification")
        os.makedirs(vdir, exist_ok=True)
        results = {}
        for name in only:
            cmd = checks.get(name)
            if not cmd:
                results[name] = {"result": "NOT_RUN", "why": "no local command for this check"}
                continue
            log = os.path.join(vdir, f"{sha[:12]}-{name}.log")
            open(log, "w").close()
            t0 = time.time()
            print(f"== {name}: running ({cmd if isinstance(cmd, str) else ' '.join(cmd)})", flush=True)
            try:
                r = sh(cmd if isinstance(cmd, list) else ["sh", "-c", cmd], cwd=wt, timeout=4 * 3600, log=log)
                res = "PASS" if r.returncode == 0 else "FAIL"
                why = f"exit {r.returncode} in {int(time.time() - t0)} s" + ("" if r.returncode == 0 else f": {(r.stderr or r.stdout)[-300:]}")
            except subprocess.TimeoutExpired:
                res, why = "NOT_RUN", "timed out after 4 h"
            results[name] = {"result": res, "why": why, "log": log}
            print(f"   {res} — {why[:160]}")
        doc = {"sha": sha, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "by": getpass.getuser(),
               "host": platform.node(), "checkout": co, "worktree_head": head, "checks": results}
        out = os.path.join(vdir, f"{sha}.json")
        json.dump(doc, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\nwritten: {out}")
        print("results: " + ", ".join(f"{k}={v['result']}" for k, v in results.items()))
        print("next: start a devflow pass for the task; its reviews read these results as the candidate's checks.")
        # the worktree is kept only when something failed, so the operator can look
        if all(v["result"] == "PASS" for v in results.values()):
            sh(["git", "-C", co, "worktree", "remove", "--force", wt])
        else:
            print(f"(worktree kept for inspection: {wt}; remove with `git -C {co} worktree remove --force {wt}`)")
    except KeyboardInterrupt:
        sh(["git", "-C", co, "worktree", "remove", "--force", wt])
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
