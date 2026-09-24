"""The remote a development task lives on: GitHub, or a fixture that behaves like it.

Not a step — a library the package's steps import from lib/ (steps/ holds only steps, each with
its REPEATABLE), so it declares none of its own.

Every question the workflow asks about a task is asked here, and the answer is the remote's, not
a file this runtime wrote earlier: which commit a pull request's head is at, what the checks on
exactly that commit concluded, which reviews were written against which commit, whether a merge
already happened. That is what "the state of a task is GitHub's" means in code.

Two implementations with one surface:

  GitHubRemote   the REST API, with a token from the package's own env file
                 (docker/package.env, DEVFLOW_GITHUB_TOKEN) through the egress proxy — which reaches
                 api.github.com only if the operator added it to docker/egress/allow. Without
                 either, it refuses with a hint that names the file (docs/packages.md).
  FixtureRemote  one JSON file. Used by the controls and by fixture runs through the real runner,
                 and able to inject the faults a real remote has: a request that took effect but
                 whose answer was lost, an API that is down.

What the workflow calls "a tool failure" is a RemoteError. It never becomes a verdict.
"""
import copy, fnmatch, json, os, time, urllib.error, urllib.parse, urllib.request


class RemoteError(Exception):
    """The remote could not answer. A tool failure — never a judgement about the work."""


class Transient(RemoteError):
    """Network or server trouble; a read may be tried again."""


class Unavailable(RemoteError):
    """The feature does not exist for this repository (e.g. rules on a free private repo)."""


class Conflict(RemoteError):
    """The remote refused because the state is not what the request assumed (409/422 on merge,
    an existing ref). The caller asks again what the state is; it does not treat this as done."""


