"""Local candidates for a change class that is published as a transaction.

The infra scanner publishes a Foundation as a commit pair A+B by a person; A alone is never pushed
and never exists as a pull request (OPERATING_POLICY "Foundation Rebaseline Transaction"). Until
2026-10-26 that left devflow with nothing to implement on: gate1 was approved and the run stopped
("the next move is the human operator's"). This module gives such a task a *candidate*: a real
git commit A that lives only here —

    <state_root>/devflow/candidates/<owner__repo>/<task id>/
        repo/                 a git repository: the base commit fetched by sha, the candidates
        current.json          the candidate the task stands at (sha, parent, base, round, run, bundle)
        <sha>.bundle          what the operator fetches into their checkout (base..sha)
        verification/<sha>.json   the operator's local verification of exactly that sha

— so the implementer works on a tree, the reviewer reads a frozen diff, gate2 binds to the exact
sha, and the person who publishes fetches the very commit that was judged. Nothing in this module
talks to the remote except one read: fetching the base commit's objects, by sha, with the
credential devflow's API calls already use. Nothing is ever pushed.

Three guarantees the steps rely on (RUNBOOK §Foundation candidates):
  1. no remote publication — there is no function here that writes to the remote;
  2. commit identity — reviews, checks and gate2 name the sha `commit()` returned, and
     `bundle()` carries that commit, so what the operator fetches is what was judged;
  3. verification before gate2 — `verification()` is the only source of check results for a
     candidate, and it is written by a person's run of tools/verify_candidate.py, never here.
"""
import json, os, shutil, subprocess, tempfile, time

import dfcore


def dir_for(reg, task):
    return dfcore.state_dir("candidates", str(reg["repository"]).replace("/", "__"), str(task.get("id")))


