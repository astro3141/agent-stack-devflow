"""What a model is given to read besides the change: a tree fetched once per commit, the project's
reference material as text, the official criterion's own words, the hosts its role may fetch from.

A library for work.py (the researcher's and the implementer's workspace) and evaluate.py (a
reviewer's frozen materials) — so a research record's reviewer is given exactly what its author was
given, not a thinner copy.
"""
import hashlib, os, shutil

import dfcore


def cached_tree(rem, sha, cache, run, only=None):
    """The tree of `sha` (or its `only` part), fetched once per commit and kept: a commit's tree
    never changes, and the corpus a project references is large."""
    key = sha + ("-" + hashlib.sha1(only.encode()).hexdigest()[:8] if only else "")
    dest = os.path.join(cache, rem.repo.replace("/", "__"), key)
    if os.path.isfile(os.path.join(dest, ".complete")):
        return dest
    tmp = dest + f".tmp-{run}"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    rem.materialize(sha, tmp, only=only)
    open(os.path.join(tmp, ".complete"), "w").close()
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return dest


def copy_tree(src, dest):
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".complete"))


def as_text(data, limit):
    """Reference text as UTF-8, or None for what a model cannot read (binary, too large)."""
    if len(data) > limit or b"\x00" in data[:8192]:
        return None
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def references(rem, impl, dest, cache, run):
    """The project's reference material (policy `implement.references`), text only, as UTF-8, under
    dest/<as>/. Returns [{as, ref, sha, files, path, note}]."""
    out = []
    shutil.rmtree(dest, ignore_errors=True)
    for r in impl.get("references") or []:
        name = "".join(c for c in str(r.get("as") or "ref") if c.isalnum() or c in "-_") or "ref"
        sha = rem.branch_head_or_none(r["ref"]) if r.get("ref") else None
        if not sha:
            out.append({"as": name, "ref": r.get("ref"), "sha": None, "files": 0,
                        "note": "the reference branch is not on the remote"})
            continue
        src = cached_tree(rem, sha, cache, run, only=r.get("path"))
        limit, n = int(r.get("max_file_bytes") or 2_000_000), 0
        for d, _, files in os.walk(src):
            for f in files:
                if f == ".complete":
                    continue
                p = os.path.join(d, f)
                text = as_text(open(p, "rb").read(), limit)
                if text is None:
                    continue
                rel = os.path.relpath(p, os.path.join(src, r.get("path") or "")).replace(os.sep, "/")
                out_p = os.path.join(dest, name, rel)
                os.makedirs(os.path.dirname(out_p), exist_ok=True)
                open(out_p, "w", encoding="utf-8").write(text)
                n += 1
        out.append({"as": name, "ref": r["ref"], "sha": sha, "files": n, "path": os.path.join(dest, name),
                    "note": r.get("note") or ""})
    return out


def criteria_text(pol, hook_root, task, repo_dir, dest):
    """The official criterion's own words for the task, as the project's hook reads them from the
    official files in `repo_dir` (hook op `criteria_text`, optional), written under dest/.
    Returns (files, notes)."""
    shutil.rmtree(dest, ignore_errors=True)
    try:
        got = dfcore.run_hook(pol, hook_root, "criteria_text", {"task": task, "repo_dir": repo_dir})
    except dfcore.HookError as e:
        return [], [f"the project's hook gave no criteria text: {e}"]
    written = []
    for name, text in (got.get("files") or {}).items():
        safe = "".join(c for c in name if c.isalnum() or c in "._-")[:120] or "criterion.md"
        os.makedirs(dest, exist_ok=True)
        open(os.path.join(dest, safe), "w", encoding="utf-8").write(text)
        written.append(os.path.join(dest, safe))
    return written, list(got.get("notes") or [])


def web_hosts(role, pkg):
    """The hosts a role may fetch from (its `egress:` in the package's principals.yaml,
    agent-stack §48) — told to the model, so it does not spend its time on pages the network
    refuses."""
    try:
        import yaml
        doc = yaml.safe_load(open(os.path.join(pkg, "principals.yaml"), encoding="utf-8")) or {}
        hosts = ((doc.get("principals") or {}).get(role) or {}).get("egress")
    except Exception:
        hosts = None
    if hosts is None:
        return "whatever the stack's shared allowlist holds open"
    return ", ".join(hosts) if hosts else "none (the providers only)"