# ----------------------------------------------------------------------------------- GitHub
class GitHubRemote:
    API = "https://api.github.com"

    def __init__(self, repo, token=None, proxy_env=None):
        self.repo = repo
        self.token = token if token is not None else os.environ.get("DEVFLOW_GITHUB_TOKEN", "")
        if not self.token:
            raise RemoteError("no GitHub token: put DEVFLOW_GITHUB_TOKEN=<token> in docker/package.env "
                              "and add ^api\\.github\\.com$ to docker/egress/allow (docs/packages.md)")
        handlers = []
        if proxy_env:
            handlers.append(urllib.request.ProxyHandler(
                {"https": proxy_env.get("HTTPS_PROXY"), "http": proxy_env.get("HTTP_PROXY")}))
        self._open = urllib.request.build_opener(*handlers).open
        self.kind = "github"

    def _call(self, method, path, body=None, ok404=False):
        """One API call. A read that fails transiently (network, 5xx) is tried up to twice more
        (개발 범용 워크플로 §8); a write never is — whether it took effect is asked of the remote by
        the step that made it, which is the only place that knows what 'the same effect' is."""
        attempts = 3 if method == "GET" else 1
        for i in range(attempts):
            try:
                return self._call_once(method, path, body, ok404)
            except Transient as e:
                if i == attempts - 1:
                    raise RemoteError(str(e))
                time.sleep(2 * (i + 1))

    def _call_once(self, method, path, body=None, ok404=False):
        url = path if path.startswith("http") else f"{self.API}/repos/{self.repo}{path}"
        req = urllib.request.Request(url, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28",
                                              "User-Agent": "agent-stack-devflow"})
        try:
            with self._open(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 404 and ok404:
                return None
            detail = e.read()[:300].decode(errors="replace")
            if e.code in (409, 422):
                raise Conflict(f"{method} {path}: {e.code} {detail}")
            if e.code == 403 and "Upgrade to GitHub Pro" in detail:
                raise Unavailable(f"{method} {path}: not available on this plan")
            if e.code >= 500:
                raise Transient(f"{method} {path}: HTTP {e.code} {detail}")
            raise RemoteError(f"{method} {path}: HTTP {e.code} {detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Transient(f"{method} {path}: {type(e).__name__}: {e}")

    def _pages(self, path, key=None):
        out, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            got = self._call("GET", f"{path}{sep}per_page=100&page={page}")
            rows = got.get(key, []) if key else got
            out += rows
            if len(rows) < 100:
                return out
            page += 1

    # --- reads
    def repo_info(self):
        r = self._call("GET", "")
        return {"default_branch": r["default_branch"], "allow_auto_merge": r.get("allow_auto_merge"),
                "private": r.get("private")}

    def branch_head(self, branch):
        return self._call("GET", f"/branches/{urllib.parse.quote(branch, safe='')}")["commit"]["sha"]

    def pull(self, number):
        p = self._call("GET", f"/pulls/{int(number)}")
        return {"number": p["number"], "state": p["state"], "merged": bool(p.get("merged")),
                "merge_commit_sha": p.get("merge_commit_sha") if p.get("merged") else None,
                "head_sha": p["head"]["sha"], "head_ref": p["head"]["ref"],
                "base_ref": p["base"]["ref"], "base_sha": p["base"]["sha"],
                "author": (p.get("user") or {}).get("login", ""), "body": p.get("body") or "",
                "mergeable_state": p.get("mergeable_state")}

    def issue(self, number):
        i = self._call("GET", f"/issues/{int(number)}")
        return {"number": i["number"], "state": i["state"], "body": i.get("body") or "",
                "title": i.get("title") or "", "labels": [l["name"] for l in i.get("labels", [])]}

    def changed_files(self, base, head):
        c = self._call("GET", f"/compare/{base}...{head}")
        # A rename has two paths, and a guard that sees only the new one lets a protected file
        # leave its protection by being renamed (CADP lens library: lossy observation). Both count.
        files = set()
        for f in c.get("files", []):
            files.add(f["filename"])
            if f.get("previous_filename"):
                files.add(f["previous_filename"])
        if len(c.get("files", [])) >= 300:
            raise RemoteError("compare returned 300 files, GitHub's limit: the change list may be incomplete")
        return {"files": sorted(files),
                "authors": sorted({(x.get("author") or {}).get("login") or
                                   (x.get("commit", {}).get("author") or {}).get("email", "")
                                   for x in c.get("commits", [])}),
                "merge_base": (c.get("merge_base_commit") or {}).get("sha", "")}

    def contains(self, ancestor, descendant):
        """Whether `descendant` has `ancestor` in its history (the change is on top of it)."""
        c = self._call("GET", f"/compare/{ancestor}...{descendant}")
        return c.get("status") in ("ahead", "identical")

    def file_at(self, ref, path):
        got = self._call("GET", f"/contents/{urllib.parse.quote(path)}?ref={ref}", ok404=True)
        if got is None or got.get("type") != "file":
            return None
        import base64
        return base64.b64decode(got["content"]).decode("utf-8")

    def blob_id(self, ref, path):
        got = self._call("GET", f"/contents/{urllib.parse.quote(path)}?ref={ref}", ok404=True)
        return (got or {}).get("sha")

    def check_runs(self, sha):
        rows = self._pages(f"/commits/{sha}/check-runs", key="check_runs")
        runs = [{"name": r["name"], "status": r["status"], "conclusion": r.get("conclusion"),
                 "head_sha": r.get("head_sha", sha), "id": r["id"],
                 "completed_at": r.get("completed_at")} for r in rows]
        for s in self._pages(f"/commits/{sha}/statuses"):
            runs.append({"name": s["context"], "status": "completed" if s["state"] != "pending" else "in_progress",
                         "conclusion": {"success": "success", "failure": "failure", "error": "startup_failure"}
                         .get(s["state"]), "head_sha": sha, "id": s["id"], "completed_at": s.get("updated_at"),
                         "kind": "status"})
        return runs

    def reviews(self, number):
        return [{"id": r["id"], "user": (r.get("user") or {}).get("login", ""), "commit_id": r.get("commit_id"),
                 "state": r.get("state"), "body": r.get("body") or ""}
                for r in self._pages(f"/pulls/{int(number)}/reviews")]

    def comments(self, number):
        return [{"id": c["id"], "body": c.get("body") or "", "user": (c.get("user") or {}).get("login", "")}
                for c in self._pages(f"/issues/{int(number)}/comments")]

    def statuses(self, sha):
        return [{"context": s["context"], "state": s["state"], "description": s.get("description") or ""}
                for s in self._pages(f"/commits/{sha}/statuses")]

    def ref(self, name):
        got = self._call("GET", f"/git/ref/{name.removeprefix('refs/')}", ok404=True)
        return (got or {}).get("object", {}).get("sha")

    def merge_queue(self, branch):
        """Whether a merge queue governs this branch — asked of the rules, not assumed. A plan
        without rules has no merge queue."""
        try:
            rules = self._call("GET", f"/rules/branches/{urllib.parse.quote(branch, safe='')}", ok404=True) or []
        except Unavailable:
            return False
        return any(r.get("type") == "merge_queue" for r in rules)

    def protection(self, branch):
        """What GitHub enforces on this branch. On a plan without the feature the answer is
        "nothing", said as such — never read as a tool failure, never assumed as protection."""
        b = urllib.parse.quote(branch, safe="")
        out = {"available": True, "rules": [], "classic": False}
        try:
            out["rules"] = [r.get("type") for r in (self._call("GET", f"/rules/branches/{b}", ok404=True) or [])]
            out["classic"] = bool(self._call("GET", f"/branches/{b}/protection", ok404=True))
        except Unavailable as e:
            out.update(available=False, why=str(e))
        return out

    # --- writes (every caller guards them: see guarded() in dfcore)
    def set_status(self, sha, context, state, description, target_url=""):
        self._call("POST", f"/statuses/{sha}", {"state": state, "context": context,
                                                "description": description[:140],
                                                **({"target_url": target_url} if target_url else {})})

    def add_comment(self, number, body):
        return self._call("POST", f"/issues/{int(number)}/comments", {"body": body})["id"]

    def edit_comment(self, comment_id, body):
        self._call("PATCH", f"/issues/comments/{int(comment_id)}", {"body": body})

    def set_labels(self, number, add=(), remove=()):
        for l in remove:
            try:
                self._call("DELETE", f"/issues/{int(number)}/labels/{urllib.parse.quote(l)}", ok404=True)
            except RemoteError:
                raise
        if add:
            self._call("POST", f"/issues/{int(number)}/labels", {"labels": list(add)})

    def create_ref(self, name, sha):
        self._call("POST", "/git/refs", {"ref": name, "sha": sha})

    def delete_ref(self, name):
        self._call("DELETE", f"/git/refs/{name.removeprefix('refs/')}", ok404=True)

    def merge(self, number, sha, method):
        r = self._call("PUT", f"/pulls/{int(number)}/merge", {"sha": sha, "merge_method": method})
        return r.get("sha")


# ----------------------------------------------------------------------------------- fixture
class FixtureRemote:
    """A remote in one JSON file. Every write is saved before the call returns, so a fault
    injected *after* the write is exactly "it happened, and the answer was lost".

    faults: {"lose_response": ["merge", "add_comment", ...]}  — each listed operation takes effect,
            then raises, once; {"down": true} — every call raises (the API is unreachable).
    """

    def __init__(self, path, repo):
        # the real file: a shared fixture is reached through a link, and replacing the link would
        # fork the remote instead of changing it
        self.path, self.repo, self.kind = os.path.realpath(path), repo, "fixture"
        self._load()
        if repo not in self.db.get("repos", {}):
            raise RemoteError(f"fixture has no repository {repo!r}")

    def _load(self):
        with open(self.path, encoding="utf-8") as f:
            self.db = json.load(f)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.db, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    @property
    def r(self):
        return self.db["repos"][self.repo]

    def _up(self):
        self._load()
        if (self.db.get("faults") or {}).get("down"):
            raise RemoteError("fixture: the remote is down")

    def _lose(self, op):
        lose = (self.db.get("faults") or {}).get("lose_response") or []
        if op in lose:
            lose.remove(op)
            self._save()
            raise RemoteError(f"fixture: {op} took effect, and the response was lost")

    def _files(self, sha):
        c = self.r["commits"].get(sha)
        if c is None:
            raise RemoteError(f"fixture: no commit {sha}")
        return c["files"]

    # --- reads
    def repo_info(self):
        self._up()
        return {"default_branch": self.r["default_branch"], "allow_auto_merge": False, "private": True}

    def branch_head(self, branch):
        self._up()
        if branch not in self.r["branches"]:
            raise RemoteError(f"fixture: no branch {branch}")
        return self.r["branches"][branch]

    def pull(self, number):
        self._up()
        p = self.r["pulls"].get(str(number))
        if p is None:
            raise RemoteError(f"fixture: no pull request #{number}")
        out = copy.deepcopy(p)
        out.setdefault("base_sha", self.r["branches"].get(p["base_ref"]))
        return out

    def issue(self, number):
        self._up()
        i = self.r["issues"].get(str(number))
        if i is None:
            raise RemoteError(f"fixture: no issue #{number}")
        return {"number": int(number), "state": i.get("state", "open"), "body": i.get("body", ""),
                "title": i.get("title", ""), "labels": list(i.get("labels", []))}

    def changed_files(self, base, head):
        self._up()
        a, b = self._files(base), self._files(head)
        files = sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))
        # authors of the commits between: walk parents from head until base
        authors, seen, todo = set(), set(), [head]
        while todo:
            s = todo.pop()
            if s in seen or s == base:
                continue
            seen.add(s)
            c = self.r["commits"][s]
            authors.add(c.get("author", ""))
            todo += c.get("parents", [])
        return {"files": files, "authors": sorted(authors), "merge_base": base}

    def contains(self, ancestor, descendant):
        self._up()
        seen, todo = set(), [descendant]
        while todo:
            s = todo.pop()
            if s == ancestor:
                return True
            if s in seen or s not in self.r["commits"]:
                continue
            seen.add(s)
            todo += self.r["commits"][s].get("parents", [])
        return False

    def file_at(self, ref, path):
        self._up()
        sha = self.r["branches"].get(ref, ref)
        return self._files(sha).get(path)

    def blob_id(self, ref, path):
        import hashlib
        text = self.file_at(ref, path)
        return None if text is None else hashlib.sha1(text.encode()).hexdigest()

    def check_runs(self, sha):
        self._up()
        return [dict(r, head_sha=sha) for r in self.r.get("check_runs", {}).get(sha, [])]

    def reviews(self, number):
        self._up()
        return copy.deepcopy(self.r.get("reviews", {}).get(str(number), []))

    def comments(self, number):
        self._up()
        return copy.deepcopy((self.r["issues"].get(str(number)) or
                              self.r["pulls"].get(str(number)) or {}).get("comments", []))

    def statuses(self, sha):
        self._up()
        return copy.deepcopy(self.r.get("statuses", {}).get(sha, []))

    def ref(self, name):
        self._up()
        return self.r.get("refs", {}).get(name)

    def merge_queue(self, branch):
        self._up()
        return bool((self.r.get("features") or {}).get("merge_queue"))

    def protection(self, branch):
        self._up()
        return copy.deepcopy((self.r.get("features") or {}).get("protection", {"rules": [], "classic": False}))

    # --- writes
    def _thread(self, number):
        return self.r["issues"].get(str(number)) or self.r["pulls"].get(str(number))

    def set_status(self, sha, context, state, description, target_url=""):
        self._up()
        rows = self.r.setdefault("statuses", {}).setdefault(sha, [])
        rows[:] = [s for s in rows if s["context"] != context]
        rows.append({"context": context, "state": state, "description": description[:140]})
        self._save()
        self._lose("set_status")

    def add_comment(self, number, body):
        self._up()
        t = self._thread(number)
        cid = int(time.time() * 1000) % 10**9 + len(t.setdefault("comments", []))
        t["comments"].append({"id": cid, "body": body, "user": "devflow"})
        self._save()
        self._lose("add_comment")
        return cid

    def edit_comment(self, comment_id, body):
        self._up()
        for t in list(self.r["issues"].values()) + list(self.r["pulls"].values()):
            for c in t.get("comments", []):
                if c["id"] == comment_id:
                    c["body"] = body
                    self._save()
                    self._lose("edit_comment")
                    return
        raise RemoteError(f"fixture: no comment {comment_id}")

    def set_labels(self, number, add=(), remove=()):
        self._up()
        t = self._thread(number)
        labels = [l for l in t.setdefault("labels", []) if l not in remove]
        labels += [l for l in add if l not in labels]
        t["labels"] = labels
        self._save()
        self._lose("set_labels")

    def create_ref(self, name, sha):
        self._up()
        refs = self.r.setdefault("refs", {})
        if name in refs:
            raise Conflict(f"fixture: {name} exists")
        refs[name] = sha
        self._save()
        self._lose("create_ref")

    def delete_ref(self, name):
        self._up()
        self.r.setdefault("refs", {}).pop(name, None)
        self._save()

    def merge(self, number, sha, method):
        self._up()
        p = self.r["pulls"][str(number)]
        if p.get("merged"):
            raise Conflict("fixture: already merged")
        if p["head_sha"] != sha:
            raise Conflict("fixture: head moved")
        base = p["base_ref"]
        new = f"m{len(self.r['commits']):03d}{sha[:6]}"
        files = dict(self._files(self.r["branches"][base]))
        files.update(self._files(sha))
        for path in [x for x in self._files(p["base_sha"]) if x not in self._files(sha)]:
            files.pop(path, None)
        self.r["commits"][new] = {"parents": [self.r["branches"][base], sha], "files": files,
                                  "author": "merge"}
        self.r["branches"][base] = new
        p.update(merged=True, state="closed", merge_commit_sha=new)
        for run in (self.r.get("post_merge_check_runs") or []):
            self.r.setdefault("check_runs", {}).setdefault(new, []).append(dict(run))
        self._save()
        self._lose("merge")
        return new


def open_remote(kind, repo, fixture_path=None, proxy_env=None):
    if kind == "fixture":
        if not fixture_path or not os.path.isfile(fixture_path):
            raise RemoteError(f"fixture remote file is not there: {fixture_path!r}")
        return FixtureRemote(fixture_path, repo)
    if kind == "github":
        return GitHubRemote(repo, proxy_env=proxy_env)
    raise RemoteError(f"unknown remote kind {kind!r} (github | fixture)")


def glob_any(path, patterns):
    return any(fnmatch.fnmatch(path, p) for p in patterns or [])