def load(cdir):
    try:
        return json.load(open(os.path.join(cdir, "current.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(cdir, cur):
    os.makedirs(cdir, exist_ok=True)
    tmp = os.path.join(cdir, "current.json.tmp")
    json.dump(cur, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(cdir, "current.json"))


def _git(repo, *args, env=None, timeout=600, check=True, input_bytes=None):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, env=env, timeout=timeout, input=input_bytes)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {r.stderr.decode(errors='replace')[-400:]}")
    return r


def ensure_repo(cdir, rem, base_sha, proxy_env=None):
    """The candidate repository, with the base commit's objects present. The base is fetched by
    sha from the remote with the API's own token (GitHub serves any reachable commit by sha);
    a fixture remote has no git objects, so its base is written as a local commit instead and
    `base_git` records which sha stands for it here."""
    repo = os.path.join(cdir, "repo")
    if not os.path.isdir(os.path.join(repo, ".git")):
        os.makedirs(repo, exist_ok=True)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.name", dfcore.IMPLEMENTER)
        _git(repo, "config", "user.email", dfcore.IMPLEMENTER_EMAIL)
    if _git(repo, "cat-file", "-e", f"{base_sha}^{{commit}}", check=False).returncode == 0:
        return repo, base_sha
    if getattr(rem, "kind", "") == "fixture":
        # no objects to fetch: the fixture's tree becomes a local commit standing for the base
        with tempfile.TemporaryDirectory() as t:
            rem.materialize(base_sha, t)
            env = {**os.environ, "GIT_INDEX_FILE": os.path.join(repo, ".git", f"index-base-{base_sha[:12]}")}
            _git(repo, "--work-tree", t, "add", "-A", "-f", ".", env=env)
            tree = _git(repo, "write-tree", env=env).stdout.decode().strip()
            sha = _git(repo, "commit-tree", tree, "-m", f"base {base_sha} (fixture)", env=_ident()).stdout.decode().strip()
        _git(repo, "update-ref", f"refs/candidates/base-{base_sha[:12]}", sha)
        return repo, sha
    tok = getattr(rem, "token", None)
    if not tok:
        raise RuntimeError("no credential to fetch the base commit with")
    url = f"https://x-access-token:{tok}@github.com/{rem.repo}.git"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for k in ("HTTPS_PROXY", "HTTP_PROXY"):
        if (proxy_env or {}).get(k):
            env[k] = proxy_env[k]
            env[k.lower()] = proxy_env[k]
    r = _git(repo, "fetch", "-q", "--depth=1", url, base_sha, env=env, timeout=900, check=False)
    if r.returncode != 0:
        raise RuntimeError("fetching the base commit failed: " + r.stderr.decode(errors="replace").replace(tok, "<token>")[-300:])
    _git(repo, "update-ref", f"refs/candidates/base-{base_sha[:12]}", base_sha)
    return repo, base_sha


def _ident(author=None):
    a = author or {}
    name, email = a.get("name") or dfcore.IMPLEMENTER, a.get("email") or dfcore.IMPLEMENTER_EMAIL
    date = a.get("date") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": date}


def commit(repo, parent, changes, message, author, extra_parents=()):
    """A commit on `parent` with `changes` applied: {path: (bytes, mode) | None (deleted)}. The tree
    is built from the parent's with git plumbing, in an index of its own; nothing is checked out."""
    env = {**os.environ, "GIT_INDEX_FILE": os.path.join(repo, ".git", f"index-{parent[:12]}-{int(time.time()*1000)}")}
    try:
        _git(repo, "read-tree", parent, env=env)
        for path, c in sorted(changes.items()):
            if c is None:
                _git(repo, "update-index", "--force-remove", "--", path, env=env)
                continue
            data, mode = c
            blob = _git(repo, "hash-object", "-w", "--stdin", input_bytes=data).stdout.decode().strip()
            _git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
        tree = _git(repo, "write-tree", env=env).stdout.decode().strip()
    finally:
        try:
            os.remove(env["GIT_INDEX_FILE"])
        except OSError:
            pass
    args = ["commit-tree", tree, "-p", parent]
    for p in extra_parents or ():
        args += ["-p", p]
    sha = _git(repo, *args, "-m", message, env=_ident(author)).stdout.decode().strip()
    _git(repo, "update-ref", f"refs/candidates/{sha}", sha)
    return sha


def bundle(repo, base_git, sha, dest):
    """The candidate's commits above the base, as one file the operator fetches: `git fetch <file> <sha>`.
    A bundle records refs, not bare shas, so the candidate's own ref is what it carries."""
    _git(repo, "bundle", "create", dest, f"^{base_git}", f"refs/candidates/{sha}")
    return dest


def file_at(repo, sha, path):
    r = _git(repo, "show", f"{sha}:{path}", check=False)
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def changed_files(repo, base_git, sha):
    r = _git(repo, "diff", "--name-only", "--no-renames", f"{base_git}..{sha}")
    files = sorted(f for f in r.stdout.decode().split("\n") if f)
    commits = _git(repo, "rev-list", f"{base_git}..{sha}").stdout.decode().split()
    return {"files": files, "authors": [dfcore.IMPLEMENTER_EMAIL], "commits": commits, "merge_base": base_git}


def contains(repo, ancestor, descendant):
    if _git(repo, "cat-file", "-e", f"{ancestor}^{{commit}}", check=False).returncode != 0:
        return False
    return _git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


def materialize(repo, sha, dest, only=None):
    """The tree of `sha` under dest (the shape refs.cached_tree expects of a remote)."""
    os.makedirs(dest, exist_ok=True)
    args = ["archive", "--format=tar", sha] + ([only] if only else [])
    tar = _git(repo, *args).stdout
    import io, tarfile
    written = []
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as tf:
        for m in tf.getmembers():
            if not m.isfile() or ".." in m.name.split("/"):
                continue
            out = os.path.join(dest, m.name)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(tf.extractfile(m).read())
            if m.mode & 0o111:
                os.chmod(out, 0o755)
            written.append(m.name)
    return written


def verification(cdir, sha):
    """The operator's local verification of exactly `sha`, as check runs (dfcore.evaluate_checks):
    verification/<sha>.json = {sha, at, by, host, checks: {name: {result: PASS|FAIL|NOT_RUN, why, log}}}.
    A file for another sha, or none, is no run at all: the checks stay NOT_RUN (pending)."""
    p = os.path.join(cdir, "verification", f"{sha}.json")
    try:
        v = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if str(v.get("sha")) != sha:
        return []
    conclusion = {"PASS": "success", "FAIL": "failure", "NOT_RUN": "skipped"}
    return [{"name": name, "status": "completed", "conclusion": conclusion.get(str(c.get("result")), "skipped"),
             "head_sha": sha, "id": f"local:{name}", "completed_at": v.get("at"), "kind": "local",
             "title": f"local verification by {v.get('by', '?')} on {v.get('host', '?')}",
             "summary": str(c.get("why") or "")[:2000], "url": str(c.get("log") or "")}
            for name, c in (v.get("checks") or {}).items()]


def synthetic_pr(cur, task):
    """What the steps see as 'the pull request' while a candidate stands: the candidate's sha as
    the head, the canonical head it was built on as the base, devflow's branch name as the ref —
    and `candidate: True`, which is what every remote-writing step checks before it writes."""
    return {"number": None, "state": "open", "merged": False, "head_sha": cur["sha"], "base_sha": cur["base"],
            "head_ref": dfcore.task_branch(task), "base_ref": None, "author": dfcore.IMPLEMENTER,
            "flow": "transaction", "candidate": True, "bundle": cur.get("bundle"), "round": cur.get("round")}


class CandidateRemote:
    """The remote with a candidate in front of it. Reads about the candidate's sha — its files,
    its diff against the base, its tree, its checks, its (absent) pull request — are answered from
    the candidate repository and the operator's verification; every other call goes to the real
    remote unchanged. There is no write about a candidate: the steps that write (deliver, publish,
    integrate) check `pr["candidate"]` and do not call the remote for it."""

    def __init__(self, rem, cdir, cur):
        self.rem, self.cdir, self.cur = rem, cdir, cur
        self.repo_dir = os.path.join(cdir, "repo")
        self.sha, self.base, self.base_git = cur["sha"], cur["base"], cur.get("base_git") or cur["base"]
        self.repo, self.kind, self.token = rem.repo, rem.kind, getattr(rem, "token", None)

    def __getattr__(self, name):
        return getattr(self.rem, name)

    def _mine(self, sha):
        return str(sha or "") == self.sha

    def file_at(self, ref, path):
        if self._mine(ref):
            return file_at(self.repo_dir, self.sha, path)
        if str(ref) == self.base and self.base_git != self.base:
            return file_at(self.repo_dir, self.base_git, path)
        return self.rem.file_at(ref, path)

    def changed_files(self, base, head):
        if self._mine(head):
            b = self.base_git if str(base) == self.base else base
            return changed_files(self.repo_dir, b, self.sha)
        return self.rem.changed_files(base, head)

    def contains(self, ancestor, descendant):
        if self._mine(descendant):
            a = self.base_git if str(ancestor) == self.base else ancestor
            return contains(self.repo_dir, a, self.sha)
        if self._mine(ancestor):
            return False
        return self.rem.contains(ancestor, descendant)

    def materialize(self, sha, dest, only=None):
        if self._mine(sha):
            return materialize(self.repo_dir, self.sha, dest, only=only)
        return self.rem.materialize(sha, dest, only=only)

    def check_runs(self, sha):
        if self._mine(sha):
            return verification(self.cdir, self.sha)
        return self.rem.check_runs(sha)

    def statuses(self, sha):
        return [] if self._mine(sha) else self.rem.statuses(sha)

    def pull(self, number):
        if number is None:
            return synthetic_pr(self.cur, {"id": self.cur.get("task")})
        return self.rem.pull(number)

    def reviews(self, number):
        return [] if number is None else self.rem.reviews(number)


def wrap(rem, reg, task, ev=None):
    """The remote as the steps should see it for this task: wrapped when a candidate stands and the
    evaluation says so (ev["candidate"]), else as it is."""
    if ev is not None and not ev.get("candidate"):
        return rem
    cdir = dir_for(reg, task)
    cur = load(cdir)
    if not cur or (ev is not None and str(ev["candidate"].get("sha")) != str(cur.get("sha"))):
        return rem
    return CandidateRemote(rem, cdir, cur)
