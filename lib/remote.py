"""The remote a development task lives on: GitHub, or a fixture that behaves like it.

Not a step — a library the package's steps import from lib/ (steps/ holds only steps, each with
its REPEATABLE), so it declares none of its own.

Every question the workflow asks about a task is asked here, and the answer is the remote's, not
a file this runtime wrote earlier: which commit a pull request's head is at, what the checks on
exactly that commit concluded, which reviews were written against which commit, whether a merge
already happened. That is what "the state of a task is GitHub's" means in code.

Two implementations with one surface:

  GitHubRemote   the REST API, with the token lib/ghauth.py finds (the panel's GitHub login, else
                 DEVFLOW_GITHUB_TOKEN in docker/package.env), through the egress proxy — which
                 reaches api.github.com only if the operator added it to docker/egress/allow.local.
                 Without a credential it refuses with a hint that names both ways.
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
        self.via_app = False
        if token is None:
            import ghauth
            try:
                token = ghauth.token(proxy_env)
                self.via_app = ghauth.through_app()
            except ghauth.AuthError as e:
                raise RemoteError(f"{e} — {e.hint}" if e.hint else str(e))
        self.token = token
        self._writer = None
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
                "commits": [x["sha"] for x in c.get("commits", [])],
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

    def list_dir(self, ref, path, depth=2):
        """The file paths under a directory at a ref, `depth` levels deep; [] when it is not there."""
        got = self._call("GET", f"/contents/{urllib.parse.quote(path.rstrip('/'))}?ref={ref}", ok404=True)
        if not isinstance(got, list):
            return []
        out = []
        for e in got:
            if e.get("type") == "file":
                out.append(e["path"])
            elif e.get("type") == "dir" and depth > 1:
                out += self.list_dir(ref, e["path"], depth - 1)
        return sorted(out)

    def blob_id(self, ref, path):
        got = self._call("GET", f"/contents/{urllib.parse.quote(path)}?ref={ref}", ok404=True)
        return (got or {}).get("sha")

    def check_runs(self, sha):
        rows = self._pages(f"/commits/{sha}/check-runs", key="check_runs")
        runs = [{"name": r["name"], "status": r["status"], "conclusion": r.get("conclusion"),
                 "head_sha": r.get("head_sha", sha), "id": r["id"],
                 "completed_at": r.get("completed_at"), "url": r.get("html_url") or "",
                 # what the check said about itself — shown to an implementer fixing a failure
                 "title": ((r.get("output") or {}).get("title") or "")[:200],
                 "summary": ((r.get("output") or {}).get("summary") or "")[:2000]} for r in rows]
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
        return [{"id": c["id"], "body": c.get("body") or "", "user": (c.get("user") or {}).get("login", ""),
                 "app": (c.get("performed_via_github_app") or {}).get("slug", "")}
                for c in self._pages(f"/issues/{int(number)}/comments")]

    def writer(self):
        """Who devflow writes as: the token's login, and — for the App login's token — the App its
        writes are performed via. What devflow reads back as its own must carry both; a comment
        anyone else wrote, or the same person wrote by hand, is not devflow's. Not knowing who
        devflow is, is a tool failure, never "anyone"."""
        if self._writer is None:
            me = self._call("GET", f"{self.API}/user") or {}
            app = ""
            if self.via_app:
                got = self._call("GET", f"{self.API}/user/installations") or {}
                slugs = {i.get("app_slug") for i in got.get("installations") or [] if i.get("app_slug")}
                if len(slugs) != 1:
                    raise RemoteError(f"cannot tell which App this login writes through (installations: {sorted(slugs)})")
                app = slugs.pop()
            if not me.get("login"):
                raise RemoteError("cannot tell which account this token writes as")
            self._writer = {"login": me["login"], "app": app}
        return dict(self._writer)

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

    # --- what an implementation needs: a tree to work in, a commit, a pull request
    def branch_head_or_none(self, branch):
        got = self._call("GET", f"/branches/{urllib.parse.quote(branch, safe='')}", ok404=True)
        return (got or {}).get("commit", {}).get("sha")

    def find_pull(self, head_ref):
        """The newest pull request whose head is this repository's `head_ref`, or None."""
        owner = self.repo.split("/")[0]
        rows = self._call("GET", f"/pulls?state=all&head={urllib.parse.quote(owner + ':' + head_ref)}&per_page=10")
        rows = sorted(rows or [], key=lambda p: p["number"], reverse=True)
        return self.pull(rows[0]["number"]) if rows else None

    def create_pull(self, head_ref, base_ref, title, body):
        r = self._call("POST", "/pulls", {"head": head_ref, "base": base_ref, "title": title[:250], "body": body})
        return r["number"]

    def commit(self, sha):
        """{message, parents, author} of a commit, or None when there is none."""
        c = self._call("GET", f"/git/commits/{sha}", ok404=True)
        if c is None:
            return None
        return {"message": c.get("message") or "", "parents": [p["sha"] for p in c.get("parents") or []],
                "author": (c.get("author") or {}).get("name", "")}

    def materialize(self, sha, dest, only=None):
        """The tree of `sha`, written under dest (the archive's top directory removed). `only`: a
        path prefix to keep. Returns the paths written."""
        import tarfile, tempfile
        url = f"{self.API}/repos/{self.repo}/tarball/{sha}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}",
                                                   "User-Agent": "agent-stack-devflow"})
        written = []
        with tempfile.TemporaryFile() as tmp:
            try:
                with self._open(req, timeout=300) as r:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        tmp.write(chunk)
            except urllib.error.HTTPError as e:
                raise RemoteError(f"tarball {sha[:12]}: HTTP {e.code}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise RemoteError(f"tarball {sha[:12]}: {type(e).__name__}: {e} "
                                  "(the archive is served from codeload.github.com: docker/egress/allow.local)")
            tmp.seek(0)
            with tarfile.open(fileobj=tmp, mode="r:*") as tf:
                for m in tf.getmembers():
                    if not m.isfile():
                        continue
                    rel = m.name.split("/", 1)[1] if "/" in m.name else ""
                    if not rel or ".." in rel.split("/") or (only and not rel.startswith(only)):
                        continue
                    out = os.path.join(dest, rel)
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with tf.extractfile(m) as src, open(out, "wb") as f:
                        f.write(src.read())
                    if m.mode & 0o111:
                        os.chmod(out, 0o755)
                    written.append(rel)
        return written

    def push_commit(self, branch, parent, changes, message, author, base=None, extra_parents=()):
        """One commit with `changes` ({path: (bytes, mode) | None for a deletion}) applied to the tree
        of `base` (default `parent`), whose parents are `parent` and `extra_parents`, and `branch`
        moved to it — created if absent, fast-forwarded if present, never forced. A branch that
        moved away from `parent` meanwhile is a Conflict: someone else pushed.

        `base` and `extra_parents` are how a change is brought onto a newer canonical head without
        rewriting what was pushed: the tree is canonical's plus the change, the parents are the
        branch's head and canonical — a merge the branch fast-forwards to."""
        import base64
        base = base or parent
        tree_items = []
        for path, ch in sorted(changes.items()):
            if ch is None:
                tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            data, mode = ch
            blob = self._call("POST", "/git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"})
            tree_items.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
        base_tree = self._call("GET", f"/git/commits/{base}")["tree"]["sha"]
        tree = self._call("POST", "/git/trees", {"base_tree": base_tree, "tree": tree_items})
        commit = self._call("POST", "/git/commits", {"message": message, "tree": tree["sha"],
                                                    "parents": [parent] + list(extra_parents),
                                                    "author": author})["sha"]
        now = self.branch_head_or_none(branch)
        if now is None:
            self.create_ref(f"refs/heads/{branch}", commit)
        elif now != parent:
            raise Conflict(f"{branch} is at {now[:12]}, not at {parent[:12]}: someone else pushed")
        else:
            self._call("PATCH", f"/git/refs/heads/{branch}", {"sha": commit, "force": False})
        return commit


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
        return {"files": files, "authors": sorted(authors), "commits": sorted(seen), "merge_base": base}

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

    WRITER = {"login": "devflow", "app": "devflow-app"}

    def writer(self):
        self._up()
        return dict(self.r.get("writer") or self.WRITER)

    def list_dir(self, ref, path, depth=2):
        self._up()
        sha = self.r["branches"].get(ref, ref)
        pre = path.rstrip("/") + "/"
        return sorted(p for p in self._files(sha) if p.startswith(pre) and p[len(pre):].count("/") < depth)

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
        w = self.writer()          # reads the store; take it before holding a thread of it
        self._up()
        t = self._thread(number)
        cid = int(time.time() * 1000) % 10**9 + len(t.setdefault("comments", []))
        t["comments"].append({"id": cid, "body": body, "user": w["login"], "app": w["app"]})
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

    # --- what an implementation needs
    def branch_head_or_none(self, branch):
        self._up()
        return self.r["branches"].get(branch)

    def find_pull(self, head_ref):
        self._up()
        rows = sorted((p for p in self.r["pulls"].values() if p.get("head_ref") == head_ref),
                      key=lambda p: int(p["number"]), reverse=True)
        return self.pull(rows[0]["number"]) if rows else None

    def commit(self, sha):
        self._up()
        c = self.r["commits"].get(sha)
        if c is None:
            return None
        return {"message": c.get("message", ""), "parents": list(c.get("parents", [])), "author": c.get("author", "")}

    def create_pull(self, head_ref, base_ref, title, body):
        self._up()
        if head_ref not in self.r["branches"]:
            raise Conflict(f"fixture: no branch {head_ref}")
        n = max([int(k) for k in self.r["pulls"]] + [int(k) for k in self.r["issues"]] + [0]) + 1
        self.r["pulls"][str(n)] = {"number": n, "state": "open", "merged": False, "merge_commit_sha": None,
                                   "head_sha": self.r["branches"][head_ref], "head_ref": head_ref,
                                   "base_ref": base_ref, "base_sha": self.r["branches"][base_ref],
                                   "author": self.db.get("token_user", "operator"), "title": title,
                                   "body": body, "comments": []}
        self._save()
        self._lose("create_pull")
        return n

    def materialize(self, sha, dest, only=None):
        self._up()
        written = []
        for rel, text in self._files(self.r["branches"].get(sha, sha)).items():
            if only and not rel.startswith(only):
                continue
            out = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if text.startswith("base64:"):          # a binary file the fixture carries whole
                import base64
                with open(out, "wb") as f:
                    f.write(base64.b64decode(text[7:]))
            else:
                with open(out, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
            written.append(rel)
        return written

    def push_commit(self, branch, parent, changes, message, author, base=None, extra_parents=()):
        import hashlib
        self._up()
        now = self.r["branches"].get(branch)
        if now is not None and now != parent:
            raise Conflict(f"fixture: {branch} is at {now}, not at {parent}")
        files = dict(self._files(base or parent))
        for path, ch in changes.items():
            if ch is None:
                files.pop(path, None)
            else:
                files[path] = ch[0].decode("utf-8")
        new = "d" + hashlib.sha1(json.dumps([parent, message, files], sort_keys=True).encode()).hexdigest()[:39]
        self.r["commits"][new] = {"parents": [parent] + list(extra_parents), "files": files,
                                  "author": author.get("name", ""), "message": message}
        self.r["branches"][branch] = new
        for p in self.r["pulls"].values():
            if p.get("head_ref") == branch and p.get("state") == "open":
                p["head_sha"] = new
        self._save()
        self._lose("push_commit")
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
